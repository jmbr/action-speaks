"""The per-user session daemon: where its socket lives, and how to run it as a service.

Starting Lean loads the whole library environment, which costs seconds and a large resident
set. The HTTP server already amortizes that across requests, but only while somebody runs it
by hand; the CLI pays it on every invocation and the MCP server pays it once per process.
A daemon that outlives a single command removes both, and gives the machine one memory
budget rather than one per caller.

The daemon is deliberately **per user and per checkout**, not machine-wide:

* A verdict records the toolchain and the Mathlib, Physlib and Cslib revisions it was checked
  against, and a ledger entry means nothing without them. One daemon answering for two
  checkouts with different pins would answer from whichever it happened to load.
* The source guard is not a sandbox. Running a submission as the person who submitted it is
  one proposition; running other users' submissions as the daemon's owner is another.
* Ledgers and project trust are per-user state, and merging them would extend one user's
  trust decisions to everybody else.

A Unix socket under a private directory enforces that boundary with directory permissions
rather than with access control this project would have to write and then defend.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config

# `sun_path` is 108 bytes on Linux and 104 on macOS, including the terminator. Everything
# here stays under the smaller limit, because a path that binds on one and not the other is
# the kind of difference that only shows up on somebody else's machine.
SUN_PATH_MAX = 104

SOCKET_DIR = "action-speaks"


class DaemonError(RuntimeError):
    """The daemon cannot be addressed or started, or it refused a request."""


class DaemonUnreachable(DaemonError):
    """Nothing answered on the socket.

    Separate from `DaemonError` because only this one justifies retrying the work in the
    calling process. A daemon that answered with an error answered: repeating the request
    locally would hide a real refusal — an untrusted project, say — behind a second run that
    might well succeed for the wrong reason.
    """


@dataclass(frozen=True)
class Address:
    """Where the daemon listens, and what had to be assumed to decide that."""

    path: Path
    warning: str | None = None

    def exists(self) -> bool:
        return self.path.exists()


def _private(directory: Path) -> bool:
    """Owned by this user, and readable and writable by nobody else."""
    try:
        info = directory.stat()
    except OSError:
        return False
    return info.st_uid == os.getuid() and not (info.st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def runtime_dir() -> tuple[Path, str | None]:
    """The directory to put the socket in, and a warning if it is a fallback.

    `XDG_RUNTIME_DIR` is the one variable in the XDG Base Directory Specification with no
    specified default, because its guarantees — owned by the user, mode 0700, lifetime bound
    to the login session — cannot be synthesized by an application. The specification's
    instruction when it is unset is to "fall back to a replacement directory with similar
    capabilities and print a warning message", which is what happens here.

    It is honored on every platform when set: it is a standard, and reading it costs nothing.
    It is `pam_systemd` that creates `/run/user/$UID` on Linux, so it is equally absent under
    `sudo -i`, under cron, and in a minimal container.
    """
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured and Path(configured).is_absolute():
        # The spec requires absolute paths and says to ignore anything else.
        return Path(configured), None

    # macOS gives each user a private `TMPDIR` under /var/folders, which has the properties
    # asked for even though it is not called XDG_RUNTIME_DIR.
    temp = os.environ.get("TMPDIR")
    if temp and Path(temp).is_absolute() and _private(Path(temp)):
        return Path(temp), (
            "XDG_RUNTIME_DIR is not set; using the private TMPDIR instead. The socket may "
            "not be removed when you log out."
        )

    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / SOCKET_DIR / "run", (
        f"XDG_RUNTIME_DIR is not set and TMPDIR is not private; using {cache / SOCKET_DIR}"
        "/run instead. That directory is not cleared at logout, so a stale socket is removed "
        "on the next start rather than by the system."
    )


def checkout_id(config: Config) -> str:
    """A short, stable name for this checkout, so two of them never share a socket."""
    root = config.lean_dir.resolve().parent
    return hashlib.sha256(str(root).encode()).hexdigest()[:12]


def address(config: Config | None = None) -> Address:
    """Resolve the socket path without creating anything."""
    override = os.environ.get("ACTION_SPEAKS_SOCKET")
    if override:
        path = Path(override)
        if len(str(path).encode()) > SUN_PATH_MAX:
            raise DaemonError(
                f"ACTION_SPEAKS_SOCKET is {len(str(path).encode())} bytes, over the {SUN_PATH_MAX} "
                "the platform allows for a socket path"
            )
        return Address(path)

    config = config or Config.discover()
    base, warning = runtime_dir()
    path = base / SOCKET_DIR / f"{checkout_id(config)}.sock"
    if len(str(path).encode()) > SUN_PATH_MAX:
        # Long temporary directories are normal on macOS, so say what to do rather than
        # failing with the kernel's bare "path too long".
        raise DaemonError(
            f"the socket path {path} is {len(str(path).encode())} bytes, over the "
            f"{SUN_PATH_MAX} the platform allows. Set ACTION_SPEAKS_SOCKET to a shorter path."
        )
    return Address(path, warning)


def prepare(path: Path) -> None:
    """Create the socket's directory privately, and clear a socket nobody is listening on.

    A Unix socket file outlives the process that bound it, and binding over one fails with
    `EADDRINUSE`, so a daemon killed rather than stopped would otherwise block every later
    start. Connecting first is what distinguishes a stale file from a running daemon.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _private(directory):
        raise DaemonError(
            f"{directory} is readable by other users, so a socket there would be too. "
            "Fix its ownership and permissions, or set ACTION_SPEAKS_SOCKET elsewhere."
        )
    if not path.exists():
        return
    if connectable(path):
        raise DaemonError(f"a daemon is already listening on {path}")
    path.unlink()


def connectable(path: Path) -> bool:
    """Whether something is accepting connections on this socket right now."""
    if not path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


# -- client ----------------------------------------------------------------


class _UnixConnection(http.client.HTTPConnection):
    """`HTTPConnection` that dials a path instead of a host."""

    def __init__(self, path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self._path))


def call(
    path: str,
    payload: dict | None = None,
    *,
    address: "Address | None" = None,
    method: str = "POST",
    timeout: float = 600.0,
    config: Config | None = None,
) -> dict:
    """Send one request to the daemon and decode its reply."""
    addr = address or globals()["address"](config)
    connection = _UnixConnection(addr.path, timeout)
    try:
        body = json.dumps(payload or {}).encode()
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        decoded = json.loads(response.read().decode() or "{}")
    except TimeoutError as exc:
        # The daemon has the request and is still working on it. Running it again here would
        # duplicate the work rather than recover from anything.
        raise DaemonError(f"the daemon did not answer within {timeout}s: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise DaemonUnreachable(f"cannot reach the daemon on {addr.path}: {exc}") from exc
    finally:
        connection.close()
    if not isinstance(decoded, dict):
        raise DaemonError("the daemon returned a reply that is not a JSON object")
    if response.status >= 400:
        raise DaemonError(decoded.get("error", f"the daemon returned {response.status}"))
    return decoded


def available(config: Config | None = None) -> Address | None:
    """The daemon's address if one is listening and the caller has not opted out.

    Opting out matters for reproducing a verdict: `ACTION_SPEAKS_NO_DAEMON=1` forces the work into
    this process, against this checkout, with no question of which daemon answered.
    """
    if os.environ.get("ACTION_SPEAKS_NO_DAEMON"):
        return None
    try:
        addr = address(config)
    except DaemonError:
        return None
    return addr if connectable(addr.path) else None


# -- service units ---------------------------------------------------------


def unit_name(config: Config) -> str:
    return f"action-speaks-{checkout_id(config)}"


def inherited_socket() -> socket.socket | None:
    """The listening socket systemd passed us, if this process was socket-activated.

    With `Accept=no` systemd creates and listens on the socket itself, then starts the
    service with that descriptor as fd 3. Binding the path again would fail with
    `EADDRINUSE`, so the daemon has to take what it was given.
    """
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    try:
        count = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError:
        return None
    if count < 1:
        return None
    # SD_LISTEN_FDS_START. More than one would mean more than one ListenStream.
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=3)


def systemd_units(config: Config, python: Path | None = None) -> dict[str, str]:
    """A socket-activated user service for this checkout.

    Socket activation rather than a permanently running service: a warm pool holds several
    gigabytes, and an idle timeout lets the machine have that back when nobody is verifying,
    while the socket stays available so the next call still starts it.

    `%t` is the user's runtime directory, which systemd guarantees is mode 0700 and owned by
    the user, so the isolation argument above rests on something the service manager already
    maintains rather than on anything checked here.
    """
    root = config.lean_dir.resolve().parent
    python = python or Path(sys.executable)
    name = unit_name(config)
    socket_unit = f"""[Unit]
Description=action-speaks verification daemon socket ({root.name})
Documentation=https://github.com/jmbr/action-speaks

[Socket]
ListenStream=%t/{SOCKET_DIR}/{checkout_id(config)}.sock
# The daemon binds a socket systemd has already created, so the directory must exist first.
RuntimeDirectory={SOCKET_DIR}
# Both default to 0755, which would leave the socket's directory readable by every other
# user — and a later `action-speaks serve` run by hand refuses to bind in one.
RuntimeDirectoryMode=0700
DirectoryMode=0700
SocketMode=0600
RuntimeDirectoryPreserve=yes
Accept=no

[Install]
WantedBy=sockets.target
"""
    service_unit = f"""[Unit]
Description=action-speaks verification daemon ({root.name})
Documentation=https://github.com/jmbr/action-speaks
Requires={name}.socket
After={name}.socket

[Service]
Type=simple
# Pinned to the checkout that was built: a verdict records that checkout's toolchain and
# library revisions, and a daemon answering from a different one would record the wrong
# provenance.
WorkingDirectory={root}
Environment=ACTION_SPEAKS_ROOT={root}
ExecStart={python} -m action_speaks.cli serve --socket %t/{SOCKET_DIR}/{checkout_id(config)}.sock
# Lean processes need a moment to exit; killing them early leaves temporary files behind.
TimeoutStopSec=30
Restart=on-failure

[Install]
WantedBy=default.target
"""
    return {f"{name}.socket": socket_unit, f"{name}.service": service_unit}


def launchd_agent(config: Config, python: Path | None = None) -> tuple[str, str]:
    """The macOS equivalent: a launchd agent with the same socket and checkout pinning."""
    root = config.lean_dir.resolve().parent
    python = python or Path(sys.executable)
    label = f"org.action-speaks.{checkout_id(config)}"
    path = address(config).path
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>action_speaks.cli</string>
    <string>serve</string>
    <string>--socket</string>
    <string>{path}</string>
  </array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>EnvironmentVariables</key>
  <dict><key>ACTION_SPEAKS_ROOT</key><string>{root}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict>
</plist>
"""
    return f"{label}.plist", plist


def install_instructions(config: Config, python: Path | None = None) -> tuple[dict[Path, str], str]:
    """The files to write for this platform, and the commands that enable them."""
    name = unit_name(config)
    if sys.platform == "darwin":
        filename, plist = launchd_agent(config, python)
        target = Path.home() / "Library" / "LaunchAgents" / filename
        commands = (
            f"launchctl unload {target} 2>/dev/null || true\n"
            f"launchctl load {target}\n"
            "\nlaunchd starts the daemon at login. `launchctl list | grep action-speaks` shows it."
        )
        return {target: plist}, commands
    if sys.platform.startswith("linux"):
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        directory = base / "systemd" / "user"
        files = {directory / n: body for n, body in systemd_units(config, python).items()}
        commands = (
            "systemctl --user daemon-reload\n"
            f"systemctl --user enable --now {name}.socket\n"
            "\n# Without lingering the daemon stops at logout, which defeats the purpose:\n"
            f"loginctl enable-linger {os.environ.get('USER', '$USER')}\n"
            f"\n# Then check it: systemctl --user status {name}.service\n"
            "# The socket is created on start, so it does not need to survive a logout."
        )
        return files, commands
    return {}, (
        f"No service manager is generated for {sys.platform}. Run the daemon in the "
        "foreground instead:\n\n    action-speaks serve\n"
    )

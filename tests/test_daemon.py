"""The session daemon: where its socket goes, how it is reached, and what runs it.

None of this needs Lean. The socket transport is exercised against a trivial handler, and
the service units are checked as text, because what matters about them is that they pin the
checkout and do not depend on a shell.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import stat
import sys
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nullius import (
    cli,  # noqa: E402
    mcp_server,  # noqa: E402
)
from nullius import daemon as D  # noqa: E402
from nullius.config import Config  # noqa: E402
from nullius.http_server import UnixServer  # noqa: E402

CONFIG = Config(
    lean_dir=Path("/checkout/lean"),
    repl_bin=Path("unused-repl"),
    lake_bin=Path("unused-lake"),
    ledger_path=Path("unused-ledger.sqlite3"),
)


@pytest.fixture
def short_root() -> Iterator[Path]:
    """A deliberately short temporary root.

    pytest's `tmp_path` embeds the test name and a run counter, which together already reach
    98 of the 104 bytes a socket path may use, so it overflows as soon as that counter gains
    a digit. Only the test that is about the limit should be anywhere near it.
    """
    with tempfile.TemporaryDirectory(prefix="nul") as name:
        yield Path(name)


@pytest.fixture
def cache(short_root: Path) -> Path:
    return short_root / "c"


@pytest.fixture
def private(short_root: Path) -> Path:
    directory = short_root / "r"
    directory.mkdir(mode=0o700)
    return directory


@pytest.fixture(autouse=True)
def isolated(monkeypatch, cache: Path) -> None:
    """No test may reach a daemon the developer happens to be running."""
    for name in (
        "XDG_RUNTIME_DIR",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "NULLIUS_SOCKET",
        "NULLIUS_NO_DAEMON",
        "LISTEN_PID",
        "LISTEN_FDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))


# -- addressing ------------------------------------------------------------


def test_xdg_runtime_dir_is_honored_on_every_platform(monkeypatch, private: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(private))
    base, warning = D.runtime_dir()
    assert base == private
    assert warning is None


def test_relative_runtime_dir_is_ignored_as_the_spec_requires(monkeypatch, private: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative/path")
    monkeypatch.setenv("TMPDIR", str(private))
    base, warning = D.runtime_dir()
    assert base == private
    assert warning and "XDG_RUNTIME_DIR is not set" in warning


def test_private_tmpdir_is_the_first_fallback(monkeypatch, private: Path) -> None:
    """macOS gives each user a private TMPDIR, which has the properties asked for."""
    monkeypatch.setenv("TMPDIR", str(private))
    base, warning = D.runtime_dir()
    assert base == private
    assert warning is not None


def test_world_readable_tmpdir_is_refused_in_favor_of_the_cache(
    monkeypatch, short_root: Path, cache: Path
) -> None:
    shared = short_root / "shared"
    shared.mkdir(mode=0o755)
    monkeypatch.setenv("TMPDIR", str(shared))
    base, warning = D.runtime_dir()
    assert shared not in base.parents and base != shared
    assert base == cache / "nullius" / "run"
    assert warning and "not private" in warning


def test_fallback_always_warns_as_the_specification_requires(cache: Path) -> None:
    base, warning = D.runtime_dir()
    assert base == cache / "nullius" / "run"
    assert warning


def test_two_checkouts_never_share_a_socket() -> None:
    other = Config(
        lean_dir=Path("/elsewhere/lean"),
        repl_bin=CONFIG.repl_bin,
        lake_bin=CONFIG.lake_bin,
        ledger_path=CONFIG.ledger_path,
    )
    assert D.checkout_id(CONFIG) != D.checkout_id(other)
    assert D.address(CONFIG).path != D.address(other).path


def test_overlong_socket_path_is_explained_rather_than_left_to_the_kernel(
    monkeypatch, short_root: Path
) -> None:
    deep = short_root / ("d" * 120)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(deep))
    with pytest.raises(D.DaemonError, match="NULLIUS_SOCKET"):
        D.address(CONFIG)


def test_explicit_socket_overrides_the_resolver(monkeypatch, short_root: Path) -> None:
    chosen = short_root / "chosen.sock"
    monkeypatch.setenv("NULLIUS_SOCKET", str(chosen))
    assert D.address(CONFIG).path == chosen


# -- preparing and probing -------------------------------------------------


def test_prepare_creates_a_private_directory(short_root: Path) -> None:
    path = short_root / "run" / "nullius" / "a.sock"
    D.prepare(path)
    assert path.parent.is_dir()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_prepare_refuses_a_directory_others_can_read(short_root: Path) -> None:
    directory = short_root / "loose"
    directory.mkdir(mode=0o755)
    with pytest.raises(D.DaemonError, match="readable by other users"):
        D.prepare(directory / "a.sock")


def test_stale_socket_is_removed_rather_than_blocking_the_next_start(private: Path) -> None:
    """A daemon that was killed leaves a file behind, and binding over it fails."""
    path = private / "stale.sock"
    path.write_text("")
    D.prepare(path)
    assert not path.exists()


def test_a_live_daemon_is_not_displaced(private: Path) -> None:
    path = private / "live.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    try:
        assert D.connectable(path)
        with pytest.raises(D.DaemonError, match="already listening"):
            D.prepare(path)
        assert path.exists()
    finally:
        listener.close()
        path.unlink()


def test_absent_and_dead_sockets_are_not_connectable(private: Path) -> None:
    assert not D.connectable(private / "missing.sock")
    dead = private / "dead.sock"
    dead.write_text("")
    assert not D.connectable(dead)


def test_available_respects_the_opt_out(monkeypatch, private: Path) -> None:
    """Reproducing a verdict means knowing which checkout answered."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(private))
    monkeypatch.setenv("NULLIUS_NO_DAEMON", "1")
    assert D.available(CONFIG) is None


def test_available_is_none_when_nothing_listens(monkeypatch, private: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(private))
    assert D.available(CONFIG) is None


# -- socket activation -----------------------------------------------------


def test_no_inherited_socket_without_systemd(monkeypatch) -> None:
    assert D.inherited_socket() is None
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))
    monkeypatch.setenv("LISTEN_FDS", "1")
    assert D.inherited_socket() is None, "another process's descriptors are not ours"
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "0")
    assert D.inherited_socket() is None


# -- transport -------------------------------------------------------------


class Echo(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"ok": True, "peer": self.client_address[0]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def served(private: Path) -> Iterator[Path]:
    path = private / "s.sock"
    server = UnixServer(str(path), Echo)
    server.verbose = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield path
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_unix_server_answers_a_request(served: Path) -> None:
    connection = D._UnixConnection(served, 5.0)
    connection.request("GET", "/health")
    response = connection.getresponse()
    assert response.status == 200
    assert json.loads(response.read())["ok"] is True
    connection.close()


def test_unix_server_does_not_mistake_the_path_for_a_host_and_port(served: Path) -> None:
    """The stock `server_bind` reads `server_name` and `server_port` out of the address."""
    connection = http.client.HTTPConnection("localhost")
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.connect(str(served))
    connection.request("GET", "/health")
    assert connection.getresponse().status == 200
    connection.close()


def test_closing_the_server_removes_its_socket(private: Path) -> None:
    path = private / "gone.sock"
    server = UnixServer(str(path), Echo)
    assert path.exists()
    server.server_close()
    assert not path.exists(), "a leftover file makes the next bind fail with EADDRINUSE"


def test_an_inherited_socket_is_left_for_its_owner(private: Path) -> None:
    path = private / "kept.sock"
    server = UnixServer(str(path), Echo)
    server.owns_path = False
    server.server_close()
    assert path.exists(), "systemd owns a socket it created, and reuses it"
    path.unlink()


def test_client_reports_an_unreachable_daemon(private: Path) -> None:
    with pytest.raises(D.DaemonError, match="cannot reach the daemon"):
        D.call("/health", address=D.Address(private / "nothing.sock"), method="GET")


# -- service units ---------------------------------------------------------


def test_systemd_units_pin_the_checkout_and_its_interpreter() -> None:
    units = D.systemd_units(CONFIG, python=Path("/venv/bin/python3"))
    name = D.unit_name(CONFIG)
    service = units[f"{name}.service"]
    assert "Environment=NULLIUS_ROOT=/checkout" in service
    assert "WorkingDirectory=/checkout" in service
    assert "/venv/bin/python3 -m nullius.cli serve" in service
    # An activated virtualenv is exactly what a service does not have.
    assert "activate" not in service
    assert "TimeoutStopSec" in service


def test_systemd_unit_names_are_not_template_instances() -> None:
    """`nullius@id.service` would read as an instance of a template that does not exist."""
    assert "@" not in D.unit_name(CONFIG)


def test_socket_unit_provides_its_runtime_directory() -> None:
    units = D.systemd_units(CONFIG)
    socket_unit = units[f"{D.unit_name(CONFIG)}.socket"]
    assert "ListenStream=%t/nullius/" in socket_unit
    assert "RuntimeDirectory=nullius" in socket_unit
    assert "Accept=no" in socket_unit


def test_launchd_agent_pins_the_checkout() -> None:
    filename, plist = D.launchd_agent(CONFIG, python=Path("/venv/bin/python3"))
    assert filename.endswith(".plist")
    assert "<string>/checkout</string>" in plist
    assert "NULLIUS_ROOT" in plist


def test_linux_instructions_cover_lingering_and_reload(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    files, commands = D.install_instructions(CONFIG)
    assert any(str(p).endswith(".service") for p in files)
    assert all("systemd/user" in str(p) for p in files)
    assert "daemon-reload" in commands
    # Without lingering the daemon dies at logout, which defeats the point.
    assert "enable-linger" in commands


def test_unsupported_platform_falls_back_to_the_foreground(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "freebsd14")
    files, commands = D.install_instructions(CONFIG)
    assert files == {}
    assert "nullius serve" in commands


def test_a_failed_bind_does_not_delete_the_winners_socket(private: Path) -> None:
    """`TCPServer.__init__` closes the server when `server_bind` raises."""
    path = private / "contested.sock"
    winner = UnixServer(str(path), Echo)
    try:
        with pytest.raises(OSError):
            UnixServer(str(path), Echo)
        assert path.exists(), "the loser unlinked a socket it never bound"
        assert D.connectable(path)
    finally:
        winner.server_close()


def test_socket_unit_keeps_its_directory_private() -> None:
    """systemd defaults both to 0755, which `prepare` then refuses as world-readable."""
    socket_unit = D.systemd_units(CONFIG)[f"{D.unit_name(CONFIG)}.socket"]
    assert "RuntimeDirectoryMode=0700" in socket_unit
    assert "DirectoryMode=0700" in socket_unit


def test_transport_failure_and_a_refusal_are_different(private: Path) -> None:
    """Only an unreachable daemon justifies redoing the work locally."""
    assert issubclass(D.DaemonUnreachable, D.DaemonError)
    with pytest.raises(D.DaemonUnreachable):
        D.call("/health", address=D.Address(private / "absent.sock"), method="GET")

    class Refusing(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.dumps({"error": "project is untrusted"}).encode()
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    path = private / "refuse.sock"
    server = UnixServer(str(path), Refusing)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(D.DaemonError, match="project is untrusted") as caught:
            D.call("/verify", {"source": "x"}, address=D.Address(path))
        assert not isinstance(caught.value, D.DaemonUnreachable), (
            "a daemon that answered must not be retried in process"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_socket_activation_is_not_mistaken_for_a_rival_daemon(monkeypatch, private: Path) -> None:
    """systemd binds and listens before starting us, so a connect to it succeeds.

    Probing the path first would make every activated start report "already listening" and
    exit 1, which `Restart=on-failure` turns into a restart loop.
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(private))
    monkeypatch.setattr(D, "connectable", lambda path: True)
    monkeypatch.setattr(D, "inherited_socket", object)
    served = {}

    def record(argv):
        served["argv"] = argv
        return 0

    monkeypatch.setattr(cli.http_server, "main", record)

    args = argparse.Namespace(socket=None, pool=1, no_warm=True, install=False, print_units=False)
    assert cli.cmd_serve(args) == 0, "an activated daemon refused to start"
    assert "--socket" in served["argv"]
    # The daemon supplies Lean sessions; the caller owns the ledger.
    assert "--no-log" in served["argv"]


def test_a_rival_daemon_is_still_refused_when_not_activated(monkeypatch, private: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(private))
    monkeypatch.setattr(D, "connectable", lambda path: True)
    monkeypatch.setattr(D, "inherited_socket", lambda: None)
    monkeypatch.setattr(
        cli.http_server, "main", lambda argv: pytest.fail("started over a live daemon")
    )
    args = argparse.Namespace(socket=None, pool=1, no_warm=True, install=False, print_units=False)
    assert cli.cmd_serve(args) == 1


# -- MCP routing -----------------------------------------------------------
#
# Without this, every agent on the machine holds its own Lean session: measured at ~7.6 GB
# resident each, about half private. What matters is not merely that the daemon is preferred
# but that no local session is created when it answers, so these assert on `session` being
# untouched rather than on the reply.


@pytest.fixture
def no_local_session(monkeypatch):
    """Make starting a Lean session in this process an error."""

    def forbidden():
        raise AssertionError("started a local Lean session while a daemon was available")

    monkeypatch.setattr(mcp_server, "session", forbidden)


def verdict_reply() -> dict:
    return {
        "status": "verified",
        "target": "t",
        "claim": "c",
        "checks": [],
        "axioms": [],
        "statement": "True",
        "verified": True,
        "render": "VERIFIED: t",
        "feedback": "",
    }


def test_mcp_verify_uses_the_daemon_without_starting_a_session(
    monkeypatch, private: Path, no_local_session
) -> None:
    monkeypatch.setattr(D, "available", lambda cfg=None: D.Address(private / "s.sock"))
    seen = {}

    def call(path, payload=None, **kw):
        seen["path"], seen["payload"] = path, payload
        return verdict_reply()

    monkeypatch.setattr(D, "call", call)
    verdict = mcp_server.verify_source(CONFIG, {"claim": "c"}, "theorem t : True := trivial")
    assert verdict.verified
    assert seen["path"] == "/verify"
    assert seen["payload"]["source"] == "theorem t : True := trivial"


def test_mcp_statement_and_close_use_the_daemon_too(
    monkeypatch, private: Path, no_local_session
) -> None:
    monkeypatch.setattr(D, "available", lambda cfg=None: D.Address(private / "s.sock"))
    paths = []

    def call(path, payload=None, **kw):
        paths.append(path)
        if path == "/statement":
            return {"ok": True, "binders": 0, "statement": "True"}
        return {"query": "g", "backend": "local", "hits": [], "error": None, "note": ""}

    monkeypatch.setattr(D, "call", call)
    assert mcp_server.check_statement(CONFIG, ": True")["ok"]
    assert mcp_server.find_proof(CONFIG, "True", "", ("exact?",)).backend == "local"
    assert paths == ["/statement", "/find_proof"]


def test_mcp_project_selection_reaches_the_daemon(
    monkeypatch, private: Path, no_local_session
) -> None:
    monkeypatch.setattr(D, "available", lambda cfg=None: D.Address(private / "s.sock"))
    seen = {}
    monkeypatch.setattr(
        D, "call", lambda path, payload=None, **kw: seen.update(payload) or verdict_reply()
    )
    mcp_server.verify_source(CONFIG, {"project": "demo"}, "theorem t : True := trivial")
    assert seen["project"] == "demo", "the daemon resolves the project, so it must be told"


def test_mcp_falls_back_when_the_daemon_stops_between_probe_and_call(
    monkeypatch, private: Path
) -> None:
    """The window the CLI also guards: probed present, gone by the time we call."""
    monkeypatch.setattr(D, "available", lambda cfg=None: D.Address(private / "s.sock"))

    def unreachable(*a, **kw):
        raise D.DaemonUnreachable("gone")

    monkeypatch.setattr(D, "call", unreachable)
    used = {}
    monkeypatch.setattr(mcp_server, "session", lambda: used.setdefault("local", True))
    monkeypatch.setattr(
        mcp_server,
        "Verifier",
        lambda *a, **kw: type("V", (), {"verify": lambda s, *a, **k: "local"})(),
    )
    assert mcp_server.verify_source(CONFIG, {}, "theorem t : True := trivial") == "local"
    assert used.get("local"), "an unreachable daemon must not stop the check"


def test_mcp_does_not_retry_a_refusal_in_process(
    monkeypatch, private: Path, no_local_session
) -> None:
    """A daemon that answered has answered; redoing the work could hide the reason."""
    monkeypatch.setattr(D, "available", lambda cfg=None: D.Address(private / "s.sock"))

    def refused(*a, **kw):
        raise D.DaemonError("project is untrusted")

    monkeypatch.setattr(D, "call", refused)
    with pytest.raises(D.DaemonError, match="untrusted"):
        mcp_server.verify_source(CONFIG, {}, "theorem t : True := trivial")


def test_mcp_works_normally_with_no_daemon(monkeypatch) -> None:
    monkeypatch.setenv("NULLIUS_NO_DAEMON", "1")
    used = {}
    monkeypatch.setattr(mcp_server, "session", lambda: used.setdefault("local", True))
    monkeypatch.setattr(
        mcp_server,
        "Verifier",
        lambda *a, **kw: type("V", (), {"verify": lambda s, *a, **k: "local"})(),
    )
    assert mcp_server.verify_source(CONFIG, {}, "theorem t : True := trivial") == "local"
    assert used.get("local")

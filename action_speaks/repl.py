"""Persistent Lean REPL session.

The expensive part of talking to Lean is `import Mathlib` (seconds, and several GB
resident). We pay it once per process and then branch every subsequent check off the
resulting environment, which costs milliseconds.

Branching off a *pristine* environment is not merely an optimisation, it is a soundness
requirement: if check *n* ran in the environment left behind by check *n-1*, an agent could
declare `axiom evil : False` in one call and quietly use it in the next. `Session.run`
therefore always passes the base environment unless a caller explicitly chains.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import Config

PRELUDE = "import Mathlib\nimport Physlib\nimport Cslib\nimport ActionSpeaks.Audit"

AUDIT_MARKER = "ACTION_SPEAKS_AUDIT "


class ReplError(RuntimeError):
    pass


class ReplTimeout(ReplError):
    pass


@dataclass
class ReplResponse:
    """A decoded REPL reply, with the pieces the pipeline cares about pulled out."""

    raw: dict[str, Any]
    elapsed: float = 0.0

    @property
    def env(self) -> int | None:
        return self.raw.get("env")

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.raw.get("messages", []) or []

    @property
    def sorries(self) -> list[dict[str, Any]]:
        return self.raw.get("sorries", []) or []

    def by_severity(self, sev: str) -> list[str]:
        return [str(m.get("data", "")) for m in self.messages if m.get("severity") == sev]

    @property
    def errors(self) -> list[str]:
        return self.by_severity("error")

    @property
    def warnings(self) -> list[str]:
        return self.by_severity("warning")

    @property
    def infos(self) -> list[str]:
        return self.by_severity("info")

    @property
    def ok(self) -> bool:
        return not self.errors

    def audit_records(self) -> list[dict[str, Any]]:
        """Decode the `ACTION_SPEAKS_AUDIT {json}` messages emitted by `ActionSpeaks.Audit`."""
        out: list[dict[str, Any]] = []
        for text in self.infos:
            for line in text.splitlines():
                line = line.strip()
                if line.startswith(AUDIT_MARKER):
                    try:
                        out.append(json.loads(line[len(AUDIT_MARKER) :]))
                    except json.JSONDecodeError:
                        pass
        return out

    def format_messages(self, limit: int = 4000) -> str:
        parts = []
        for m in self.messages:
            data = str(m.get("data", ""))
            if data.startswith(AUDIT_MARKER):
                continue
            pos = m.get("pos") or {}
            where = f"{pos.get('line', '?')}:{pos.get('column', '?')}"
            parts.append(f"[{m.get('severity')}] line {where}: {data}")
        text = "\n".join(parts)
        return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


class Session:
    """A single long-lived `repl` process with Mathlib preloaded."""

    def __init__(self, config: Config | None = None, prelude: str = PRELUDE):
        self.config = config or Config.discover()
        self.prelude = prelude
        self.proc: subprocess.Popen | None = None
        self.base_env: int | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.RLock()
        self._stderr: list[str] = []
        self.started_at: float | None = None
        self.startup_seconds: float | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, timeout: float | None = None) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self.config.validate()
            timeout = timeout or self.config.startup_timeout
            env = dict(os.environ)
            env.setdefault("LEAN_NUM_THREADS", str(self.config.lean_threads))
            t0 = time.time()
            self.proc = subprocess.Popen(
                [str(self.config.lake_bin), "env", str(self.config.repl_bin)],
                cwd=str(self.config.lean_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,
            )
            self._lines = queue.Queue()
            self._stderr = []
            threading.Thread(target=self._pump_stdout, args=(self.proc,), daemon=True).start()
            threading.Thread(target=self._pump_stderr, args=(self.proc,), daemon=True).start()

            resp = self._exchange({"cmd": self.prelude}, timeout=timeout)
            if resp.errors:
                self.close()
                raise ReplError(f"prelude failed: {resp.errors}")
            self.base_env = resp.env
            self.started_at = time.time()
            self.startup_seconds = time.time() - t0

    def close(self) -> None:
        with self._lock:
            proc, self.proc = self.proc, None
            self.base_env = None
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass

    def restart(self) -> None:
        self.close()
        self.start()

    def __enter__(self) -> "Session":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- plumbing ----------------------------------------------------------

    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    self._lines.put(line)
        except Exception:
            pass
        finally:
            self._lines.put(None)

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stderr is not None:
                for line in proc.stderr:
                    self._stderr.append(line)
                    del self._stderr[:-200]
        except Exception:
            pass

    def _exchange(self, payload: dict[str, Any], timeout: float) -> ReplResponse:
        """Write one command and read exactly one reply.

        The REPL pretty-prints each reply across several lines and separates replies with a
        blank line, so we accumulate until the buffer parses as JSON.
        """
        proc = self.proc
        if proc is None or proc.poll() is not None:
            raise ReplError("REPL process is not running")
        assert proc.stdin is not None

        started = time.time()
        try:
            proc.stdin.write(json.dumps(payload) + "\n\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise ReplError(f"failed to write to REPL: {exc}") from exc

        buf: list[str] = []
        deadline = started + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                # A wedged Lean process cannot be interrupted politely; killing it is the
                # only reliable recovery, and the pool will start a fresh one.
                self.close()
                raise ReplTimeout(f"no reply within {timeout:.0f}s (REPL killed)")
            try:
                line = self._lines.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if line is None:
                err = "".join(self._stderr[-20:])
                self.close()
                raise ReplError(f"REPL exited unexpectedly. stderr:\n{err}")
            buf.append(line)
            if line.strip() == "" or line.rstrip().endswith("}"):
                text = "".join(buf).strip()
                if not text:
                    buf.clear()
                    continue
                try:
                    return ReplResponse(json.loads(text), time.time() - started)
                except json.JSONDecodeError:
                    continue

    # -- public API --------------------------------------------------------

    def run(
        self,
        code: str,
        env: int | None = -1,
        timeout: float | None = None,
        retry: bool = True,
    ) -> ReplResponse:
        """Elaborate `code`.

        `env=-1` (the default) means "the pristine Mathlib environment", isolating this call
        from every other call. Pass an explicit env id to chain deliberately, or `None` to
        start a fresh environment (required when the code has its own `import`s).
        """
        with self._lock:
            self.start()
            try:
                return self._exchange(
                    self._payload(code, env), timeout or self.config.command_timeout
                )
            except ReplTimeout:
                raise
            except ReplError:
                if not retry:
                    raise
                self.restart()
                return self._exchange(
                    self._payload(code, env), timeout or self.config.command_timeout
                )

    def _payload(self, code: str, env: int | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"cmd": code}
        use_env = self.base_env if env == -1 else env
        if use_env is not None:
            payload["env"] = use_env
        return payload

    def tactic(self, tactic: str, proof_state: int, timeout: float | None = None) -> ReplResponse:
        with self._lock:
            self.start()
            return self._exchange(
                {"tactic": tactic, "proofState": proof_state},
                timeout or self.config.command_timeout,
            )

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class SessionPool:
    """Pre-warmed sessions so concurrent checks do not queue behind one another.

    Each session holds a full Mathlib environment. The `.olean` files are memory-mapped and
    shared between processes, so the marginal cost of an extra session is much lower than
    the first one suggests.
    """

    def __init__(self, config: Config | None = None, size: int | None = None):
        self.config = config or Config.discover()
        self.size = max(1, size if size is not None else self.config.pool_size)
        self._free: queue.Queue[Session] = queue.Queue()
        self._all: list[Session] = []
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        with self._lock:
            while len(self._all) < self.size:
                s = Session(self.config)
                self._all.append(s)
                self._free.put(s)

    def warm(self) -> None:
        self._ensure()
        for s in list(self._all):
            s.start()

    def acquire(self, timeout: float = 600.0) -> Session:
        self._ensure()
        s = self._free.get(timeout=timeout)
        try:
            s.start()
        except Exception:
            self._free.put(s)
            raise
        return s

    def release(self, s: Session) -> None:
        self._free.put(s)

    def close(self) -> None:
        with self._lock:
            for s in self._all:
                s.close()
            self._all.clear()
            self._free = queue.Queue()

    def __enter__(self) -> "SessionPool":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _Borrowed:
    """Context manager yielding a pooled session and always returning it."""

    def __init__(self, pool: SessionPool, timeout: float = 600.0):
        self.pool = pool
        self.timeout = timeout
        self.session: Session | None = None

    def __enter__(self) -> Session:
        self.session = self.pool.acquire(self.timeout)
        return self.session

    def __exit__(self, *exc) -> None:
        if self.session is not None:
            self.pool.release(self.session)


def borrow(pool: SessionPool, timeout: float = 600.0) -> _Borrowed:
    return _Borrowed(pool, timeout)

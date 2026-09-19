"""Finding Mathlib lemmas.

In practice an agent's proofs fail far more often because it cannot find the right lemma
name than because it cannot do the mathematics. Mathlib has hundreds of thousands of
declarations and models invent plausible-sounding names that do not exist.

Three complementary routes are offered:

* `loogle` - search by *shape*: `Nat.succ_le_succ`, `(?a + ?b) * ?c`, `|- Continuous _`.
  Exact, fast, and the right tool when you know the form of the statement. Runs against a
  local index built from the same `.olean` files a submission is checked against, so it
  covers Physlib and agrees with the verifier about which lemmas exist. Requires
  `scripts/build-loogle.sh`; if that has not been run, the shortfall is reported rather than
  papered over with a different index.
* `loogle_remote` - the same, against the hosted service, when explicitly asked for. Handy
  when no local index has been built, but its answers describe a different Mathlib revision
  and no Physlib.
* `leansearch` - search by *meaning*, in natural language. Right when you know what you want
  mathematically but not how Mathlib spells it. Remote only: it is a hosted semantic model,
  with nothing to run locally.
* `local_search` - runs `exact?`, `apply?`, `rw?` and friends inside the REPL against a real
  goal. Slower, but authoritative: whatever it returns actually closes the goal.

The HTTP backends are best-effort. They are external services, so every call is wrapped with
a timeout and degrades to an error record rather than raising, letting the agent fall back to
the local route.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from .repl import ReplError, ReplTimeout, Session

if TYPE_CHECKING:
    from .config import Config

LOOGLE_URL = "https://loogle.lean-lang.org/json"
LEANSEARCH_URL = "https://leansearch.net/search"
USER_AGENT = "nullius-verifier/0.1 (+local research tool)"


@dataclass
class Hit:
    name: str
    kind: str = ""
    module: str = ""
    signature: str = ""
    doc: str = ""
    source: str = ""
    ledger: dict[str, Any] | None = None

    def render(self) -> str:
        head = f"{self.name}"
        if self.signature:
            head += f" : {self.signature}"
        tail = f"    [{self.source}{(' / ' + self.module) if self.module else ''}]"
        if self.ledger:
            ids = ", ".join(f"#{i}" for i in self.ledger["ids"])
            tail += f"\n    ledger {ids}; retrieve with log --id {self.ledger['ids'][0]}"
            tail += "\n    Not preloaded: retrieve the source and verify any new submission."
        return head + "\n" + tail


@dataclass
class SearchResult:
    query: str
    backend: str
    hits: list[Hit] = field(default_factory=list)
    error: str | None = None
    note: str = ""
    index: dict[str, Any] | None = None

    def render(self, limit: int = 10) -> str:
        if self.error:
            return f"{self.backend} search failed: {self.error}"
        if not self.hits:
            text = f"{self.backend}: no results for {self.query!r}"
            return text + (f"\n  ({self.note})" if self.note else "")
        lines = [f"{self.backend}: {len(self.hits)} hit(s) for {self.query!r}"]
        if self.note:
            lines.append(f"  ({self.note})")
        for h in self.hits[:limit]:
            lines.append("  " + h.render().replace("\n", "\n  "))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "query": self.query,
            "backend": self.backend,
            "error": self.error,
            "note": self.note,
            "hits": [
                {
                    "name": h.name,
                    "kind": h.kind,
                    "module": h.module,
                    "signature": h.signature,
                    "doc": h.doc[:500],
                    "source": h.source,
                    **({"ledger": h.ledger} if h.ledger is not None else {}),
                }
                for h in self.hits
            ],
        }
        if self.index is not None:
            result["index"] = self.index
        return result


def _http_json(
    url: str,
    *,
    data: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body else {}),
        },
        method="POST" if body else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _parse_loogle_json(query: str, raw: Any, limit: int, source: str) -> SearchResult:
    """Decode Loogle's JSON. The local binary and the hosted service emit the same shape."""
    if isinstance(raw, dict) and raw.get("error"):
        sugg = raw.get("suggestions") or []
        note = f"suggestions: {', '.join(map(str, sugg))}" if sugg else ""
        return SearchResult(query, source, error=str(raw["error"]), note=note)

    hits = []
    for h in (raw.get("hits") or [])[:limit]:
        hits.append(
            Hit(
                name=str(h.get("name", "")),
                module=str(h.get("module", "") or ""),
                signature=str(h.get("type", "") or "").strip(),
                doc=str(h.get("doc") or ""),
                source=source,
            )
        )
    return SearchResult(query, source, hits, note=str(raw.get("header", "") or "").strip())


class LoogleSession:
    """A persistent local Loogle process.

    Loading the search index costs seconds and dwarfs the query itself, so the process is
    kept alive between queries exactly as the Lean REPL session is: the first call pays for
    the index, the rest are milliseconds.

    A local index is worth having for two reasons beyond latency. It covers Physlib, which
    the hosted service does not index at all, and it is built from the very `.olean` files a
    submission is checked against — so it cannot report a lemma that this verifier's Mathlib
    does not have, or miss one that it does.
    """

    def __init__(
        self,
        binary: Path | None = None,
        lean_dir: Path | None = None,
        module: str | None = None,
        lake_bin: Path | None = None,
        *,
        index_file: Path | None = None,
        index_mode: str = "use",
        environment: dict[str, str] | None = None,
        direct: bool = False,
    ) -> None:
        from .config import Config

        cfg: Config | None = None
        if binary is None or lean_dir is None or lake_bin is None:
            try:
                cfg = Config.discover()
            except Exception:
                cfg = None
        self.binary = binary or (cfg.loogle_bin if cfg else None)
        self.lean_dir = lean_dir or (cfg.lean_dir if cfg else Path("lean"))
        self.lake_bin = lake_bin or (cfg.lake_bin if cfg else Path("lake"))
        self.module = module or (
            cfg.loogle_module if cfg else os.environ.get("NULLIUS_LOOGLE_MODULE", "NulliusAll")
        )
        self.proc: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self.startup_seconds: float | None = None
        self.index_file = index_file
        self.index_mode = index_mode
        self.environment = environment
        self.direct = direct
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=20)

    @staticmethod
    def _pump(stream: TextIO, lines: queue.Queue[str | None]) -> None:
        try:
            for line in stream:
                lines.put(line)
        finally:
            lines.put(None)

    @staticmethod
    def _pump_errors(stream: TextIO, errors: deque[str]) -> None:
        for line in stream:
            errors.append(line)

    def _readline(self, timeout: float) -> str:
        try:
            line = self._lines.get(timeout=max(0.001, timeout))
        except queue.Empty as exc:
            raise ReplTimeout(f"Loogle did not reply within {timeout:.1f}s") from exc
        if line is None:
            raise ReplError("Loogle exited: " + "".join(self._stderr).strip())
        return line

    @property
    def available(self) -> bool:
        return bool(self.binary and Path(self.binary).exists())

    def start(self, timeout: float = 900.0) -> None:
        """Spawn the process and wait for it to become ready.

        Readiness is worth waiting for explicitly: on the first run, or whenever the oleans
        have changed, Loogle rebuilds its index before accepting anything, which takes
        minutes. Doing that here means the cost is paid once, at startup, rather than being
        mistaken for a slow first query.
        """
        if not self.available:
            raise ReplError(f"loogle binary not found at {self.binary}")
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            t0 = time.time()
            command = [
                str(self.binary),
                "--module",
                self.module,
                "-i",
                "--json",
                "--index-mode",
                self.index_mode,
            ]
            if self.index_file is not None:
                command += ["--index-file", str(self.index_file)]
            if not self.direct:
                command = [str(self.lake_bin), "env", *command]
            self.proc = subprocess.Popen(
                command,
                cwd=str(self.lean_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self.environment,
                start_new_session=True,
            )
            assert self.proc.stdout is not None and self.proc.stderr is not None
            self._lines = queue.Queue()
            self._stderr = deque(maxlen=20)
            threading.Thread(
                target=self._pump, args=(self.proc.stdout, self._lines), daemon=True
            ).start()
            threading.Thread(
                target=self._pump_errors, args=(self.proc.stderr, self._stderr), daemon=True
            ).start()
            try:
                banner = self._readline(timeout)
                if banner.strip() != "Loogle is ready.":
                    raise ReplError(f"unexpected Loogle startup response: {banner[:200]}")
            except ReplError:
                self.close()
                raise
            self.startup_seconds = time.time() - t0

    def query(self, q: str, limit: int = 12, timeout: float = 900.0) -> SearchResult:
        with self._lock:
            try:
                self.start(timeout=timeout)
                assert self.proc is not None and self.proc.stdin
                # One query per line, one JSON object per line back.
                self.proc.stdin.write(q.replace("\n", " ") + "\n")
                self.proc.stdin.flush()
                line = self._readline(timeout)
            except (ReplError, OSError) as exc:
                self.close()
                return SearchResult(q, "loogle-local", error=f"loogle: {exc}")
        if not line.strip():
            return SearchResult(q, "loogle-local", error="loogle returned nothing")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return SearchResult(q, "loogle-local", error=f"loogle: unparsable reply {line[:120]!r}")
        return _parse_loogle_json(q, raw, limit, "loogle-local")

    def close(self) -> None:
        with self._lock:
            if self.proc is None:
                return
            try:
                if self.proc.poll() is None:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
            except ProcessLookupError:
                self.proc.wait()
            finally:
                for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    if stream:
                        stream.close()
                self.proc = None


_local_loogle: LoogleSession | None = None
_local_lock = threading.Lock()
_configured_sessions: dict[tuple[str, ...], LoogleSession] = {}


def local_loogle_session(config: Config | None = None) -> LoogleSession:
    """The process-wide local Loogle session, created on first use."""
    global _local_loogle
    with _local_lock:
        if config is not None:
            key = (
                str(config.lean_dir.resolve()),
                str(config.loogle_bin),
                str(config.lake_bin),
                config.loogle_module,
            )
            if key not in _configured_sessions:
                _configured_sessions[key] = LoogleSession(
                    binary=config.loogle_bin,
                    lean_dir=config.lean_dir,
                    lake_bin=config.lake_bin,
                    module=config.loogle_module,
                )
            return _configured_sessions[key]
        if _local_loogle is None:
            _local_loogle = LoogleSession()
        return _local_loogle


def close_sessions(config: Config | None = None) -> None:
    """Close existing search processes without creating new sessions."""
    global _local_loogle
    with _local_lock:
        if _local_loogle is not None and (
            config is None
            or (
                _local_loogle.lean_dir.resolve() == config.lean_dir.resolve()
                and _local_loogle.binary == config.loogle_bin
                and _local_loogle.module == config.loogle_module
                and _local_loogle.lake_bin == config.lake_bin
            )
        ):
            _local_loogle.close()
            _local_loogle = None
        for key in list(_configured_sessions):
            if config is None or key == (
                str(config.lean_dir.resolve()),
                str(config.loogle_bin),
                str(config.lake_bin),
                config.loogle_module,
            ):
                _configured_sessions.pop(key).close()
    from .ledger_search import close_ledger_sessions

    close_ledger_sessions(config)


def loogle_remote(query: str, limit: int = 12, timeout: float = 20.0) -> SearchResult:
    """Shape-directed search against the hosted service at loogle.lean-lang.org.

    Only used when asked for by name. Its index is a Mathlib revision that is not
    necessarily ours and contains no Physlib, so a hit may name a lemma this verifier does
    not have, and a miss does not mean the lemma is absent here.
    """
    try:
        raw = _http_json(LOOGLE_URL, params={"q": query}, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return SearchResult(query, "loogle", error=f"HTTP {exc.code}")
    except Exception as exc:
        return SearchResult(query, "loogle", error=str(exc))
    return _parse_loogle_json(query, raw, limit, "loogle")


def loogle(
    query: str, limit: int = 12, timeout: float = 20.0, *, config: Config | None = None
) -> SearchResult:
    """Shape-directed search, against the local index.

    Query forms Loogle understands:
      `Nat.succ_le_succ`        - declarations mentioning this constant
      `"comm"`                  - declarations whose name contains this substring
      `(?a + ?b) * ?c`          - declarations whose statement matches this pattern
      `|- Continuous _`         - declarations whose *conclusion* matches
      `Real.sqrt, |- _ < _`     - conjunction of constraints

    This never silently uses the hosted service. The local index is built from the very
    `.olean` files a submission is checked against, so its answers agree with the verifier
    by construction; the hosted one indexes a different Mathlib revision and no Physlib at
    all. Quietly swapping the second for the first would make "not found" ambiguous between
    "no such lemma" and "wrong library" — and an agent reads the first meaning and goes back
    to guessing names. If the local index is unavailable, that is reported, and the hosted
    service remains available by asking for it: `loogle_remote`, or `backend="loogle-remote"`.
    """
    session = (
        None if config is not None and config.loogle_bin is None else local_loogle_session(config)
    )
    if session is None or not session.available:
        return SearchResult(
            query,
            "loogle-local",
            error=(
                "no local Loogle index. Build one with `scripts/build-loogle.sh` (~15 s, plus "
                "a few minutes for the first index), or search the hosted service explicitly "
                "with backend='loogle-remote' — noting that it indexes a different Mathlib "
                "revision and does not cover Physlib."
            ),
        )
    return session.query(query, limit=limit)


def leansearch(query: str, limit: int = 8, timeout: float = 25.0) -> SearchResult:
    """Natural-language semantic search over Mathlib."""
    try:
        raw = _http_json(
            LEANSEARCH_URL, data={"query": [query], "num_results": limit}, timeout=timeout
        )
    except urllib.error.HTTPError as exc:
        return SearchResult(query, "leansearch", error=f"HTTP {exc.code}")
    except Exception as exc:
        return SearchResult(query, "leansearch", error=str(exc))

    # The endpoint returns a list (one entry per query) of lists of results.
    batch = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], list) else raw
    hits = []
    for item in (batch or [])[:limit]:
        r = item.get("result", item) if isinstance(item, dict) else {}
        name = r.get("name")
        if isinstance(name, list):
            name = ".".join(name)
        module = r.get("module_name")
        if isinstance(module, list):
            module = ".".join(module)
        hits.append(
            Hit(
                name=str(name or ""),
                kind=str(r.get("kind", "") or ""),
                module=str(module or ""),
                signature=str(r.get("signature", "") or "").strip(),
                doc=str(r.get("informal_description") or r.get("docstring") or ""),
                source="leansearch",
            )
        )
    return SearchResult(query, "leansearch", hits)


LOCAL_TACTICS = ("exact?", "apply?", "rw?", "hint")

# `exact?` answers with `Try this:\n  [apply] exact Nat.add_eq_left.mpr rfl`, i.e. the
# suggestion sits on the line *after* the header and carries a bracketed tag.
_TRY_HEADER = "Try this:"
_TAG = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?")


def _parse_suggestions(text: str) -> list[str]:
    """Pull concrete tactic suggestions out of a `Try this:` block."""
    out: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(_TRY_HEADER):
            rest = stripped[len(_TRY_HEADER) :].strip()
            candidates = [rest] if rest else []
            # Everything indented under the header belongs to this suggestion block.
            for follow in lines[i + 1 :]:
                if not follow.strip():
                    break
                if not follow.startswith((" ", "\t")) and follow.strip().endswith(":"):
                    break
                candidates.append(follow.strip())
            for c in candidates:
                c = _TAG.sub("", c).strip()
                if c:
                    out.append(c)
        elif stripped.startswith(("exact ", "apply ", "refine ", "rw [")) and not out:
            out.append(stripped)
    # Preserve order while removing duplicates.
    seen: set[str] = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def local_search(
    session: Session,
    goal: str,
    tactics: tuple[str, ...] = ("exact?", "apply?"),
    binders: str = "",
    timeout: float | None = 90.0,
) -> SearchResult:
    """Ask Lean itself what closes `goal`.

    `goal` is a proposition, e.g. `n + 0 = n`; `binders` supplies any context it needs, e.g.
    `(n : Nat)`. Unlike the HTTP backends this cannot hallucinate: `exact?` reports a lemma
    only if it genuinely applies to the goal.
    """
    hits: list[Hit] = []
    errors: list[str] = []
    for tac in tactics:
        if tac not in LOCAL_TACTICS:
            errors.append(f"{tac}: not an allowed search tactic")
            continue
        src = f"example {binders} : {goal} := by {tac}"
        try:
            resp = session.run(src, timeout=timeout)
        except ReplError as exc:
            errors.append(f"{tac}: {exc}")
            continue
        for msg in resp.infos + resp.warnings:
            for suggestion in _parse_suggestions(str(msg)):
                hits.append(Hit(name=suggestion, source=tac))
        if resp.errors:
            errors.append(f"{tac}: {'; '.join(resp.errors)[:200]}")

    seen: set[str] = set()
    unique = [h for h in hits if not (h.name in seen or seen.add(h.name))]
    return SearchResult(
        goal,
        "local",
        unique,
        error="; ".join(errors) if errors and not unique else None,
        note="verified by Lean against the actual goal" if unique else "",
    )


BACKENDS = ("loogle", "loogle-remote", "leansearch", "both", "ledger")


def search(
    query: str,
    limit: int = 10,
    timeout: float = 20.0,
    backend: str = "both",
    *,
    include_ledger: bool = False,
    refresh_ledger: bool = False,
    config: Config | None = None,
) -> list[SearchResult]:
    """Dispatch a query to the requested backend(s).

    `both` means local shape search plus natural-language search: a natural-language query
    rarely works on Loogle and a pattern rarely works on LeanSearch, so running both and
    letting the caller pick beats guessing which one the query was meant for. The hosted
    Loogle is never included implicitly — it has to be named.

    Shared by the CLI, the MCP server and the harness so that "which backend answers" cannot
    drift between them.
    """
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; expected one of {', '.join(BACKENDS)}")
    if not isinstance(include_ledger, bool) or not isinstance(refresh_ledger, bool):
        raise ValueError("include_ledger and refresh_ledger must be booleans")
    if include_ledger and backend in ("loogle-remote", "leansearch"):
        raise ValueError("include_ledger requires a local backend: loogle, both, or ledger")
    if refresh_ledger and not (include_ledger or backend == "ledger"):
        raise ValueError("refresh_ledger requires include_ledger or backend='ledger'")
    if limit < 1:
        raise ValueError("limit must be positive")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    out: list[SearchResult] = []
    if backend in ("loogle", "both"):
        out.append(loogle(query, limit=limit, timeout=timeout, config=config))
    if backend == "loogle-remote":
        out.append(loogle_remote(query, limit=limit, timeout=timeout))
    if backend in ("leansearch", "both"):
        out.append(leansearch(query, limit=limit, timeout=timeout))
    if include_ledger or backend == "ledger":
        from .ledger_search import search_ledger

        out.append(
            search_ledger(
                query, limit=limit, timeout=timeout, refresh=refresh_ledger, config=config
            )
        )
    return out

"""HTTP service, for harnesses that are not written in Python.

    python3 -m nullius.http_server --port 823 --pool 4

Endpoints (all JSON):

    GET  /health              readiness and provenance
    POST /verify              {"source": ..., "claim": ..., "require_nontrivial": false}
    POST /verify_many         {"items": [{"source": ...}, ...]}
    POST /statement           {"statement": "(n : Nat) (h : 5 < n) : 25 < n * n"}
    POST /search              {"query": ..., "backend": "both"}
    POST /find_proof          {"goal": ..., "binders": ...}
    GET  /ledger?limit=20     recent verdicts
    GET  /ledger/123          one entry, including source and target (read-only)
    GET  /ledger?ref=UUID:ID  a local, imported or linked origin (read-only)

Configure a registered, trusted project with --project. POST /verify can then use
{"module": "Demo", "target": "Demo.saved", "build": false} instead of source.
POST bodies and ledger GET queries may select another registered project with `project`.
Per-request selection retains the server's --no-log policy.

Binds to localhost by default. There is no authentication, and submissions run arbitrary
elaboration in Lean, so do not expose this to an untrusted network.
"""

from __future__ import annotations

import argparse
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlparse

from .config import Config, ConfigError
from .harness import Harness, filter_project_rows, project_history_stats
from .ledger import LedgerReadError, read_ledger_row
from .ledger_transfer import read_entry_reference, read_project_rows

_harness: Harness | None = None

# One harness per selected project, reusing the shared pool. A `Harness` holds no connection
# of its own — `Ledger` opens one per operation — so reusing it across request threads costs
# nothing and saves reopening the project's ledger on every call.
_project_harnesses: dict[str | None, Harness] = {}
_project_lock = threading.Lock()

MAX_BODY = 4 * 1024 * 1024


def _project_selector(body: dict[str, Any], query: dict[str, list[str]]) -> str | None:
    if any(key in body or key in query for key in ("trust", "relocate")):
        raise ValueError("register and trust projects through the CLI or Python API")
    values = query.get("project", [])
    if len(values) > 1:
        raise ValueError("project must be specified only once")
    selector = body.get("project", values[0] if values else None)
    if "project" in body or values:
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError("project must be a nonempty registered selector")
        if values and selector != values[0]:
            raise ValueError("body and query project selectors disagree")
    return selector


def _request_config(selector: str | None) -> Config:
    assert _harness is not None
    cfg = _harness.config
    if selector is not None:
        cfg = cfg.for_project(selector)
    if cfg.project_id is not None and not cfg.project_trusted:
        raise ValueError("project is untrusted; register it with --trust before using this server")
    return cfg


@contextmanager
def _request_harness(selector: str | None) -> Iterator[Harness]:
    assert _harness is not None
    cfg = _request_config(selector)
    if selector is None:
        yield _harness
        return
    # Borrow the one warm pool rather than starting a second set of Lean sessions per
    # request: selecting a project swaps the ledger and the project metadata, and leaves the
    # verifier tree alone. Building a pool here instead would put no ceiling on the number of
    # live Lean processes, since each request would bring its own.
    with _project_lock:
        selected = _project_harnesses.get(cfg.project_id)
        if selected is None:
            selected = Harness(
                config=cfg,
                pool=_harness.pool,
                log=_harness.ledger is not None,
                tag=_harness.tag,
            )
            _project_harnesses[cfg.project_id] = selected
    yield selected


class Handler(BaseHTTPRequestHandler):
    server_version = "nullius/0.1"

    # `format` shadows the builtin, but the name is fixed by the base class: callers in
    # http.server pass it positionally, and a rename breaks anyone who passes it by keyword.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(format, *args)

    # -- helpers -----------------------------------------------------------

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("request body too large")
        body = json.loads(self.rfile.read(length).decode())
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._get()
        except (TypeError, ValueError, ConfigError) as exc:
            self._send(400, {"error": str(exc)})
        except LedgerReadError as exc:
            self._send(500, {"error": str(exc), "type": type(exc).__name__})

    def _get(self) -> None:
        assert _harness is not None
        url = urlparse(self.path)
        q = parse_qs(url.query, keep_blank_values=True)
        selector = _project_selector({}, q)
        if url.path == "/health":
            if selector is not None:
                raise ValueError("project selection on GET is supported only for ledger reads")
            self._send(
                200,
                {
                    "ok": True,
                    "provenance": _harness.provenance(),
                    "pool_size": _harness.pool.size,
                },
            )
            return
        if url.path != "/ledger" and not url.path.startswith("/ledger/"):
            self._send(404, {"error": f"no such endpoint: {url.path}"})
            return

        reference = (q.get("ref") or [None])[0]
        raw_id = None
        if url.path.startswith("/ledger/ref/"):
            if "ref" in q:
                raise ValueError("ref must be specified only once")
            reference = unquote(url.path.removeprefix("/ledger/ref/"))
        elif url.path.startswith("/ledger/"):
            raw_id = url.path.removeprefix("/ledger/")
            if reference is not None:
                raise ValueError("ledger ID and ref are mutually exclusive")
            if not raw_id.isascii() or not raw_id.isdigit() or int(raw_id) <= 0:
                raise ValueError("ledger ID must be a positive integer")
        if len(q.get("ref", [])) > 1:
            raise ValueError("ref must be specified only once")
        single = reference is not None or raw_id is not None
        if single and any(key in q for key in ("limit", "status", "tag", "recall", "stats", "id")):
            raise ValueError("id/ref cannot be combined with listing options")
        if _harness.ledger is None:
            if single or selector is not None or _harness.config.project_id is not None:
                raise ValueError("ledger access is disabled (Harness(log=False))")
            self._send(200, {"rows": [], "stats": None})
            return
        cfg = _request_config(selector)
        if single:
            if reference is not None:
                row = read_entry_reference(cfg.ledger_path, reference)
                label = reference
            else:
                assert raw_id is not None
                row = read_ledger_row(cfg.ledger_path, int(raw_id))
                label = f"#{raw_id}"
            if row is None:
                self._send(404, {"error": f"ledger entry {label} not found"})
            else:
                self._send(200, row)
            return
        if "id" in q:
            raise ValueError("use /ledger/<id> to retrieve a local entry")
        limit = int((q.get("limit") or ["20"])[0])
        status = (q.get("status") or [None])[0]
        tag = (q.get("tag") or [None])[0]
        recall = (q.get("recall") or [None])[0]
        if cfg.project_id is not None:
            history = read_project_rows(cfg.ledger_path, verified_only=False)
            rows = filter_project_rows(history, limit=limit, status=status, tag=tag, recall=recall)
            stats = project_history_stats(history, cfg.ledger_path)
        else:
            if recall is not None:
                raise ValueError("HTTP recall listing requires a project")
            options: dict[str, Any] = {"limit": limit, "status": status}
            if tag is not None:
                options["tag"] = tag
            rows = _harness.ledger.recent(**options)
            stats = _harness.ledger.stats()
        self._send(200, {"rows": rows, "stats": stats})

    def do_POST(self) -> None:  # noqa: N802
        assert _harness is not None
        url = urlparse(self.path)
        try:
            body = self._body()
        except (ValueError, UnicodeError) as exc:
            self._send(400, {"error": f"bad request body: {exc}"})
            return

        try:
            selector = _project_selector(body, parse_qs(url.query, keep_blank_values=True))
            with _request_harness(selector) as selected:
                code, payload = self._post(url.path, body, selected)
            self._send(code, payload)
        except (TypeError, ValueError, ConfigError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": str(exc), "type": type(exc).__name__})

    def _post(self, path: str, body: dict[str, Any], selected: Harness) -> tuple[int, Any]:
        if path == "/verify":
            if "module" in body:
                if "source" in body or "check_vacuity" in body or "no_vacuity" in body:
                    raise ValueError("module verification cannot use source or vacuity overrides")
                if "target" not in body:
                    raise ValueError("module verification requires a target")
                for key in ("build", "require_nontrivial"):
                    if key in body and not isinstance(body[key], bool):
                        raise ValueError(f"{key} must be a boolean")
                if "derived_from" in body and not isinstance(body["derived_from"], str):
                    raise ValueError("derived_from must be a UUID:ID string")
                v = selected.verify_module(
                    body["module"],
                    body["target"],
                    claim=body.get("claim"),
                    require_nontrivial=body.get("require_nontrivial", False),
                    build=body.get("build", False),
                    timeout=body.get("timeout"),
                    tag=body.get("tag"),
                    derived_from=body.get("derived_from"),
                )
            else:
                if "build" in body or "derived_from" in body:
                    raise ValueError("build and derived_from require module verification")
                if not isinstance(body.get("source"), str) or not body["source"].strip():
                    raise ValueError("`source` must be a nonempty string")
                v = selected.verify(
                    body["source"],
                    claim=body.get("claim"),
                    target=body.get("target"),
                    require_nontrivial=bool(body.get("require_nontrivial", False)),
                    check_vacuity=bool(body.get("check_vacuity", True)),
                    timeout=body.get("timeout"),
                    tag=body.get("tag"),
                )
            out = v.to_dict()
            out["feedback"] = v.feedback()
            out["render"] = v.render()
            return 200, out

        elif path == "/verify_many":
            items = body.get("items") or []
            verdicts = selected.verify_many(items)
            return 200, {"results": [{**v.to_dict(), "feedback": v.feedback()} for v in verdicts]}

        elif path == "/statement":
            return 200, selected.check_statement(body.get("statement", ""))

        elif path == "/search":
            results = selected.search(
                body.get("query", ""),
                backend=body.get("backend", "both"),
                limit=int(body.get("limit", 8)),
                include_ledger=body.get("include_ledger", False),
                refresh_ledger=body.get("refresh_ledger", False),
            )
            return 200, {"results": [r.to_dict() for r in results]}

        elif path == "/find_proof":
            res = selected.find_proof(
                body.get("goal", ""),
                binders=body.get("binders", ""),
                tactics=tuple(body.get("tactics") or ("exact?", "apply?")),
            )
            return 200, res.to_dict()

        else:
            return 404, {"error": f"no such endpoint: {path}"}


def main(argv: list[str] | None = None) -> int:
    global _harness
    p = argparse.ArgumentParser(
        prog="nullius.http_server",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=823)
    p.add_argument("--pool", type=int, default=2, help="concurrent Lean sessions")
    p.add_argument("--no-log", action="store_true", help="do not write to the ledger")
    p.add_argument("--project", help="registered project name, UUID or root")
    p.add_argument("--no-warm", action="store_true", help="start sessions lazily")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    options: dict[str, Any] = {"project": args.project} if args.project is not None else {}
    try:
        _harness = Harness(pool_size=args.pool, log=not args.no_log, tag="http", **options)
        _request_config(None)
    except (ValueError, ConfigError, LedgerReadError) as exc:
        p.exit(2, f"error: {exc}\n")
    if not args.no_warm:
        print(f"warming {args.pool} Lean session(s) ...", flush=True)
        _harness.warm()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.verbose = args.verbose  # type: ignore[attr-defined]
    prov = _harness.provenance()
    print(
        f"nullius listening on http://{args.host}:{args.port}  "
        f"({prov['toolchain']}, mathlib {prov['mathlib_rev'][:12]})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        # Project harnesses borrow the shared pool, so closing them only releases their own
        # search sessions; the pool itself goes with the harness that owns it.
        for selected in _project_harnesses.values():
            selected.close()
        _harness.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

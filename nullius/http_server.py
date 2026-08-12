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

Binds to localhost by default. There is no authentication, and submissions run arbitrary
elaboration in Lean, so do not expose this to an untrusted network.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .harness import Harness

_harness: Harness | None = None

MAX_BODY = 4 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "nullius/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

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
        return json.loads(self.rfile.read(length).decode())

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        assert _harness is not None
        url = urlparse(self.path)
        if url.path == "/health":
            self._send(
                200,
                {
                    "ok": True,
                    "provenance": _harness.provenance(),
                    "pool_size": _harness.pool.size,
                },
            )
        elif url.path == "/ledger":
            q = parse_qs(url.query)
            limit = int((q.get("limit") or ["20"])[0])
            status = (q.get("status") or [None])[0]
            lg = _harness.ledger
            self._send(
                200,
                {"rows": lg.recent(limit=limit, status=status) if lg else [], "stats":
                 lg.stats() if lg else None},
            )
        else:
            self._send(404, {"error": f"no such endpoint: {url.path}"})

    def do_POST(self) -> None:  # noqa: N802
        assert _harness is not None
        path = urlparse(self.path).path
        try:
            body = self._body()
        except Exception as exc:
            self._send(400, {"error": f"bad request body: {exc}"})
            return

        try:
            if path == "/verify":
                if not body.get("source"):
                    self._send(400, {"error": "`source` is required"})
                    return
                v = _harness.verify(
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
                self._send(200, out)

            elif path == "/verify_many":
                items = body.get("items") or []
                verdicts = _harness.verify_many(items)
                self._send(
                    200,
                    {
                        "results": [
                            {**v.to_dict(), "feedback": v.feedback()} for v in verdicts
                        ]
                    },
                )

            elif path == "/statement":
                self._send(200, _harness.check_statement(body.get("statement", "")))

            elif path == "/search":
                results = _harness.search(
                    body.get("query", ""),
                    backend=body.get("backend", "both"),
                    limit=int(body.get("limit", 8)),
                )
                self._send(200, {"results": [r.to_dict() for r in results]})

            elif path == "/find_proof":
                res = _harness.find_proof(
                    body.get("goal", ""),
                    binders=body.get("binders", ""),
                    tactics=tuple(body.get("tactics") or ("exact?", "apply?")),
                )
                self._send(200, res.to_dict())

            else:
                self._send(404, {"error": f"no such endpoint: {path}"})
        except Exception as exc:
            self._send(500, {"error": str(exc), "type": type(exc).__name__})


def main(argv: list[str] | None = None) -> int:
    global _harness
    p = argparse.ArgumentParser(prog="nullius.http_server", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=823)
    p.add_argument("--pool", type=int, default=2, help="concurrent Lean sessions")
    p.add_argument("--no-log", action="store_true", help="do not write to the ledger")
    p.add_argument("--no-warm", action="store_true", help="start sessions lazily")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    _harness = Harness(pool_size=args.pool, log=not args.no_log, tag="http")
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
        _harness.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

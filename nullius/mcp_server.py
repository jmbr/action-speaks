"""MCP server exposing the verifier to agents.

Speaks the Model Context Protocol over stdio using nothing but the standard library, so it
can be dropped into any MCP-capable client without installing a framework.

Tools offered, in the order an agent normally needs them:

  search     find the Mathlib lemma you need (by meaning or by shape)
  close      ask Lean which lemma or tactic closes a specific goal
  statement  elaborate a statement *before* proving it, and find out whether its hypotheses
             are contradictory (in which case any proof is worthless)
  verify     the main event: check a proof and return a verdict
  log        look up past verdicts

The names match the CLI subcommands exactly, so `nullius search` and the `search` tool are
the same operation, and a workflow written for one transfers to the other.

Design note: the server keeps one warm Lean session per process. The first call pays the
Mathlib import (a few seconds); everything after that is milliseconds.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from typing import Any, Callable

from . import search as S
from .config import Config
from .ledger import Ledger
from .repl import ReplError, Session
from .verify import Verifier

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "nullius", "version": "0.1.0"}

# Reentrant: `session()` holds this lock while calling `config()`, which takes it again.
_state_lock = threading.RLock()
_session: Session | None = None
_config: Config | None = None
_ledger: Ledger | None = None


def config() -> Config:
    global _config
    with _state_lock:
        if _config is None:
            _config = Config.discover()
        return _config


def ledger() -> Ledger:
    global _ledger
    with _state_lock:
        if _ledger is None:
            _ledger = Ledger(config().ledger_path)
        return _ledger


def session() -> Session:
    global _session
    with _state_lock:
        if _session is None:
            _session = Session(config())
        _session.start()
        return _session


def verifier() -> Verifier:
    return Verifier(session(), config())


# --- tools ---------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "verify",
        "description": (
            "Verify a Lean 4 + Mathlib proof and return a trustworthy verdict. Use this "
            "whenever you assert a mathematical fact you want to be believed. The proof is "
            "rejected unless it (a) compiles with no errors, (b) contains no `sorry`, "
            "(c) depends only on Mathlib's standard axioms, (d) has non-contradictory "
            "hypotheses. A theorem with contradictory hypotheses is vacuously true and "
            "supports no claim, so it is rejected even though Lean accepts it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": (
                        "Complete Lean 4 source. Do NOT write `import` lines: Mathlib is "
                        "already imported. Declare helper lemmas first and the main theorem "
                        "last."
                    ),
                },
                "claim": {
                    "type": "string",
                    "description": (
                        "The informal statement in English that this proof is meant to "
                        "support. Recorded for review; compare it against the elaborated "
                        "statement returned in the result."
                    ),
                },
                "target": {
                    "type": "string",
                    "description": "Declaration to audit. Defaults to the last theorem.",
                },
                "require_nontrivial": {
                    "type": "boolean",
                    "description": (
                        "If true, reject proofs whose hypotheses are never needed, which "
                        "usually means the formalisation is weaker than the claim."
                    ),
                    "default": False,
                },
                "tag": {
                    "type": "string",
                    "description": (
                        "Optional label recorded with this verdict, so a group of related "
                        "checks can be retrieved later with `log`. Useful for "
                        "collecting every result belonging to one paper or project."
                    ),
                },
            },
            "required": ["source"],
        },
    },
    {
        "name": "statement",
        "description": (
            "Elaborate a Lean statement WITHOUT proving it, to confirm it type-checks and "
            "says what you intend. Also reports whether its hypotheses are contradictory. "
            "Call this before investing effort in a proof: if the hypotheses are "
            "contradictory, any proof you produce will be rejected as vacuous."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "statement": {
                    "type": "string",
                    "description": (
                        "Binders and conclusion, without the theorem name, e.g. "
                        "'(n : Nat) (h : 5 < n) : 25 < n * n'"
                    ),
                }
            },
            "required": ["statement"],
        },
    },
    {
        "name": "search",
        "description": (
            "Search Mathlib for a lemma. Two backends: 'leansearch' takes natural language "
            "('sum of two even numbers is even'); 'loogle' takes a shape ('|- Irrational "
            "(Real.sqrt _)', '(?a + ?b) * ?c', 'Nat.succ_le_succ'). Use this instead of "
            "guessing lemma names - invented names are the most common cause of failed "
            "proofs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "backend": {
                    "type": "string",
                    "enum": ["loogle", "leansearch", "both"],
                    "default": "both",
                },
                "limit": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "close",
        "description": (
            "Find a lemma or single tactic that closes a Lean goal, by running "
            "`exact?`/`apply?` in the real environment. Slower than `search`, but "
            "authoritative: anything it returns genuinely closes the goal, so unlike a "
            "name-based search it cannot hallucinate. Use it whenever you would otherwise "
            "guess a lemma name. It finishes a goal in one step; it is not a general "
            "prover, so decompose a hard goal first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "e.g. '25 < n * n'"},
                "binders": {
                    "type": "string",
                    "description": "context the goal needs, e.g. '(n : Nat) (h : 5 < n)'",
                    "default": "",
                },
                "tactics": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["exact?", "apply?", "rw?", "hint"]},
                    "default": ["exact?", "apply?"],
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "log",
        "description": "List past verification results recorded by this verifier.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
                "status": {"type": "string", "enum": ["verified", "rejected", "error"]},
                "tag": {
                    "type": "string",
                    "description": "Only entries recorded with this tag by `verify`.",
                },
                "stats": {"type": "boolean", "default": False},
            },
        },
    },
]


def tool_verify(args: dict[str, Any]) -> str:
    source = args.get("source", "")
    if not source.strip():
        return "error: `source` is empty"
    verdict = verifier().verify(
        source,
        target=args.get("target"),
        claim=args.get("claim"),
        require_nontrivial=bool(args.get("require_nontrivial", False)),
    )
    row = ledger().record(verdict, source, tag=args.get("tag") or "mcp")
    out = [verdict.render(), f"\nledger entry: #{row}"]
    if verdict.verified:
        out.append(
            "\nThis proof is machine-checked. You may cite it, but state the ELABORATED "
            "statement above rather than a looser paraphrase."
        )
    else:
        out.append(
            "\nNOT verified. Do not present this claim as proved. Fix the issues above and "
            "resubmit."
        )
    return "\n".join(out)


def tool_statement(args: dict[str, Any]) -> str:
    res = verifier().check_statement(args.get("statement", ""))
    if not res.get("ok"):
        return (
            f"statement does not elaborate ({res.get('error')}):\n"
            f"{res.get('messages') or res.get('violations') or ''}"
        )
    lines = [f"elaborates, {res['binders']} binder(s):", f"  {res['statement']}"]
    if res["vacuous"]:
        lines.append(
            "\nWARNING: hypotheses are CONTRADICTORY "
            f"(witness: {res['vacuity_witness']}). Anything follows from them, so a proof "
            "of this statement would be vacuous and will be rejected. Restate the claim."
        )
    else:
        lines.append("\nhypotheses are satisfiable as far as the prover can tell.")
    for g in res.get("open_goals") or []:
        lines.append(f"\ngoal to prove:\n{g}")
    return "\n".join(lines)


def tool_search(args: dict[str, Any]) -> str:
    query = args.get("query", "")
    backend = args.get("backend", "both")
    limit = int(args.get("limit", 8))
    out = []
    if backend in ("loogle", "both"):
        out.append(S.loogle(query, limit=limit).render(limit))
    if backend in ("leansearch", "both"):
        out.append(S.leansearch(query, limit=limit).render(limit))
    return "\n\n".join(out) or "no results"


def tool_close(args: dict[str, Any]) -> str:
    tactics = tuple(args.get("tactics") or ("exact?", "apply?"))
    res = S.local_search(
        session(), args.get("goal", ""), tactics=tactics, binders=args.get("binders", "")
    )
    return res.render()


def tool_log(args: dict[str, Any]) -> str:
    lg = ledger()
    if args.get("stats"):
        return json.dumps(lg.stats(), indent=2)
    rows = lg.recent(
        limit=int(args.get("limit", 10)), status=args.get("status"), tag=args.get("tag")
    )
    if not rows:
        return "ledger is empty"
    lines = []
    for r in rows:
        mark = "verified" if r["verified"] else r["status"]
        lines.append(f"#{r['id']} [{mark}] {r['created_iso']} {r['target'] or '-'}")
        if r["claim"]:
            lines.append(f"    claim: {r['claim']}")
        fails = json.loads(r["failures"])
        if fails:
            lines.append(f"    failed: {', '.join(fails)}")
    return "\n".join(lines)


HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "verify": tool_verify,
    "statement": tool_statement,
    "search": tool_search,
    "close": tool_close,
    "log": tool_log,
}


# --- JSON-RPC plumbing ---------------------------------------------------


def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(req: dict[str, Any]) -> dict[str, Any] | None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return _result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return _error(req_id, -32601, f"unknown tool: {name}")
        try:
            text = fn(args)
            is_error = False
        except ReplError as exc:
            text = f"Lean backend error: {exc}"
            is_error = True
        except Exception:
            text = f"tool failed:\n{traceback.format_exc(limit=3)}"
            is_error = True
        return _result(
            req_id, {"content": [{"type": "text", "text": text}], "isError": is_error}
        )
    if method == "shutdown":
        return _result(req_id, {})
    return _error(req_id, -32601, f"unknown method: {method}")


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = handle(req)
        except Exception:
            resp = _error(req.get("id"), -32603, traceback.format_exc(limit=3))
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    if _session is not None:
        _session.close()
    # The local Loogle process, if one was started, holds a large index in memory.
    S.local_loogle_session().close()
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())

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

from . import daemon as D
from . import search as S
from .config import Config
from .harness import (
    filter_project_rows,
    project_history_stats,
    validate_derived_from,
    validate_module_request,
)
from .ledger import Ledger, LedgerReadError, Recollection, read_ledger_row
from .ledger_transfer import read_entry_reference, read_project_rows
from .project_check import verify_project
from .repl import ReplError, Session
from .verify import Verdict, Verifier

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


# --- daemon routing -------------------------------------------------------
#
# Each MCP client starts its own server process, so without this every agent on the machine
# holds a separate Lean session: measured at ~7.6 GB resident, about half of it private once
# the memory-mapped `.olean` files are shared. Sending the work to `nullius serve` instead
# means one warm pool for all of them, and no import cost per agent.
#
# These helpers must be consulted *before* `session()`, or the server starts the very process
# the daemon exists to avoid.


def _remote(cfg: Config) -> Any:
    """The daemon to use for this request, or None to work in this process."""
    return D.available(cfg)


def _project_of(args: dict[str, Any]) -> dict[str, Any]:
    return {"project": args["project"]} if "project" in args else {}


def verify_source(cfg: Config, args: dict[str, Any], source: str) -> Verdict:
    """Verify a snippet, through the daemon when one is listening."""
    request = {
        "source": source,
        "target": args.get("target"),
        "claim": args.get("claim"),
        "require_nontrivial": args.get("require_nontrivial", False),
        **_project_of(args),
    }
    addr = _remote(cfg)
    if addr is not None:
        try:
            return Verdict.from_dict(D.call("/verify", request, address=addr))
        except D.DaemonUnreachable:
            # Stopped between the probe and the call. A refusal is different and is left to
            # propagate: repeating it here would hide the reason behind a second attempt.
            pass
    vf = Verifier(session(), cfg)
    return vf.verify(
        source,
        target=args.get("target"),
        claim=args.get("claim"),
        require_nontrivial=args.get("require_nontrivial", False),
    )


def check_statement(cfg: Config, statement: str) -> dict[str, Any]:
    addr = _remote(cfg)
    if addr is not None:
        try:
            return D.call("/statement", {"statement": statement}, address=addr)
        except D.DaemonUnreachable:
            pass
    return Verifier(session(), cfg).check_statement(statement)


def find_proof(cfg: Config, goal: str, binders: str, tactics: tuple[str, ...]) -> S.SearchResult:
    addr = _remote(cfg)
    if addr is not None:
        try:
            return S.SearchResult.from_dict(
                D.call(
                    "/find_proof",
                    {"goal": goal, "binders": binders, "tactics": list(tactics)},
                    address=addr,
                )
            )
        except D.DaemonUnreachable:
            pass
    return S.local_search(session(), goal, tactics=tactics, binders=binders)


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
            "supports no claim, so it is rejected even though Lean accepts it. "
            "For a registered, trusted project, supply project, module and target instead "
            "of source to audit its current checkout. Builds require explicit build: true."
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
                "project": {
                    "type": "string",
                    "description": (
                        "Registered project name, UUID or root; never registers or trusts."
                    ),
                },
                "module": {
                    "type": "string",
                    "description": "Current project module to audit instead of source.",
                },
                "build": {
                    "type": "boolean",
                    "description": "Explicitly build the project module; omitted means no build.",
                },
                "derived_from": {
                    "type": "string",
                    "description": "Prior UUID:ID reference in the project's ledger or links.",
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
            "anyOf": [
                {
                    "required": ["source"],
                    "not": {
                        "anyOf": [
                            {"required": ["module"]},
                            {"required": ["build"]},
                            {"required": ["derived_from"]},
                        ]
                    },
                },
                {
                    "required": ["project", "module", "target"],
                    "not": {"required": ["source"]},
                },
            ],
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
            "Search for a lemma by name, shape or meaning. Backends: 'loogle' (default, "
            "with 'leansearch') searches a local index built from the very Mathlib, "
            "Physlib and Cslib this verifier checks against, so its answers agree with what "
            "you can actually cite; 'leansearch' takes natural language ('sum of two even "
            "numbers is even'); 'loogle' takes a shape ('|- Irrational (Real.sqrt _)', "
            "'(?a + ?b) * ?c', 'Nat.succ_le_succ'). Use this instead of guessing lemma "
            "names - invented names are the most common cause of failed proofs. "
            "'loogle-remote' queries the public loogle.lean-lang.org instead; ask for it "
            "only if the local index is unavailable, and treat its results with care, since "
            "it indexes a different Mathlib revision and neither Physlib nor Cslib. "
            "'ledger' searches only the optional local ledger index; include_ledger adds "
            "it to 'loogle' or 'both'. Ledger hits are pointers: retrieve their source with "
            "`log` and re-verify before citing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {"type": "string", "description": "Registered project selector."},
                "backend": {
                    "type": "string",
                    "enum": list(S.BACKENDS),
                    "default": "both",
                },
                "limit": {"type": "integer", "default": 8},
                "include_ledger": {"type": "boolean", "default": False},
                "refresh_ledger": {
                    "type": "boolean",
                    "default": False,
                    "description": "Rebuild the requested ledger index before searching.",
                },
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
        "description": (
            "List past verification results recorded by this verifier, or search them with "
            "`recall` to find work already done on a claim. Use `id` alone to retrieve one "
            "entry read-only, including its source and target, or `ref` for a UUID:ID "
            "origin. Optional `project` selects project history, including linked entries. "
            "Project records reference a module and target, not a source snapshot. "
            "A hit is a pointer to re-verify, never proof in itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Registered project selector."},
                "ref": {
                    "type": "string",
                    "description": "Retrieve a UUID:ID origin; exclusive with id/listing options.",
                },
                "id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Retrieve one entry; cannot be combined with listing options.",
                },
                "limit": {"type": "integer", "default": 10},
                "status": {"type": "string", "enum": ["verified", "rejected", "error"]},
                "tag": {
                    "type": "string",
                    "description": "Only entries recorded with this tag by `verify`.",
                },
                "recall": {
                    "type": "string",
                    "description": (
                        "Find earlier verified work resembling this text, instead of "
                        "listing recent entries."
                    ),
                },
                "stats": {"type": "boolean", "default": False},
            },
        },
    },
]


def tool_verify(args: dict[str, Any]) -> str:
    module_mode = "module" in args
    if module_mode:
        if "source" in args or "check_vacuity" in args or "no_vacuity" in args:
            raise ValueError("module verification cannot use source or vacuity overrides")
        if "project" not in args or "target" not in args:
            raise ValueError("module verification requires project, module and target")
    else:
        if "build" in args or "derived_from" in args:
            raise ValueError("build and derived_from require module verification")
        source = args.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("`source` must be a nonempty string")
    for key in ("build", "require_nontrivial"):
        if key in args and not isinstance(args[key], bool):
            raise ValueError(f"{key} must be a boolean")
    if "derived_from" in args and not isinstance(args["derived_from"], str):
        raise ValueError("derived_from must be a UUID:ID string")
    cfg = _selected_config(args)
    if module_mode:
        validate_module_request(cfg, args["module"], args["target"])
    origin = validate_derived_from(cfg, args.get("derived_from"), log=True)
    lg = ledger() if "project" not in args else Ledger(cfg.ledger_path)
    if cfg.project_id is not None:
        lg.bind_project(cfg.project_id)
    prior = []
    if module_mode:
        verdict = verify_project(
            cfg,
            args["module"],
            args["target"],
            claim=args.get("claim"),
            require_nontrivial=args.get("require_nontrivial", False),
            build=args.get("build", False),
        )
        row = lg.record(
            verdict,
            "",
            tag=args.get("tag") or "mcp",
            record_kind="project",
            project_id=cfg.project_id,
            module=args["module"],
            environment_id=verdict.provenance.get("environment_id"),
            derived_from=origin,
        )
    else:
        source = args["source"]
        prior = lg.recall(source=source, current=cfg.provenance())
        verdict = verify_source(cfg, args, source)
        if cfg.project_id is None:
            row = lg.record(verdict, source, tag=args.get("tag") or "mcp")
        else:
            row = lg.record(
                verdict, source, tag=args.get("tag") or "mcp", project_id=cfg.project_id
            )
    out = [verdict.render(), f"\nledger entry: #{row}"]
    if prior:
        out.append("\nseen before:\n" + "\n".join(h.render() for h in prior))
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


def _selected_config(args: dict[str, Any]) -> Config:
    cfg = config()
    if "project" in args:
        project = args["project"]
        if not isinstance(project, str) or not project.strip():
            raise ValueError("project must be a nonempty registered selector")
        cfg = cfg.for_project(project)
    return cfg


def tool_statement(args: dict[str, Any]) -> str:
    statement = args.get("statement", "")
    res = check_statement(config(), statement)
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
    # The elaborated statement is in hand here, which matches the same theorem however it
    # was previously phrased.
    hits = ledger().recall(
        statement=res.get("statement"), text=statement, current=config().provenance()
    )
    if hits:
        lines.append("\nrelated earlier work:\n" + "\n".join(h.render() for h in hits))
    return "\n".join(lines)


def tool_search(args: dict[str, Any]) -> str:
    query = args.get("query", "")
    backend = args.get("backend", "both")
    limit = int(args.get("limit", 8))
    results = S.search(
        query,
        limit=limit,
        backend=backend,
        include_ledger=args.get("include_ledger", False),
        refresh_ledger=args.get("refresh_ledger", False),
        config=_selected_config(args) if "project" in args else _config,
    )
    text = "\n\n".join(r.render(limit) for r in results) or "no results"
    if any(r.error and r.backend in ("loogle-ledger", "project-ledger") for r in results):
        raise ValueError(text)
    return text


def tool_close(args: dict[str, Any]) -> str:
    tactics = tuple(args.get("tactics") or ("exact?", "apply?"))
    res = find_proof(config(), args.get("goal", ""), args.get("binders", ""), tactics)
    return res.render()


def tool_log(args: dict[str, Any]) -> str:
    if "id" in args or "ref" in args:
        if "id" in args and "ref" in args:
            raise ValueError("id and ref are mutually exclusive")
        if any(key in args for key in ("limit", "status", "tag", "recall", "stats")):
            raise ValueError("id/ref cannot be combined with limit, status, tag, recall or stats")
        if "id" in args:
            row_id = args["id"]
            if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
                raise ValueError("ledger ID must be a positive integer")
        cfg = _selected_config(args)
        row = (
            read_entry_reference(cfg.ledger_path, args["ref"])
            if "ref" in args
            else read_ledger_row(cfg.ledger_path, args["id"])
        )
        if row is None:
            label = args["ref"] if "ref" in args else f"#{args['id']}"
            raise ValueError(f"ledger entry {label} not found")
        return json.dumps(row, indent=2, ensure_ascii=False)
    if "project" in args:
        cfg = _selected_config(args)
        history = read_project_rows(cfg.ledger_path, verified_only=False)
        if args.get("stats"):
            return json.dumps(project_history_stats(history, cfg.ledger_path), indent=2)
        rows = filter_project_rows(
            history,
            limit=args.get("limit", 10),
            status=args.get("status"),
            tag=args.get("tag"),
            recall=args.get("recall"),
        )
        if args.get("recall") is not None:
            if not rows:
                return f"nothing recorded resembling {args['recall']!r}"
            return "\n".join(Recollection(row, "text", []).render() for row in rows)
        if not rows:
            return "ledger is empty"
        return "\n".join(_render_log_rows(rows))
    lg = ledger()
    if args.get("stats"):
        return json.dumps(lg.stats(), indent=2)
    if args.get("recall"):
        hits = lg.recall(
            text=str(args["recall"]),
            limit=int(args.get("limit", 10)),
            current=config().provenance(),
        )
        if not hits:
            return f"nothing recorded resembling {args['recall']!r}"
        return "\n".join(h.render() for h in hits)
    rows = lg.recent(
        limit=int(args.get("limit", 10)), status=args.get("status"), tag=args.get("tag")
    )
    if not rows:
        return "ledger is empty"
    return "\n".join(_render_log_rows(rows))


def _render_log_rows(rows: list[dict[str, Any]]) -> list[str]:
    lines = []
    for r in rows:
        mark = "verified" if r["verified"] else r["status"]
        lines.append(f"#{r['id']} [{mark}] {r['created_iso']} {r['target'] or '-'}")
        if r.get("origin") or r.get("reference"):
            lines.append(f"    ref: {r.get('origin') or r['reference']}")
        if r.get("record_kind") == "project":
            lines.append(
                f"    module: {r.get('module')} (current checkout; no source snapshot)\n"
                "    historical verdict: reverify in the intended project before citing"
            )
        if r["claim"]:
            lines.append(f"    claim: {r['claim']}")
        fails = json.loads(r["failures"])
        if fails:
            lines.append(f"    failed: {', '.join(fails)}")
    return lines


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
        # `or ""` rather than the bare `.get`: a request with no tool name should take the
        # unknown-tool path below, not hand `None` to a lookup typed for `str`.
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return _error(req_id, -32601, f"unknown tool: {name}")
        try:
            if any(key in args for key in ("trust", "relocate")):
                raise ValueError("register and trust projects through the CLI or Python API")
            if "project" in args and name not in ("verify", "search", "log"):
                raise ValueError("project is supported only by verify, search and log")
            text = fn(args)
            is_error = False
        except ReplError as exc:
            text = f"Lean backend error: {exc}"
            is_error = True
        except (ValueError, LedgerReadError) as exc:
            text = f"error: {exc}"
            is_error = True
        except Exception:
            text = f"tool failed:\n{traceback.format_exc(limit=3)}"
            is_error = True
        return _result(req_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    if method == "shutdown":
        return _result(req_id, {})
    return _error(req_id, -32601, f"unknown method: {method}")


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
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
        return 0
    finally:
        try:
            if _session is not None:
                _session.close()
        finally:
            S.close_sessions(_config)


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())

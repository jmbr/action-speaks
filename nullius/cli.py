"""Command-line interface.

    nullius doctor                      check the installation
    nullius verify FILE                 verify a Lean file (or - for stdin)
    nullius statement 'STMT'            elaborate a statement without proving it
    nullius search QUERY                find Mathlib lemmas
    nullius close 'GOAL' -b '(n : Nat)' ask Lean which lemma closes a goal
    nullius log                         show recent verifications

Subcommand names match the MCP tool names exactly, so a workflow written against one
interface transfers unchanged to the other.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import search as S
from .config import Config, ConfigError
from .ledger import Ledger, LedgerReadError, read_ledger_row
from .repl import ReplError, Session
from .verify import Verifier


def _session(cfg: Config) -> Session:
    s = Session(cfg)
    s.start()
    return s


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = Config.discover()
    print(f"lean project : {cfg.lean_dir}")
    print(f"repl binary  : {cfg.repl_bin}")
    print(f"lake         : {cfg.lake_bin}")
    print(f"toolchain    : {cfg.toolchain()}")
    print(f"mathlib rev  : {cfg.mathlib_rev()}")
    print(f"physlib rev  : {cfg.package_rev('Physlib')}")
    print(f"cslib rev    : {cfg.package_rev('cslib')}")
    print(f"repl rev     : {cfg.package_rev('repl')}")
    if cfg.loogle_bin:
        print(f"loogle       : {cfg.loogle_bin} ({cfg.loogle_rev()[:12] or 'unknown rev'})")
    else:
        print("loogle       : not built (shape search uses the hosted service)")
    print(f"ledger       : {cfg.ledger_path}")
    try:
        cfg.validate()
    except ConfigError as exc:
        print(f"\nFAIL: {exc}")
        return 1
    print("\nstarting REPL (first run imports Mathlib, Physlib and Cslib) ...")
    t0 = time.time()
    try:
        s = _session(cfg)
    except (ReplError, ConfigError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"  ready in {time.time() - t0:.2f}s (env {s.base_env})")

    vf = Verifier(s)
    t1 = time.time()
    good = vf.verify("theorem t (n : Nat) (h : n > 5) : n * n > 25 := by nlinarith", target="t")
    print(f"  genuine proof  -> {good.status} in {time.time() - t1:.2f}s")
    bad = vf.verify("theorem t : 2 + 2 = 5 := by sorry", target="t")
    print(f"  sorry proof    -> {bad.status}")
    vac = vf.verify(
        "theorem t (n : Nat) (h1 : n > 5) (h2 : n < 3) : n = 42 := by omega", target="t"
    )
    print(f"  vacuous proof  -> {vac.status}")
    s.close()

    ok = good.verified and not bad.verified and not vac.verified
    print("\nOK: verifier is discriminating correctly." if ok else "\nFAIL: sanity checks wrong.")
    return 0 if ok else 1


def _print_recall(hits: list, header: str) -> None:
    if not hits:
        return
    print(f"\n{header}")
    for h in hits:
        print("  " + h.render().replace("\n", "\n  "))


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = Config.discover()
    src = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
    # Constructed only when logging is on: `Ledger` creates the database file, and
    # `--no-log` promises to leave it alone.
    ledger = None if args.no_log else Ledger(cfg.ledger_path)
    # Cheap, and a rejected twin carries a reason that is actionable before the retry.
    prior = ledger.recall(source=src, current=cfg.provenance()) if ledger else []
    s = _session(cfg)
    verdict = Verifier(s).verify(
        src,
        target=args.target,
        claim=args.claim,
        require_nontrivial=args.require_nontrivial,
        check_vacuity=not args.no_vacuity,
        timeout=args.timeout,
    )
    s.close()

    if ledger is not None:
        row = ledger.record(verdict, src, tag=args.tag)
        if not args.json:
            print(f"(ledger #{row})")
    if args.json:
        out = verdict.to_dict()
        out["recall"] = [h.render() for h in prior]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(verdict.render())
        _print_recall(prior, "seen before:")
    return 0 if verdict.verified else 1


def cmd_statement(args: argparse.Namespace) -> int:
    cfg = Config.discover()
    s = _session(cfg)
    res = Verifier(s).check_statement(args.statement, timeout=args.timeout)
    s.close()
    hits = []
    if res.get("ok"):
        hits = Ledger(cfg.ledger_path).recall(
            statement=res.get("statement"), text=args.statement, current=cfg.provenance()
        )
    if args.json:
        res["recall"] = [h.render() for h in hits]
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    if not res.get("ok"):
        print(f"statement does not elaborate: {res.get('error')}")
        print(res.get("messages") or res.get("violations") or "")
        return 1
    print(f"elaborates with {res['binders']} binder(s)")
    print(f"  {res['statement']}")
    if res["vacuous"]:
        print(
            "\nWARNING: the hypotheses are contradictory. Any proof of this statement would\n"
            f"be vacuous and support no claim (witness: {res['vacuity_witness']})."
        )
        _print_recall(hits, "related earlier work:")
        return 1
    for g in res.get("open_goals") or []:
        print(f"\ngoal:\n{g}")
    _print_recall(hits, "related earlier work:")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    query = " ".join(args.query)
    include_ledger = getattr(args, "include_ledger", False)
    refresh_ledger = getattr(args, "refresh_ledger", False)
    try:
        results = S.search(
            query,
            limit=args.limit,
            backend=args.backend,
            include_ledger=include_ledger,
            refresh_ledger=refresh_ledger,
        )
    finally:
        S.close_sessions()
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(r.render(limit=args.limit))
            print()
    if args.backend == "ledger" or include_ledger or refresh_ledger:
        return int(any(r.error and r.backend in ("ledger", "loogle-ledger") for r in results))
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    cfg = Config.discover()
    s = _session(cfg)
    res = S.local_search(s, args.goal, binders=args.binders, tactics=tuple(args.tactics.split(",")))
    s.close()
    print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False) if args.json else res.render())
    return 0 if res.hits else 1


def cmd_log(args: argparse.Namespace) -> int:
    row_id = getattr(args, "id", None)
    if row_id is not None:
        if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
            raise ValueError("ledger ID must be a positive integer")
        if (
            args.limit is not None
            or args.status is not None
            or args.tag is not None
            or args.recall is not None
            or args.stats
        ):
            raise ValueError(
                "--id cannot be combined with --limit, --status, --tag, --recall or --stats"
            )
    cfg = Config.discover()
    if row_id is not None:
        row = read_ledger_row(cfg.ledger_path, row_id)
        if row is None:
            print(f"ledger entry #{row_id} not found", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(row, indent=2, ensure_ascii=False))
        else:
            print(f"ledger #{row['id']} [{row['status']}]")
            print(f"target: {row['target'] or '-'}")
            print(f"source:\n{row['source']}")
        return 0
    limit = args.limit if args.limit is not None else 20
    ledger = Ledger(cfg.ledger_path)
    if args.stats:
        print(json.dumps(ledger.stats(), indent=2))
        return 0
    if args.recall:
        hits = ledger.recall(text=args.recall, limit=limit, current=cfg.provenance())
        if not hits:
            print(f"nothing recorded resembling {args.recall!r}")
            return 1
        for h in hits:
            print(h.render())
        return 0
    rows = ledger.recent(limit=limit, status=args.status, tag=args.tag)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("ledger is empty")
        return 0
    for r in rows:
        mark = "OK  " if r["verified"] else "FAIL"
        print(f"#{r['id']:<5} {mark} {r['created_iso']}  {r['target'] or '-'}")
        if r["claim"]:
            print(f"        claim: {r['claim']}")
        fails = json.loads(r["failures"])
        if fails:
            print(f"        failed: {', '.join(fails)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nullius", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check the installation end to end")
    d.set_defaults(func=cmd_doctor)

    v = sub.add_parser("verify", help="verify a Lean file")
    v.add_argument("file", help="path to a .lean file, or - for stdin")
    v.add_argument("-t", "--target", help="declaration to audit (default: last theorem)")
    v.add_argument("-c", "--claim", help="the informal claim this proof is meant to support")
    v.add_argument(
        "--require-nontrivial",
        action="store_true",
        help="reject proofs whose hypotheses are unused",
    )
    v.add_argument("--no-vacuity", action="store_true", help="skip vacuity/triviality probes")
    v.add_argument("--timeout", type=float, default=None)
    v.add_argument("--tag", help="label this entry in the ledger")
    v.add_argument("--no-log", action="store_true", help="do not write to the ledger")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_verify)

    st = sub.add_parser("statement", help="elaborate a statement without proving it")
    st.add_argument("statement", help="e.g. '(n : Nat) (h : n > 5) : n * n > 25'")
    st.add_argument("--timeout", type=float, default=None)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_statement)

    se = sub.add_parser("search", help="find Mathlib lemmas")
    se.add_argument("query", nargs="+")
    se.add_argument(
        "-b",
        "--backend",
        choices=S.BACKENDS,
        default="both",
        help="'loogle' is the local index; 'loogle-remote' is the hosted service",
    )
    se.add_argument("-n", "--limit", type=int, default=8)
    se.add_argument(
        "--include-ledger", action="store_true", help="also search the optional local ledger index"
    )
    se.add_argument(
        "--refresh-ledger", action="store_true", help="rebuild the requested ledger index"
    )
    se.add_argument("--json", action="store_true")
    se.set_defaults(func=cmd_search)

    g = sub.add_parser("close", help="ask Lean which lemma or tactic closes a goal")
    g.add_argument("goal")
    g.add_argument("-b", "--binders", default="", help="e.g. '(n : Nat) (h : 0 < n)'")
    g.add_argument("--tactics", default="exact?,apply?")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_close)

    lg = sub.add_parser("log", help="show recent verifications")
    lg.add_argument("-n", "--limit", type=int, help="maximum recent entries (default: 20)")
    lg.add_argument("--id", type=int, help="retrieve one entry, including its source, read-only")
    lg.add_argument("--status", choices=("verified", "rejected", "error"))
    lg.add_argument("--tag", help="only entries recorded with this --tag")
    lg.add_argument(
        "--recall",
        metavar="TEXT",
        help="find earlier work resembling TEXT (verified entries only)",
    )
    lg.add_argument("--stats", action="store_true")
    lg.add_argument("--json", action="store_true")
    lg.set_defaults(func=cmd_log)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except LedgerReadError as exc:
        print(f"ledger error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

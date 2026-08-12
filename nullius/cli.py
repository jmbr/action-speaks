"""Command-line interface.

    nullius doctor                     check the installation
    nullius verify FILE                verify a Lean file (or - for stdin)
    nullius statement 'STMT'           elaborate a statement without proving it
    nullius search QUERY               find Mathlib lemmas
    nullius goal 'GOAL' -b '(n : Nat)' ask Lean what closes a goal
    nullius log                        show recent verifications
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import search as S
from .config import Config, ConfigError
from .ledger import Ledger
from .repl import ReplError, Session
from .verify import Verifier, infer_target


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
    print(f"repl rev     : {cfg.package_rev('repl')}")
    print(f"ledger       : {cfg.ledger_path}")
    try:
        cfg.validate()
    except ConfigError as exc:
        print(f"\nFAIL: {exc}")
        return 1
    print("\nstarting REPL (first run imports Mathlib and Physlib) ...")
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


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = Config.discover()
    src = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
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

    if not args.no_log:
        row = Ledger(cfg.ledger_path).record(verdict, src, tag=args.tag)
        if not args.json:
            print(f"(ledger #{row})")
    print(verdict.to_json() if args.json else verdict.render())
    return 0 if verdict.verified else 1


def cmd_statement(args: argparse.Namespace) -> int:
    cfg = Config.discover()
    s = _session(cfg)
    res = Verifier(s).check_statement(args.statement, timeout=args.timeout)
    s.close()
    if args.json:
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
        return 1
    for g in res.get("open_goals") or []:
        print(f"\ngoal:\n{g}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    query = " ".join(args.query)
    results = []
    if args.backend in ("loogle", "both"):
        results.append(S.loogle(query, limit=args.limit))
    if args.backend in ("leansearch", "both"):
        results.append(S.leansearch(query, limit=args.limit))
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
        return 0
    for r in results:
        print(r.render(limit=args.limit))
        print()
    return 0


def cmd_goal(args: argparse.Namespace) -> int:
    cfg = Config.discover()
    s = _session(cfg)
    res = S.local_search(
        s, args.goal, binders=args.binders, tactics=tuple(args.tactics.split(","))
    )
    s.close()
    print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False) if args.json else res.render())
    return 0 if res.hits else 1


def cmd_log(args: argparse.Namespace) -> int:
    cfg = Config.discover()
    ledger = Ledger(cfg.ledger_path)
    if args.stats:
        print(json.dumps(ledger.stats(), indent=2))
        return 0
    rows = ledger.recent(limit=args.limit, status=args.status, tag=args.tag)
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
    p = argparse.ArgumentParser(prog="nullius", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check the installation end to end")
    d.set_defaults(func=cmd_doctor)

    v = sub.add_parser("verify", help="verify a Lean file")
    v.add_argument("file", help="path to a .lean file, or - for stdin")
    v.add_argument("-t", "--target", help="declaration to audit (default: last theorem)")
    v.add_argument("-c", "--claim", help="the informal claim this proof is meant to support")
    v.add_argument("--require-nontrivial", action="store_true",
                   help="reject proofs whose hypotheses are unused")
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
    se.add_argument("-b", "--backend", choices=("loogle", "leansearch", "both"), default="both")
    se.add_argument("-n", "--limit", type=int, default=8)
    se.add_argument("--json", action="store_true")
    se.set_defaults(func=cmd_search)

    g = sub.add_parser("goal", help="ask Lean what closes a goal")
    g.add_argument("goal")
    g.add_argument("-b", "--binders", default="", help="e.g. '(n : Nat) (h : 0 < n)'")
    g.add_argument("--tactics", default="exact?,apply?")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_goal)

    lg = sub.add_parser("log", help="show recent verifications")
    lg.add_argument("-n", "--limit", type=int, default=20)
    lg.add_argument("--status", choices=("verified", "rejected", "error"))
    lg.add_argument("--tag", help="only entries recorded with this --tag")
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
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

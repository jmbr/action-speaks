"""Command-line interface.

    nullius doctor                      check the installation
    nullius verify FILE                 verify a Lean file (or - for stdin)
    nullius statement 'STMT'            elaborate a statement without proving it
    nullius search QUERY                find Mathlib lemmas
    nullius close 'GOAL' -b '(n : Nat)' ask Lean which lemma closes a goal
    nullius log                         show recent verifications
    nullius project register PATH       register a project (execution needs --trust)
    nullius serve                       run the session daemon for this checkout

Subcommand names match the MCP tool names exactly, so a workflow written against one
interface transfers unchanged to the other.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import daemon as D
from . import http_server
from . import search as S
from .config import Config, ConfigError
from .harness import (
    filter_project_rows,
    project_history_stats,
    validate_derived_from,
    validate_module_request,
)
from .ledger import Ledger, LedgerReadError, Recollection, read_ledger_row
from .ledger_transfer import read_entry_reference, read_project_rows, transfer_entries
from .project_check import verify_project
from .projects import list_projects, register_project, rename_project, resolve_project
from .repl import ReplError, Session
from .verify import Verdict, Verifier


def _session(cfg: Config) -> Session:
    s = Session(cfg)
    s.start()
    return s


def _daemon(args: argparse.Namespace, cfg: Config) -> "D.Address | None":
    """The daemon to use for this command, or None to do the work in this process."""
    if getattr(args, "no_daemon", False):
        return None
    return D.available(cfg)


def _verify(cfg: Config, request: dict[str, Any], args: argparse.Namespace) -> Verdict:
    """Verify through the daemon when one is listening, and in process otherwise.

    The daemon holds warm Lean sessions, so this is the difference between seconds and
    milliseconds per invocation. It does the checking only: the ledger belongs to whoever
    ran the command, which is why `nullius serve` does not write to one.
    """
    addr = _daemon(args, cfg)
    if addr is not None:
        try:
            reply = D.call(
                "/verify", request, address=addr, timeout=(request.get("timeout") or 600.0) + 60
            )
            return Verdict.from_dict(reply)
        except D.DaemonUnreachable as exc:
            # Stopped between the probe and the call: redo the work here rather than fail.
            print(f"daemon unavailable ({exc}); verifying in this process", file=sys.stderr)
    s = _session(cfg)
    try:
        return Verifier(s).verify(
            request["source"],
            target=request.get("target"),
            claim=request.get("claim"),
            require_nontrivial=bool(request.get("require_nontrivial")),
            check_vacuity=bool(request.get("check_vacuity", True)),
            timeout=request.get("timeout"),
        )
    finally:
        s.close()


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
        addr = D.address(cfg)
        running = D.connectable(addr.path)
        print(f"daemon       : {'listening on' if running else 'not running;'} {addr.path}")
        if addr.warning:
            print(f"               {addr.warning}")
    except D.DaemonError as exc:
        print(f"daemon       : unavailable ({exc})")
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
    project = getattr(args, "project", None)
    module = getattr(args, "module", None)
    build = getattr(args, "build", False)
    derived_from = getattr(args, "derived_from", None)
    if module is not None:
        if not project or not args.target:
            raise ValueError("module verification requires --project, --module and --target")
        if args.file is not None or args.no_vacuity:
            raise ValueError("module verification cannot use FILE or --no-vacuity")
    else:
        if build or derived_from is not None:
            raise ValueError("--build and --derived-from require module verification")
        if args.file is None:
            raise ValueError("provide FILE, or --project, --module and --target together")
    if derived_from is not None and args.no_log:
        raise ValueError("--derived-from requires ledger access; cannot use --no-log")
    cfg = Config.discover()
    if project is not None:
        cfg = cfg.for_project(project)
    if module is not None:
        validate_module_request(cfg, module, args.target)
    origin = validate_derived_from(cfg, derived_from, log=not args.no_log)
    src = ""
    if module is None:
        assert isinstance(args.file, str)
        src = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
    # Constructed only when logging is on: `Ledger` creates the database file, and
    # `--no-log` promises to leave it alone.
    ledger = None if args.no_log else Ledger(cfg.ledger_path)
    if ledger is not None and cfg.project_id is not None:
        ledger.bind_project(cfg.project_id)
    # Cheap, and a rejected twin carries a reason that is actionable before the retry.
    prior = ledger.recall(source=src, current=cfg.provenance()) if ledger and src else []
    if module is not None:
        verdict = verify_project(
            cfg,
            module,
            args.target,
            claim=args.claim,
            require_nontrivial=args.require_nontrivial,
            build=build,
            timeout=args.timeout,
        )
    else:
        request = {
            "source": src,
            "target": args.target,
            "claim": args.claim,
            "require_nontrivial": args.require_nontrivial,
            "check_vacuity": not args.no_vacuity,
            "timeout": args.timeout,
        }
        if project is not None:
            request["project"] = project
        verdict = _verify(cfg, request, args)

    if ledger is not None:
        if module is not None:
            row = ledger.record(
                verdict,
                "",
                tag=args.tag,
                record_kind="project",
                project_id=cfg.project_id,
                module=module,
                environment_id=verdict.provenance.get("environment_id"),
                derived_from=origin,
            )
        elif cfg.project_id is not None:
            row = ledger.record(verdict, src, tag=args.tag, project_id=cfg.project_id)
        else:
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
    addr = _daemon(args, cfg)
    res = None
    if addr is not None:
        try:
            res = D.call(
                "/statement",
                {"statement": args.statement, "timeout": args.timeout},
                address=addr,
            )
        except D.DaemonUnreachable as exc:
            print(f"daemon unavailable ({exc}); checking in this process", file=sys.stderr)
    if res is None:
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
    project = getattr(args, "project", None)
    selected: dict[str, Any] = (
        {"config": Config.discover().for_project(project)} if project is not None else {}
    )
    try:
        results = S.search(
            query,
            limit=args.limit,
            backend=args.backend,
            include_ledger=include_ledger,
            refresh_ledger=refresh_ledger,
            **selected,
        )
    finally:
        if selected:
            S.close_sessions(selected["config"])
        else:
            S.close_sessions()
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(r.render(limit=args.limit))
            print()
    if args.backend == "ledger" or include_ledger or refresh_ledger:
        return int(
            any(
                r.error and r.backend in ("ledger", "loogle-ledger", "project-ledger")
                for r in results
            )
        )
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    cfg = Config.discover()
    addr = _daemon(args, cfg)
    res = None
    if addr is not None:
        try:
            res = S.SearchResult.from_dict(
                D.call(
                    "/find_proof",
                    {
                        "goal": args.goal,
                        "binders": args.binders,
                        "tactics": args.tactics.split(","),
                    },
                    address=addr,
                )
            )
        except D.DaemonUnreachable as exc:
            print(f"daemon unavailable ({exc}); searching in this process", file=sys.stderr)
    if res is None:
        s = _session(cfg)
        res = S.local_search(
            s, args.goal, binders=args.binders, tactics=tuple(args.tactics.split(","))
        )
        s.close()
    print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False) if args.json else res.render())
    return 0 if res.hits else 1


def cmd_log(args: argparse.Namespace) -> int:
    row_id = getattr(args, "id", None)
    reference = getattr(args, "ref", None)
    if row_id is not None and reference is not None:
        raise ValueError("--id and --ref are mutually exclusive")
    if row_id is not None:
        if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
            raise ValueError("ledger ID must be a positive integer")
    if row_id is not None or reference is not None:
        if (
            args.limit is not None
            or args.status is not None
            or args.tag is not None
            or args.recall is not None
            or args.stats
        ):
            raise ValueError(
                "--id/--ref cannot be combined with --limit, --status, --tag, --recall or --stats"
            )
    cfg = Config.discover()
    project = getattr(args, "project", None)
    if project is not None:
        cfg = cfg.for_project(project)
    if row_id is not None or reference is not None:
        if reference is not None:
            row = read_entry_reference(cfg.ledger_path, reference)
        else:
            assert row_id is not None
            row = read_ledger_row(cfg.ledger_path, row_id)
        if row is None:
            print(f"ledger entry {reference or f'#{row_id}'} not found", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(row, indent=2, ensure_ascii=False))
        else:
            print(f"ledger #{row['id']} [{row['status']}]")
            print(f"target: {row['target'] or '-'}")
            _print_row_reference(row)
            print(f"source:\n{row['source']}")
        return 0
    limit = args.limit if args.limit is not None else 20
    if cfg.project_id is not None:
        history = read_project_rows(cfg.ledger_path, verified_only=False)
        if args.stats:
            print(json.dumps(project_history_stats(history, cfg.ledger_path), indent=2))
            return 0
        rows = filter_project_rows(
            history, limit=limit, status=args.status, tag=args.tag, recall=args.recall
        )
        if args.recall is not None and not args.json:
            if not rows:
                print(f"nothing recorded resembling {args.recall!r}")
                return 1
            for row in rows:
                print(Recollection(row, "text", []).render())
            return 0
    else:
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
        _print_row_reference(r)
        if r["claim"]:
            print(f"        claim: {r['claim']}")
        fails = json.loads(r["failures"])
        if fails:
            print(f"        failed: {', '.join(fails)}")
    return 0


def _print_row_reference(row: dict) -> None:
    if row.get("reference") or row.get("origin"):
        print(f"        ref: {row.get('origin') or row['reference']}")
    if row.get("record_kind") == "project":
        print(
            f"        module: {row.get('module')} (current checkout; no source snapshot)\n"
            "        historical verdict: reverify in the intended project before citing"
        )


def cmd_project(args: argparse.Namespace) -> int:
    if args.project_action == "register":
        result = register_project(
            Path(args.path), args.name, trust=args.trust, relocate=args.relocate
        ).to_dict()
    elif args.project_action == "rename":
        result = rename_project(args.selector, args.name).to_dict()
    elif args.project_action == "list":
        projects = [p.to_dict() for p in list_projects()]
        if args.json:
            print(json.dumps(projects, indent=2, ensure_ascii=False))
        elif not projects:
            print("no registered projects")
        else:
            for project in projects:
                trust = "trusted" if project["trusted"] else "untrusted"
                print(f"{project['name']} [{project['id']}] {project['root']} ({trust})")
        return 0
    else:
        project = resolve_project(args.selector)
        result = transfer_entries(
            Path(args.from_ledger),
            project.ledger_path,
            ids=args.id,
            tag=args.tag,
            mode=args.project_action,
            dry_run=args.dry_run,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.project_action in ("link", "import"):
        print(f"{result['action']}: {result['count']} entries -> {result['destination']}")
        for note in result.get("notes", []):
            print(note)
    else:
        trust = "trusted" if result["trusted"] else "untrusted"
        print(f"{result['name']} [{result['id']}] {result['root']} ({trust})")
    return 0


def cmd_serve(args) -> int:
    """Run the daemon, or write the service files that run it for you."""
    cfg = Config.discover()
    try:
        addr = D.address(cfg) if not args.socket else D.Address(Path(args.socket))
    except D.DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.install or args.print_units:
        files, commands = D.install_instructions(cfg)
        if not files:
            print(commands)
            return 0
        for path, body in files.items():
            if args.print_units:
                print(f"# {path}\n{body}")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            print(f"wrote {path}")
        if not args.print_units:
            print(f"\nEnable it with:\n\n{commands}")
        return 0

    if addr.warning:
        print(f"warning: {addr.warning}", file=sys.stderr)
    # Under socket activation systemd has already bound and listened on this path, so a
    # connection succeeds and the probe below would mistake systemd for a rival daemon,
    # exit 1, and — with Restart=on-failure — loop. An inherited descriptor settles it.
    activated = D.inherited_socket() is not None
    if not activated and D.connectable(addr.path):
        print(f"a daemon is already listening on {addr.path}", file=sys.stderr)
        return 1
    # No ledger: the daemon supplies warm Lean sessions, and the caller records the verdict
    # into its own ledger with its own tag and project. Two writers would double-count.
    argv = ["--socket", str(addr.path), "--pool", str(args.pool), "--no-log"]
    if args.no_warm:
        argv.append("--no-warm")
    return http_server.main(argv)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nullius", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check the installation end to end")
    d.set_defaults(func=cmd_doctor)

    v = sub.add_parser("verify", help="verify a Lean file")
    v.add_argument("file", nargs="?", help="path to a .lean file, or - for stdin")
    v.add_argument("--project", help="registered project name, UUID or path")
    v.add_argument("--module", help="audit a module in the project's current checkout")
    v.add_argument("--build", action="store_true", help="explicitly build the selected module")
    v.add_argument("--derived-from", metavar="UUID:ID", help="prior entry in project history")
    v.add_argument("-t", "--target", help="declaration to audit (default: last theorem)")
    v.add_argument(
        "--no-daemon", action="store_true", help="verify in this process, not via the daemon"
    )
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
    st.add_argument(
        "--no-daemon", action="store_true", help="work in this process, not via the daemon"
    )
    st.set_defaults(func=cmd_statement)

    se = sub.add_parser("search", help="find Mathlib lemmas")
    se.add_argument("query", nargs="+")
    se.add_argument("--project", help="registered project name, UUID or path")
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
    g.add_argument(
        "--no-daemon", action="store_true", help="work in this process, not via the daemon"
    )
    g.set_defaults(func=cmd_close)

    lg = sub.add_parser("log", help="show recent verifications")
    lg.add_argument("-n", "--limit", type=int, help="maximum recent entries (default: 20)")
    lg.add_argument("--id", type=int, help="retrieve one entry, including its source, read-only")
    lg.add_argument("--project", help="registered project name, UUID or path")
    lg.add_argument("--ref", metavar="UUID:ID", help="retrieve a local, imported or linked origin")
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

    sv = sub.add_parser("serve", help="run the session daemon for this checkout")
    sv.add_argument("--socket", help="listen here instead of the resolved runtime directory")
    sv.add_argument("--pool", type=int, default=2, help="concurrent Lean sessions")
    sv.add_argument("--no-warm", action="store_true", help="start sessions lazily")
    sv.add_argument(
        "--install", action="store_true", help="write the service files for this platform"
    )
    sv.add_argument(
        "--print-units", action="store_true", help="show those files without writing them"
    )
    sv.set_defaults(func=cmd_serve)

    pr = sub.add_parser("project", help="register projects and associate ledger history")
    pr.add_argument("--json", action="store_true", help="emit JSON")
    actions = pr.add_subparsers(dest="project_action", required=True)
    register = actions.add_parser("register", help="register a checkout; trust is explicit")
    register.add_argument("path")
    register.add_argument("--name")
    register.add_argument("--trust", action="store_true")
    register.add_argument("--relocate", action="store_true")
    rename = actions.add_parser("rename", help="rename a project, retaining its old alias")
    rename.add_argument("selector")
    rename.add_argument("name")
    listing = actions.add_parser("list", help="list registered projects")
    for parser in (register, rename, listing):
        parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        parser.set_defaults(func=cmd_project)
    for mode in ("link", "import"):
        transfer = actions.add_parser(mode, help=f"{mode} selected history into a project")
        transfer.add_argument("selector")
        transfer.add_argument("--from-ledger", required=True)
        selection = transfer.add_mutually_exclusive_group(required=True)
        selection.add_argument("--id", type=int, action="append")
        selection.add_argument("--tag")
        transfer.add_argument("--dry-run", action="store_true")
        transfer.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        transfer.set_defaults(func=cmd_project)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except D.DaemonError as exc:
        print(f"daemon error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except LedgerReadError as exc:
        print(f"ledger error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"file error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

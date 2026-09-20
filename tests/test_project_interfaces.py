"""Project interface contracts, using mocks rather than Lean, registries or databases."""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nullius import cli, harness, http_server, mcp_server  # noqa: E402
from nullius import search as S  # noqa: E402
from nullius.config import Config  # noqa: E402
from nullius.ledger import LedgerReadError  # noqa: E402
from nullius.projects import Project, ProjectError  # noqa: E402
from nullius.verify import Verdict  # noqa: E402

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
LEDGER_ID = "22222222-2222-4222-8222-222222222222"
ORIGIN_ID = "33333333-3333-4333-8333-333333333333"
REFERENCE = f"{ORIGIN_ID}:7"
COPY_REFERENCE = f"{LEDGER_ID}:42"
SOURCE = "theorem saved : True := by trivial"
LOCAL = {
    "id": 42,
    "status": "verified",
    "verified": 1,
    "target": "Demo.saved",
    "source": "",
    "created_at": 20.0,
    "created_iso": "2026-01-02T00:00:00",
    "claim": "saved claim",
    "statement": "True",
    "failures": "[]",
    "tag": "paper",
    "record_kind": "project",
    "project_id": PROJECT_ID,
    "module": "Demo",
    "environment_id": "environment",
    "origin": REFERENCE,
    "reference": REFERENCE,
}
LINKED = {
    **LOCAL,
    "id": 7,
    "status": "rejected",
    "verified": 0,
    "created_at": 30.0,
    "record_kind": "snippet",
    "source": SOURCE,
    "linked": True,
    "origin": f"{ORIGIN_ID}:8",
    "reference": f"{ORIGIN_ID}:8",
}


class ProjectInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.cfg = Config(
            lean_dir=Path("verifier-lean"),
            repl_bin=Path("unused-repl"),
            lake_bin=Path("unused-lake"),
            ledger_path=Path("standalone.sqlite3"),
        )
        self.project = Project(PROJECT_ID, "demo", Path("/unused-demo"), True, ())
        self.project_cfg = replace(
            self.cfg,
            ledger_path=self.project.ledger_path,
            ledger_index_dir=self.project.index_dir,
            project_id=PROJECT_ID,
            project_name="demo",
            project_root=self.project.root,
            project_trusted=True,
        )
        self.discover = self.mock(Config, "discover", return_value=self.cfg)
        self.select = self.mock(Config, "for_project", autospec=True, return_value=self.project_cfg)
        self.mock(Config, "provenance", return_value={"toolchain": "lean", "mathlib_rev": "rev"})
        self.verdict = Verdict(
            "verified",
            "Demo.saved",
            "saved claim",
            statement="True",
            provenance={"environment_id": "environment"},
        )
        self.pool = self.mock(harness, "SessionPool")
        self.pool.return_value.size = 2
        self.borrow = self.mock(harness, "borrow")
        self.cli_session = self.mock(cli, "_session")
        self.mcp_session = self.mock(mcp_server, "session")
        self.ledgers = {}
        self.checkers = {}
        self.verifiers = {}
        for owner in (cli, harness, mcp_server):
            self.ledgers[owner] = self.mock(owner, "Ledger")
            self.ledgers[owner].return_value.record.return_value = 42
            self.ledgers[owner].return_value.recall.return_value = []
            self.checkers[owner] = self.mock(owner, "verify_project", return_value=self.verdict)
            self.verifiers[owner] = self.mock(owner, "Verifier")
            self.verifiers[owner].return_value.verify.return_value = self.verdict
        self.mcp_verifier = self.mock(mcp_server, "verifier")
        self.mcp_verifier.return_value.verify.return_value = self.verdict
        self.origins = self.mock(harness, "read_entry_reference", return_value=LOCAL)
        self.reads = {}
        self.references = {}
        self.histories = {}
        for owner in (cli, http_server, mcp_server):
            self.reads[owner] = self.mock(owner, "read_ledger_row", return_value=LOCAL)
            self.references[owner] = self.mock(owner, "read_entry_reference", return_value=LOCAL)
            self.histories[owner] = self.mock(
                owner, "read_project_rows", return_value=[LOCAL, LINKED]
            )
        self.register = self.mock(cli, "register_project", return_value=self.project)
        self.rename = self.mock(cli, "rename_project", return_value=self.project)
        self.list_projects = self.mock(cli, "list_projects", return_value=[self.project])
        self.resolve = self.mock(cli, "resolve_project", return_value=self.project)
        self.transfer = self.mock(
            cli,
            "transfer_entries",
            return_value={
                "action": "would-link",
                "count": 2,
                "destination": str(self.project.ledger_path),
                "notes": ["would-assign-UUID: source"],
            },
        )
        self.search = self.mock(S, "search", return_value=[])
        self.close_sessions = self.mock(S, "close_sessions")
        self.stack.enter_context(patch.object(mcp_server, "_config", self.cfg))
        self.stack.enter_context(patch.object(mcp_server, "_ledger", None))
        self.stack.enter_context(patch.object(mcp_server, "_session", None))
        self.http_harness = harness.Harness(config=self.project_cfg, tag="http")
        self.stack.enter_context(patch.object(http_server, "_harness", self.http_harness))
        self.ledgers[harness].reset_mock()
        self.pool.reset_mock()
        for name in (
            "sqlite3.connect",
            "subprocess.Popen",
            "subprocess.run",
            "urllib.request.urlopen",
        ):
            self.stack.enter_context(
                patch(name, side_effect=AssertionError(f"unexpected external operation: {name}"))
            )

    def mock(self, owner, name, **kwargs):
        return self.stack.enter_context(patch.object(owner, name, **kwargs))

    def cli_call(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(args))
            except SystemExit as exc:
                code = exc.code
        self.assertIsInstance(code, int)
        return code, out.getvalue(), err.getvalue()

    def mcp_call(self, name: str, args: dict) -> dict:
        response = mcp_server.handle(
            {"id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
        )
        assert response is not None
        return response["result"]

    def http_call(self, path: str, body=None, method: str = "POST") -> tuple[int, object]:
        handler = http_server.Handler.__new__(http_server.Handler)
        handler.path = path
        encoded = json.dumps(body if body is not None else {}).encode()
        handler.headers = {"Content-Length": str(len(encoded))}
        handler.rfile = io.BytesIO(encoded)
        handler._send = Mock()
        if method == "GET":
            handler.do_GET()
        else:
            handler.do_POST()
        handler._send.assert_called_once()
        return handler._send.call_args.args

    def test_registration_is_explicit_and_does_not_enable_trust(self) -> None:
        code, out, _ = self.cli_call("project", "register", "/unused-demo", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.project.to_dict())
        self.register.assert_called_once_with(
            Path("/unused-demo"), None, trust=False, relocate=False
        )
        self.discover.assert_not_called()
        self.checkers[cli].assert_not_called()

    def test_registration_forwards_trust_name_and_relocation(self) -> None:
        self.assertEqual(
            self.cli_call(
                "project", "--json", "register", "/copy", "--name", "new", "--trust", "--relocate"
            )[0],
            0,
        )
        self.register.assert_called_once_with(Path("/copy"), "new", trust=True, relocate=True)

    def test_rename_and_list_keep_registry_identity(self) -> None:
        code, out, _ = self.cli_call("project", "rename", PROJECT_ID, "new", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["id"], PROJECT_ID)
        self.rename.assert_called_once_with(PROJECT_ID, "new")
        self.assertEqual(
            json.loads(self.cli_call("project", "list", "--json")[1]),
            [self.project.to_dict()],
        )
        self.assertIn(PROJECT_ID, self.cli_call("project", "list")[1])
        self.transfer.assert_not_called()

    def test_link_and_import_route_explicit_selection_and_dry_run(self) -> None:
        for mode in ("link", "import"):
            with self.subTest(mode=mode):
                code, out, _ = self.cli_call(
                    "project",
                    mode,
                    "demo",
                    "--from-ledger",
                    "old.db",
                    "--id",
                    "7",
                    "--id",
                    "8",
                    "--dry-run",
                    "--json",
                )
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["count"], 2)
                self.transfer.assert_called_with(
                    Path("old.db"),
                    self.project.ledger_path,
                    ids=[7, 8],
                    tag=None,
                    mode=mode,
                    dry_run=True,
                )
                code, _, _ = self.cli_call(
                    "project", mode, "demo", "--from-ledger", "old.db", "--tag", "paper"
                )
                self.assertEqual(code, 0)
                self.transfer.assert_called_with(
                    Path("old.db"),
                    self.project.ledger_path,
                    ids=None,
                    tag="paper",
                    mode=mode,
                    dry_run=False,
                )
        self.ledgers[cli].assert_not_called()
        self.register.assert_not_called()

    def test_transfer_requires_exclusive_selection(self) -> None:
        for extra in ((), ("--id", "7", "--tag", "paper")):
            with self.subTest(extra=extra):
                self.assertEqual(
                    self.cli_call("project", "link", "demo", "--from-ledger", "old.db", *extra)[0],
                    2,
                )
        self.transfer.assert_not_called()

    def test_registry_failures_are_clear_without_fallback_or_execution(self) -> None:
        self.select.side_effect = ProjectError("project is not registered")
        self.register.side_effect = ProjectError("different registered root")
        self.assertIn("different registered root", self.cli_call("project", "register", ".")[2])
        self.assertEqual(
            self.cli_call("verify", "--project", "unknown", "--module", "Demo", "-t", "saved")[0],
            2,
        )
        self.assertTrue(
            self.mcp_call("verify", {"project": "unknown", "module": "Demo", "target": "saved"})[
                "isError"
            ]
        )
        self.assertEqual(
            self.http_call("/verify", {"project": "unknown", "module": "Demo", "target": "saved"})[
                0
            ],
            400,
        )
        for checker in self.checkers.values():
            checker.assert_not_called()

    def test_harness_project_selection_preserves_verifier_tree(self) -> None:
        h = harness.Harness(2, self.cfg, True, "paper", project="demo")
        self.select.assert_called_once_with(self.cfg, "demo")
        self.assertIs(h.config, self.project_cfg)
        self.assertIsNone(self.cfg.project_id)
        self.assertEqual(h.config.lean_dir, self.cfg.lean_dir)
        self.pool.assert_called_once_with(self.project_cfg, size=2)
        self.ledgers[harness].assert_called_once_with(self.project.ledger_path)
        self.ledgers[harness].return_value.bind_project.assert_called_once_with(PROJECT_ID)
        h.close()
        self.close_sessions.assert_called_once_with(self.project_cfg)

    def test_harness_rejects_mismatched_ledger_binding(self) -> None:
        self.ledgers[harness].return_value.bind_project.side_effect = ValueError("wrong project")
        with self.assertRaisesRegex(ValueError, "wrong project"):
            harness.Harness(config=self.cfg, project="demo")
        self.pool.assert_not_called()
        self.ledgers[harness].return_value.record.assert_not_called()

    def test_harness_project_snippets_are_not_module_records(self) -> None:
        h = harness.Harness(config=self.cfg, project="demo", tag="paper")
        self.assertIs(h.verify(SOURCE), self.verdict)
        self.ledgers[harness].return_value.record.assert_called_once_with(
            self.verdict, SOURCE, tag="paper", project_id=PROJECT_ID
        )
        self.borrow.assert_called_once_with(h.pool)
        self.checkers[harness].assert_not_called()

    def test_harness_standalone_record_call_remains_compatible(self) -> None:
        h = harness.Harness(config=self.cfg)
        self.assertIs(h.verify(SOURCE), self.verdict)
        self.ledgers[harness].return_value.record.assert_called_once_with(
            self.verdict, SOURCE, tag=None
        )
        self.ledgers[harness].return_value.bind_project.assert_not_called()

    def test_harness_module_records_reference_and_canonical_origin(self) -> None:
        h = harness.Harness(config=self.cfg, project="demo", tag="paper")
        result = h.verify_module(
            "Demo", "Demo.saved", claim="saved claim", derived_from=COPY_REFERENCE, timeout=9
        )
        self.assertIs(result, self.verdict)
        self.checkers[harness].assert_called_once_with(
            self.project_cfg,
            "Demo",
            "Demo.saved",
            claim="saved claim",
            require_nontrivial=False,
            build=False,
            timeout=9,
        )
        self.origins.assert_called_once_with(self.project.ledger_path, COPY_REFERENCE)
        self.ledgers[harness].return_value.record.assert_called_once_with(
            self.verdict,
            "",
            tag="paper",
            record_kind="project",
            project_id=PROJECT_ID,
            module="Demo",
            environment_id="environment",
            derived_from=REFERENCE,
        )
        self.borrow.assert_not_called()

    def test_harness_module_requires_registered_trusted_project(self) -> None:
        for cfg in (self.cfg, replace(self.project_cfg, project_trusted=False)):
            with (
                self.subTest(cfg=cfg),
                self.assertRaisesRegex(ValueError, "registered project|untrusted"),
            ):
                harness.Harness(config=cfg, log=False).verify_module("Demo", "Demo.saved")
        self.checkers[harness].assert_not_called()
        self.ledgers[harness].assert_not_called()

    def test_harness_derivation_uses_enriched_reference_without_origin_alias(self) -> None:
        self.origins.return_value = {
            key: value
            for key, value in {**LOCAL, "ledger_uuid": LEDGER_ID}.items()
            if key != "origin"
        }
        self.http_harness.verify_module("Demo", "Demo.saved", derived_from=COPY_REFERENCE)
        self.assertEqual(
            self.ledgers[harness].return_value.record.call_args.kwargs["derived_from"], REFERENCE
        )

    def test_harness_no_log_module_never_reads_or_creates_ledger(self) -> None:
        h = harness.Harness(config=self.cfg, project="demo", log=False)
        self.assertIs(h.verify_module("Demo", "Demo.saved"), self.verdict)
        h.verify(SOURCE)
        with self.assertRaisesRegex(ValueError, "log=False"):
            h.verify_module("Demo", "Demo.saved", derived_from=REFERENCE)
        self.origins.assert_not_called()
        self.ledgers[harness].assert_not_called()
        self.checkers[harness].assert_called_once()

    def test_harness_missing_derivation_blocks_verification(self) -> None:
        self.origins.return_value = None
        with self.assertRaisesRegex(ValueError, "not found"):
            self.http_harness.verify_module("Demo", "Demo.saved", derived_from=REFERENCE)
        self.checkers[harness].assert_not_called()
        self.ledgers[harness].return_value.record.assert_not_called()

    def test_verify_once_accepts_project_selector(self) -> None:
        self.assertIs(
            harness.verify_once(SOURCE, project="demo", config=self.cfg, log=False),
            self.verdict,
        )
        self.select.assert_called_once_with(self.cfg, "demo")
        self.ledgers[harness].assert_not_called()
        self.pool.return_value.close.assert_called_once()

    def test_cli_module_routes_without_snippet_session_or_implicit_build(self) -> None:
        code, out, err = self.cli_call(
            "verify", "--project", "demo", "--module", "Demo", "-t", "Demo.saved", "--json"
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["recall"], [])
        self.checkers[cli].assert_called_once_with(
            self.project_cfg,
            "Demo",
            "Demo.saved",
            claim=None,
            require_nontrivial=False,
            build=False,
            timeout=None,
        )
        self.ledgers[cli].return_value.bind_project.assert_called_once_with(PROJECT_ID)
        self.ledgers[cli].return_value.record.assert_called_once_with(
            self.verdict,
            "",
            tag=None,
            record_kind="project",
            project_id=PROJECT_ID,
            module="Demo",
            environment_id="environment",
            derived_from=None,
        )
        self.cli_session.assert_not_called()
        self.ledgers[cli].return_value.recall.assert_not_called()

    def test_cli_module_build_and_derivation_are_explicit(self) -> None:
        code, _, _ = self.cli_call(
            "verify",
            "--project",
            "demo",
            "--module",
            "Demo",
            "-t",
            "Demo.saved",
            "--build",
            "--derived-from",
            COPY_REFERENCE,
            "--require-nontrivial",
        )
        self.assertEqual(code, 0)
        self.assertTrue(self.checkers[cli].call_args.kwargs["build"])
        self.assertTrue(self.checkers[cli].call_args.kwargs["require_nontrivial"])
        self.assertEqual(
            self.ledgers[cli].return_value.record.call_args.kwargs["derived_from"], REFERENCE
        )

    def test_cli_no_log_module_avoids_ledger(self) -> None:
        self.assertEqual(
            self.cli_call(
                "verify", "--project", "demo", "--module", "Demo", "-t", "saved", "--no-log"
            )[0],
            0,
        )
        self.ledgers[cli].assert_not_called()
        self.origins.assert_not_called()

    def test_cli_rejects_incomplete_or_mixed_module_modes_before_io(self) -> None:
        cases = [
            (),
            ("--project", "demo"),
            ("--module", "Demo", "-t", "saved"),
            ("--project", "demo", "--module", "Demo"),
            ("file.lean", "--build"),
            ("file.lean", "--derived-from", REFERENCE),
            ("--project", "demo", "--module", "Demo", "-t", "saved", "file.lean"),
            ("--project", "demo", "--module", "Demo", "-t", "saved", "--no-vacuity"),
            (
                "--project",
                "demo",
                "--module",
                "Demo",
                "-t",
                "saved",
                "--no-log",
                "--derived-from",
                REFERENCE,
            ),
        ]
        for args in cases:
            with self.subTest(args=args):
                code, _, err = self.cli_call("verify", *args)
                self.assertEqual(code, 2)
                self.assertNotIn("Traceback", err)
        self.discover.assert_not_called()
        self.cli_session.assert_not_called()
        self.ledgers[cli].assert_not_called()

    def test_cli_source_logging_retains_snippet_contract(self) -> None:
        for project in (False, True):
            with self.subTest(project=project), patch.object(sys, "stdin", io.StringIO(SOURCE)):
                extra = ("--project", "demo") if project else ()
                self.assertEqual(self.cli_call("verify", "-", *extra)[0], 0)
                expected = {"project_id": PROJECT_ID} if project else {}
                self.ledgers[cli].return_value.record.assert_called_with(
                    self.verdict, SOURCE, tag=None, **expected
                )
                self.cli_session.assert_called_with(self.project_cfg if project else self.cfg)
        self.checkers[cli].assert_not_called()

    def test_cli_missing_derivation_does_not_create_ledger(self) -> None:
        self.origins.return_value = None
        code, _, err = self.cli_call(
            "verify",
            "--project",
            "demo",
            "--module",
            "Demo",
            "-t",
            "saved",
            "--derived-from",
            REFERENCE,
        )
        self.assertEqual(code, 2)
        self.assertIn("not found", err)
        self.ledgers[cli].assert_not_called()
        self.checkers[cli].assert_not_called()

    def test_cli_and_mcp_binding_mismatch_blocks_native_checker(self) -> None:
        for owner in (cli, mcp_server):
            self.ledgers[owner].return_value.bind_project.side_effect = ValueError("wrong project")
        code, _, err = self.cli_call(
            "verify", "--project", "demo", "--module", "Demo", "-t", "saved"
        )
        self.assertEqual(code, 2)
        self.assertIn("wrong project", err)
        response = self.mcp_call("verify", {"project": "demo", "module": "Demo", "target": "saved"})
        self.assertTrue(response["isError"])
        self.assertIn("wrong project", response["content"][0]["text"])
        self.checkers[cli].assert_not_called()
        self.checkers[mcp_server].assert_not_called()

    def test_project_search_routes_config_without_global_mutation(self) -> None:
        self.assertEqual(self.cli_call("search", "needle", "--project", "demo")[0], 0)
        self.assertIs(self.search.call_args.kwargs["config"], self.project_cfg)
        self.close_sessions.assert_called_with(self.project_cfg)
        response = self.mcp_call("search", {"query": "needle", "project": "demo"})
        self.assertFalse(response["isError"])
        self.assertIs(self.search.call_args.kwargs["config"], self.project_cfg)
        self.http_harness.search("needle")
        self.assertIs(self.search.call_args.kwargs["config"], self.project_cfg)
        self.assertIs(mcp_server._config, self.cfg)
        self.assertIsNone(self.cfg.project_id)

    def test_project_search_failure_does_not_report_success(self) -> None:
        self.search.return_value = [
            S.SearchResult(
                "needle", "loogle", [S.Hit("library.saved", signature="True", source="loogle")]
            ),
            S.SearchResult("needle", "project-ledger", error="project index needs refresh"),
        ]
        code, out, _ = self.cli_call("search", "needle", "--project", "demo", "--include-ledger")
        self.assertEqual(code, 1)
        self.assertIn("library.saved", out)
        self.assertIn("project index needs refresh", out)
        response = self.mcp_call(
            "search", {"query": "needle", "project": "demo", "include_ledger": True}
        )
        self.assertTrue(response["isError"])
        self.assertIn("library.saved", response["content"][0]["text"])
        self.assertIn("project index needs refresh", response["content"][0]["text"])

    def test_cli_project_log_listing_includes_links_readonly(self) -> None:
        code, out, _ = self.cli_call("log", "--project", "demo", "--json", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [LINKED])
        self.histories[cli].assert_called_once_with(self.project.ledger_path, verified_only=False)
        self.assertEqual(
            json.loads(
                self.cli_call(
                    "log", "--project", "demo", "--status", "verified", "--tag", "paper", "--json"
                )[1]
            ),
            [LOCAL],
        )
        self.assertIn(REFERENCE, self.cli_call("log", "--project", "demo")[1])
        stats = json.loads(self.cli_call("log", "--project", "demo", "--stats")[1])
        self.assertEqual(stats["by_status"], {"verified": 1, "rejected": 1})
        self.ledgers[cli].assert_not_called()

    def test_project_recall_is_readonly_and_only_matches_verified_rows(self) -> None:
        code, out, _ = self.cli_call("log", "--project", "demo", "--recall", "saved", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [LOCAL])
        response = self.mcp_call("log", {"project": "demo", "recall": "saved"})
        self.assertFalse(response["isError"])
        self.assertIn(REFERENCE, response["content"][0]["text"])
        self.assertNotIn(LINKED["origin"], response["content"][0]["text"])
        self.ledgers[cli].assert_not_called()
        self.ledgers[mcp_server].assert_not_called()

    def test_project_recall_preserves_candidate_and_project_context_warnings(self) -> None:
        code, text, _ = self.cli_call("log", "--project", "demo", "--recall", "saved")
        self.assertEqual(code, 0)
        response = self.mcp_call("log", {"project": "demo", "recall": "saved"})
        self.assertFalse(response["isError"])
        for rendered in (text, response["content"][0]["text"]):
            self.assertIn("candidate only", rendered)
            self.assertIn("not a stored source snapshot", rendered)
            self.assertIn(PROJECT_ID, rendered)
            self.assertIn(REFERENCE, rendered)
            self.assertIn("verified (historical)", rendered)
        self.ledgers[cli].assert_not_called()
        self.ledgers[mcp_server].assert_not_called()

    def test_cli_project_reference_and_local_id_reads(self) -> None:
        for flag, value in (("--ref", REFERENCE), ("--id", "42")):
            with self.subTest(flag=flag):
                code, out, _ = self.cli_call("log", "--project", "demo", flag, value, "--json")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out), LOCAL)
        self.references[cli].assert_called_once_with(self.project.ledger_path, REFERENCE)
        self.reads[cli].assert_called_once_with(self.project.ledger_path, 42)
        self.ledgers[cli].assert_not_called()

    def test_cli_reference_excludes_ids_and_listing_filters(self) -> None:
        for extra in (
            ("--id", "42"),
            ("--limit", "1"),
            ("--tag", "paper"),
            ("--recall", ""),
            ("--stats",),
            ("--status", "verified"),
        ):
            with self.subTest(extra=extra):
                self.assertEqual(self.cli_call("log", "--ref", REFERENCE, *extra)[0], 2)
        self.references[cli].assert_not_called()
        self.discover.assert_not_called()

    def test_mcp_schema_has_five_tools_and_typed_project_modes(self) -> None:
        tools = {tool["name"]: tool["inputSchema"] for tool in mcp_server.TOOLS}
        self.assertEqual(set(tools), {"verify", "statement", "search", "close", "log"})
        self.assertEqual(
            [variant["required"] for variant in tools["verify"]["anyOf"]],
            [["source"], ["project", "module", "target"]],
        )
        for key, kind in (
            ("project", "string"),
            ("module", "string"),
            ("build", "boolean"),
            ("derived_from", "string"),
        ):
            self.assertEqual(tools["verify"]["properties"][key]["type"], kind)
        self.assertNotIn("trust", tools["verify"]["properties"])

    def test_mcp_module_uses_project_ledger_and_no_implicit_build(self) -> None:
        global_ledger = Mock()
        self.stack.enter_context(patch.object(mcp_server, "_ledger", global_ledger))
        response = self.mcp_call(
            "verify",
            {
                "project": "demo",
                "module": "Demo",
                "target": "Demo.saved",
                "derived_from": COPY_REFERENCE,
            },
        )
        self.assertFalse(response["isError"])
        self.checkers[mcp_server].assert_called_once_with(
            self.project_cfg,
            "Demo",
            "Demo.saved",
            claim=None,
            require_nontrivial=False,
            build=False,
        )
        self.ledgers[mcp_server].assert_called_once_with(self.project.ledger_path)
        self.ledgers[mcp_server].return_value.record.assert_called_once_with(
            self.verdict,
            "",
            tag="mcp",
            record_kind="project",
            project_id=PROJECT_ID,
            module="Demo",
            environment_id="environment",
            derived_from=REFERENCE,
        )
        global_ledger.record.assert_not_called()
        global_ledger.recall.assert_not_called()
        self.mcp_session.assert_not_called()
        self.mcp_verifier.assert_not_called()

    def test_mcp_project_source_keeps_fixed_prelude(self) -> None:
        response = self.mcp_call("verify", {"source": SOURCE, "project": "demo"})
        self.assertFalse(response["isError"])
        self.verifiers[mcp_server].assert_called_once_with(
            self.mcp_session.return_value, self.project_cfg
        )
        self.ledgers[mcp_server].return_value.record.assert_called_once_with(
            self.verdict, SOURCE, tag="mcp", project_id=PROJECT_ID
        )
        self.checkers[mcp_server].assert_not_called()
        self.assertIs(mcp_server._config, self.cfg)
        self.assertIsNone(mcp_server._ledger)

    def test_mcp_standalone_source_record_signature_unchanged(self) -> None:
        self.assertFalse(self.mcp_call("verify", {"source": SOURCE})["isError"])
        self.mcp_verifier.return_value.verify.assert_called_once_with(
            SOURCE, target=None, claim=None, require_nontrivial=False
        )
        self.ledgers[mcp_server].return_value.record.assert_called_once_with(
            self.verdict, SOURCE, tag="mcp"
        )
        self.select.assert_not_called()

    def test_mcp_invalid_verification_modes_are_errors(self) -> None:
        for args in (
            {},
            {"source": ""},
            {"source": 1},
            {"source": SOURCE, "build": False},
            {"source": SOURCE, "derived_from": REFERENCE},
            {"module": "Demo", "target": "saved"},
            {"module": "Demo", "project": "demo"},
            {"source": SOURCE, "module": "Demo", "project": "demo", "target": "saved"},
            {"module": "Demo", "project": "demo", "target": "saved", "build": "false"},
            {"module": "Demo", "project": "demo", "target": "saved", "no_vacuity": True},
            {"module": None, "project": "demo", "target": "saved"},
            {"source": SOURCE, "project": None},
            {"module": "Demo", "project": "demo", "target": "saved", "derived_from": None},
            {"module": "Demo", "project": "demo", "target": "saved", "derived_from": True},
        ):
            with self.subTest(args=args):
                response = self.mcp_call("verify", args)
                self.assertTrue(response["isError"])
                self.assertNotIn("Traceback", response["content"][0]["text"])
        self.checkers[mcp_server].assert_not_called()
        self.ledgers[mcp_server].assert_not_called()

    def test_mcp_unsupported_tools_do_not_ignore_project_selector(self) -> None:
        for name in ("statement", "close"):
            with self.subTest(name=name):
                response = self.mcp_call(name, {"project": "demo"})
                self.assertTrue(response["isError"])
                self.assertNotIn("Traceback", response["content"][0]["text"])
        self.mcp_verifier.assert_not_called()
        self.mcp_session.assert_not_called()

    def test_module_boolean_options_are_never_coerced(self) -> None:
        args = {"project": "demo", "module": "Demo", "target": "saved"}
        for key in ("build", "require_nontrivial"):
            for value in ("false", "true", 0, 1, None, [], {}):
                with self.subTest(key=key, value=value):
                    response = self.mcp_call("verify", {**args, key: value})
                    self.assertTrue(response["isError"])
                    self.assertIn("must be a boolean", response["content"][0]["text"])
                    code, out = self.http_call(
                        "/verify", {"module": "Demo", "target": "saved", key: value}
                    )
                    self.assertEqual(code, 400)
                    self.assertIn("must be a boolean", out["error"])
        self.checkers[mcp_server].assert_not_called()
        self.checkers[harness].assert_not_called()

    def test_failed_module_verdict_keeps_reference_and_environment(self) -> None:
        failed = replace(
            self.verdict, status="error", provenance={"environment_id": "failed-environment"}
        )
        for checker in self.checkers.values():
            checker.return_value = failed
        code, out, _ = self.cli_call(
            "verify", "--project", "demo", "--module", "Demo", "-t", "saved", "--build", "--json"
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "error")
        response = self.mcp_call(
            "verify", {"project": "demo", "module": "Demo", "target": "saved", "build": True}
        )
        self.assertFalse(response["isError"])
        self.assertIn("NOT verified", response["content"][0]["text"])
        code, out = self.http_call("/verify", {"module": "Demo", "target": "saved", "build": True})
        self.assertEqual(code, 200)
        self.assertEqual(out["status"], "error")
        self.assertFalse(out["verified"])
        for lg in self.ledgers.values():
            lg.return_value.record.assert_called_once()
            self.assertEqual(lg.return_value.record.call_args.args, (failed, ""))
            self.assertEqual(
                lg.return_value.record.call_args.kwargs["environment_id"], "failed-environment"
            )
            self.assertEqual(lg.return_value.record.call_args.kwargs["record_kind"], "project")

    def test_checker_registration_errors_are_not_recorded_as_verdicts(self) -> None:
        for checker in self.checkers.values():
            checker.side_effect = ValueError("project registration no longer matches")
        self.assertEqual(
            self.cli_call("verify", "--project", "demo", "--module", "Demo", "-t", "saved")[0],
            2,
        )
        response = self.mcp_call("verify", {"project": "demo", "module": "Demo", "target": "saved"})
        self.assertTrue(response["isError"])
        self.assertIn("registration no longer matches", response["content"][0]["text"])
        self.assertEqual(self.http_call("/verify", {"module": "Demo", "target": "saved"})[0], 400)
        for lg in self.ledgers.values():
            lg.return_value.record.assert_not_called()

    def test_no_remote_trust_or_registration_bypass(self) -> None:
        self.select.return_value = replace(self.project_cfg, project_trusted=False)
        args = {"project": "demo", "module": "Demo", "target": "saved"}
        for extra in ({}, {"trust": True}, {"relocate": True}):
            with self.subTest(extra=extra):
                self.assertTrue(self.mcp_call("verify", {**args, **extra})["isError"])
                self.assertEqual(self.http_call("/verify", {**args, **extra})[0], 400)
        self.assertEqual(
            self.cli_call("verify", "--project", "demo", "--module", "Demo", "-t", "saved")[0],
            2,
        )
        for checker in self.checkers.values():
            checker.assert_not_called()
        self.register.assert_not_called()

    def test_mcp_project_history_and_reference_are_readonly(self) -> None:
        response = self.mcp_call("log", {"project": "demo", "limit": 1})
        self.assertFalse(response["isError"])
        self.assertIn(LINKED["origin"], response["content"][0]["text"])
        response = self.mcp_call("log", {"project": "demo", "ref": REFERENCE})
        self.assertEqual(json.loads(response["content"][0]["text"]), LOCAL)
        self.references[mcp_server].assert_called_once_with(self.project.ledger_path, REFERENCE)
        response = self.mcp_call("log", {"project": "demo", "id": 42})
        self.assertFalse(response["isError"])
        self.reads[mcp_server].assert_called_once_with(self.project.ledger_path, 42)
        self.ledgers[mcp_server].assert_not_called()

    def test_mcp_reference_excludes_id_and_listing_options(self) -> None:
        for key, value in (
            ("id", 42),
            ("limit", 10),
            ("status", "verified"),
            ("tag", ""),
            ("stats", False),
            ("recall", ""),
        ):
            with self.subTest(key=key):
                self.assertTrue(self.mcp_call("log", {"ref": REFERENCE, key: value})["isError"])
        self.references[mcp_server].assert_not_called()

    def test_reference_resolution_errors_are_explicit(self) -> None:
        for owner in (cli, mcp_server, http_server):
            self.references[owner].return_value = None
        self.assertEqual(self.cli_call("log", "--project", "demo", "--ref", REFERENCE)[0], 1)
        self.assertTrue(self.mcp_call("log", {"project": "demo", "ref": REFERENCE})["isError"])
        self.assertEqual(self.http_call(f"/ledger?ref={REFERENCE}", method="GET")[0], 404)
        for owner in (cli, mcp_server, http_server):
            self.references[owner].side_effect = LedgerReadError("linked source is missing")
        self.assertIn(
            "linked source is missing",
            self.cli_call("log", "--project", "demo", "--ref", REFERENCE)[2],
        )
        response = self.mcp_call("log", {"project": "demo", "ref": REFERENCE})
        self.assertTrue(response["isError"])
        self.assertNotIn("Traceback", response["content"][0]["text"])
        self.assertEqual(self.http_call(f"/ledger?ref={REFERENCE}", method="GET")[0], 500)

    def test_broken_project_history_never_falls_back_to_local_listing(self) -> None:
        for owner in (cli, mcp_server, http_server):
            self.histories[owner].side_effect = LedgerReadError("linked source moved")
        self.assertEqual(self.cli_call("log", "--project", "demo")[0], 1)
        response = self.mcp_call("log", {"project": "demo"})
        self.assertTrue(response["isError"])
        self.assertIn("linked source moved", response["content"][0]["text"])
        code, out = self.http_call("/ledger", method="GET")
        self.assertEqual(code, 500)
        self.assertIn("linked source moved", out["error"])
        for lg in self.ledgers.values():
            lg.return_value.recent.assert_not_called()

    def test_http_configured_module_and_search_inherit_project(self) -> None:
        code, out = self.http_call("/verify", {"module": "Demo", "target": "saved"})
        self.assertEqual(code, 200)
        self.assertIn("feedback", out)
        self.checkers[harness].assert_called_once_with(
            self.project_cfg,
            "Demo",
            "saved",
            claim=None,
            require_nontrivial=False,
            build=False,
            timeout=None,
        )
        self.borrow.assert_not_called()
        self.assertEqual(self.http_call("/search", {"query": "needle"})[0], 200)
        self.assertIs(self.search.call_args.kwargs["config"], self.project_cfg)

    def test_http_explicit_module_build_and_derivation(self) -> None:
        code, _ = self.http_call(
            "/verify",
            {"module": "Demo", "target": "saved", "build": True, "derived_from": COPY_REFERENCE},
        )
        self.assertEqual(code, 200)
        self.assertTrue(self.checkers[harness].call_args.kwargs["build"])
        self.assertEqual(
            self.ledgers[harness].return_value.record.call_args.kwargs["derived_from"], REFERENCE
        )

    def test_http_body_and_query_project_use_temporary_harness(self) -> None:
        base = harness.Harness(config=self.cfg, log=False, tag="http")
        self.stack.enter_context(patch.object(http_server, "_harness", base))
        for path, body in (
            ("/verify", {"project": "demo", "module": "Demo", "target": "saved"}),
            ("/verify?project=demo", {"module": "Demo", "target": "saved"}),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.http_call(path, body)[0], 200)
                self.assertIs(self.checkers[harness].call_args.args[0], self.project_cfg)
        self.assertIs(http_server._harness, base)
        self.assertIs(base.config, self.cfg)
        self.assertIsNone(base.ledger)
        self.ledgers[harness].assert_not_called()
        self.assertEqual(self.pool.return_value.close.call_count, 2)
        self.close_sessions.assert_called_with(self.project_cfg)

    def test_http_temporary_harness_closes_before_sending_response(self) -> None:
        self.pool.return_value.close.side_effect = RuntimeError("close failed")
        code, out = self.http_call(
            "/verify", {"project": "demo", "module": "Demo", "target": "saved"}
        )
        self.assertEqual(code, 500)
        self.assertIn("close failed", out["error"])
        self.close_sessions.assert_called_once_with(self.project_cfg)

    def test_http_no_log_project_never_reads_ledger(self) -> None:
        h = harness.Harness(config=self.project_cfg, log=False)
        self.stack.enter_context(patch.object(http_server, "_harness", h))
        self.assertEqual(self.http_call("/verify", {"module": "Demo", "target": "saved"})[0], 200)
        self.assertEqual(
            self.http_call(
                "/verify", {"module": "Demo", "target": "saved", "derived_from": REFERENCE}
            )[0],
            400,
        )
        for path in ("/ledger", "/ledger/42", f"/ledger?ref={REFERENCE}", "/ledger?project=demo"):
            with self.subTest(path=path):
                code, out = self.http_call(path, method="GET")
                self.assertEqual(code, 400)
                self.assertIn("log=False", out["error"])
        self.origins.assert_not_called()
        self.references[http_server].assert_not_called()
        self.reads[http_server].assert_not_called()
        self.histories[http_server].assert_not_called()
        self.ledgers[harness].assert_not_called()

    def test_http_project_readonly_id_reference_and_linked_listing(self) -> None:
        code, out = self.http_call("/ledger/42?project=demo", method="GET")
        self.assertEqual((code, out), (200, LOCAL))
        self.reads[http_server].assert_called_once_with(self.project.ledger_path, 42)
        for path in (
            f"/ledger?ref={REFERENCE}&project=demo",
            f"/ledger/ref/{REFERENCE}?project=demo",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.http_call(path, method="GET"), (200, LOCAL))
                self.references[http_server].assert_called_with(self.project.ledger_path, REFERENCE)
        code, out = self.http_call(
            "/ledger?project=demo&status=rejected&tag=paper&limit=1", method="GET"
        )
        self.assertEqual(code, 200)
        self.assertEqual(out["rows"], [LINKED])
        self.assertEqual(out["stats"]["total"], 2)
        self.ledgers[harness].assert_not_called()

    def test_http_bad_project_and_reference_parameters_are_not_ignored(self) -> None:
        for path in (
            "/ledger/42?project=",
            "/ledger?project=demo&project=other",
            f"/ledger/42?ref={REFERENCE}",
            f"/ledger?ref={REFERENCE}&limit=1",
            "/health?project=demo",
            "/ledger?project=demo&trust=true",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.http_call(path, method="GET")[0], 400)
        for path, body in (
            ("/verify?project=demo", {"project": "other", "source": SOURCE}),
            ("/verify", {"project": None, "source": SOURCE}),
            ("/verify", {"source": SOURCE, "build": False}),
            ("/verify", {"source": SOURCE, "derived_from": REFERENCE}),
            ("/verify", {"module": "Demo", "target": "saved", "source": SOURCE}),
            ("/verify", {"module": "Demo", "target": "saved", "check_vacuity": False}),
            ("/verify", {"module": "Demo", "target": "saved", "build": "false"}),
            ("/verify", {"module": "Demo", "target": "saved", "derived_from": None}),
            ("/verify", {"module": "Demo", "target": "saved", "require_nontrivial": "false"}),
            ("/verify", {"module": "Demo"}),
        ):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.http_call(path, body)[0], 400)
        self.checkers[harness].assert_not_called()
        self.references[http_server].assert_not_called()

    def test_http_standalone_source_behavior_is_unchanged(self) -> None:
        h = harness.Harness(config=self.cfg, log=False)
        self.stack.enter_context(patch.object(http_server, "_harness", h))
        code, out = self.http_call("/verify", {"source": SOURCE})
        self.assertEqual(code, 200)
        self.assertEqual(out["target"], "Demo.saved")
        self.verifiers[harness].return_value.verify.assert_called_once_with(
            SOURCE,
            target=None,
            claim=None,
            require_nontrivial=False,
            check_vacuity=True,
            timeout=None,
        )
        self.checkers[harness].assert_not_called()
        self.select.assert_not_called()
        self.assertEqual(self.http_call("/verify", {"module": "Demo", "target": "saved"})[0], 400)
        self.checkers[harness].assert_not_called()

    def test_http_server_project_option_passes_through(self) -> None:
        factory = self.mock(http_server, "Harness", return_value=self.http_harness)
        server = self.mock(http_server, "ThreadingHTTPServer").return_value
        server.serve_forever.side_effect = KeyboardInterrupt
        with redirect_stdout(io.StringIO()):
            self.assertEqual(http_server.main(["--project", "demo", "--no-warm", "--no-log"]), 0)
        factory.assert_called_once_with(pool_size=2, log=False, tag="http", project="demo")
        server.shutdown.assert_called_once()
        self.pool.return_value.close.assert_called_once()


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProjectInterfaceTests)
    return int(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())

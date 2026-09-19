"""Ledger interface routing and read-only retrieval, without Lean, a database or network."""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nullius import cli, harness, http_server, mcp_server  # noqa: E402
from nullius import search as S  # noqa: E402
from nullius.config import Config, ConfigError  # noqa: E402
from nullius.ledger import LedgerReadError  # noqa: E402

CORE_SEARCH = S.search
ROW = {
    "id": 7,
    "status": "rejected",
    "verified": 0,
    "target": "saved",
    "source": "-- saved source π\ntheorem saved : True := by trivial\n",
    "created_iso": "2026-01-01T00:00:00",
    "claim": "saved claim",
    "failures": '["hypotheses_used"]',
}
INVALID_SEARCH_OPTIONS = (
    {"backend": "loogle-remote", "include_ledger": True},
    {"backend": "leansearch", "include_ledger": True},
    {"refresh_ledger": True},
    {"backend": "unknown"},
    {"limit": 0},
)


class LedgerInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.cfg = Config(
            lean_dir=Path("lean"),
            repl_bin=Path("unused-repl"),
            lake_bin=Path("unused-lake"),
            ledger_path=Path("never-open-ledger.sqlite3"),
        )
        self.library = S.SearchResult(
            "needle", "loogle", [S.Hit("library.result", signature="True", source="loogle")]
        )
        self.search = self.mock(S, "search", return_value=[self.library])
        self.close_sessions = self.mock(S, "close_sessions")
        self.local_session = self.mock(
            S, "local_loogle_session", side_effect=AssertionError("unexpected Loogle session")
        )
        self.discover = self.mock(Config, "discover", return_value=self.cfg)
        self.pool = self.mock(harness, "SessionPool").return_value
        self.cli_ledger = self.mock(cli, "Ledger")
        self.mcp_ledger = self.mock(mcp_server, "Ledger")
        self.harness_ledger = self.mock(harness, "Ledger")
        self.cli_read = self.mock(cli, "read_ledger_row", return_value=ROW)
        self.mcp_read = self.mock(mcp_server, "read_ledger_row", return_value=ROW)
        self.http_read = self.mock(http_server, "read_ledger_row", return_value=ROW)
        self.stack.enter_context(patch.object(mcp_server, "_config", self.cfg))
        self.stack.enter_context(patch.object(mcp_server, "_session", None))
        self.stack.enter_context(patch.object(mcp_server, "_ledger", None))
        self.http_harness = self.mock(http_server, "_harness")
        self.http_harness.config = self.cfg
        self.http_harness.search.return_value = [self.library]
        self.http_harness.ledger = Mock()
        for name in ("sqlite3.connect", "subprocess.Popen", "urllib.request.urlopen"):
            self.stack.enter_context(
                patch(name, side_effect=AssertionError(f"unexpected external operation: {name}"))
            )

    def mock(self, owner, name, **kwargs) -> Mock:
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
        send = Mock()
        handler._send = send
        if method == "GET":
            handler.do_GET()
        else:
            handler.do_POST()
        send.assert_called_once()
        return send.call_args.args

    def use_core_search(self) -> list[Mock]:
        self.search.side_effect = CORE_SEARCH
        backends = [
            self.mock(S, name, return_value=self.library)
            for name in ("loogle", "loogle_remote", "leansearch")
        ]
        backends.append(self.stack.enter_context(patch("nullius.ledger_search.search_ledger")))
        return backends

    def test_cli_default_search_does_not_enable_ledger(self) -> None:
        code, out, err = self.cli_call("search", "needle")
        self.assertEqual(code, 0)
        self.assertIn("library.result", out)
        self.assertFalse(err)
        self.search.assert_called_once_with(
            "needle", limit=8, backend="both", include_ledger=False, refresh_ledger=False
        )
        self.close_sessions.assert_called_once_with()
        self.local_session.assert_not_called()
        self.cli_ledger.assert_not_called()
        self.cli_read.assert_not_called()

    def test_mcp_ledger_failure_preserves_library_results(self) -> None:
        self.search.return_value = [
            self.library,
            S.SearchResult("needle", "loogle-ledger", error="refresh required"),
        ]
        response = self.mcp_call("search", {"query": "needle", "include_ledger": True})
        self.assertTrue(response["isError"])
        self.assertIn("library.result", response["content"][0]["text"])
        self.assertIn("refresh required", response["content"][0]["text"])

    def test_cli_search_flags_and_json(self) -> None:
        code, out, _ = self.cli_call(
            "search", "needle", "-b", "ledger", "--refresh-ledger", "--json"
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [self.library.to_dict()])
        self.search.assert_called_with(
            "needle", limit=8, backend="ledger", include_ledger=False, refresh_ledger=True
        )
        self.cli_call("search", "needle", "-b", "loogle", "--include-ledger", "--refresh-ledger")
        self.search.assert_called_with(
            "needle", limit=8, backend="loogle", include_ledger=True, refresh_ledger=True
        )

    def test_cli_ledger_failure_preserves_library_results(self) -> None:
        self.search.return_value = [
            self.library,
            S.SearchResult("needle", "loogle-ledger", error="refresh required"),
        ]
        for extra in ((), ("--json",)):
            with self.subTest(extra=extra):
                code, out, _ = self.cli_call("search", "needle", "--include-ledger", *extra)
                self.assertEqual(code, 1)
                self.assertIn("library.result", out)
                self.assertIn("refresh required", out)
        self.search.return_value = [S.SearchResult("needle", "loogle", error="not installed")]
        self.assertEqual(self.cli_call("search", "needle")[0], 0)
        self.search.return_value = [S.SearchResult("needle", "loogle-ledger", error="unreadable")]
        self.assertEqual(self.cli_call("search", "needle", "-b", "ledger")[0], 1)

    def test_cli_search_closes_on_errors(self) -> None:
        for error, expected in (
            (ValueError("invalid combination"), 2),
            (ConfigError("missing config"), 2),
            (KeyboardInterrupt(), 130),
        ):
            with self.subTest(error=type(error).__name__):
                self.search.side_effect = error
                self.close_sessions.reset_mock()
                code, _, err = self.cli_call("search", "needle")
                self.assertEqual(code, expected)
                self.assertNotIn("Traceback", err)
                self.close_sessions.assert_called_once_with()
        self.search.side_effect = RuntimeError("unexpected failure")
        self.close_sessions.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
            self.cli_call("search", "needle")
        self.close_sessions.assert_called_once_with()

    def test_cli_invalid_search_options_use_core_validation(self) -> None:
        backends = self.use_core_search()
        for args in (
            ("-b", "loogle-remote", "--include-ledger"),
            ("-b", "leansearch", "--include-ledger"),
            ("--refresh-ledger",),
            ("--limit", "0"),
        ):
            with self.subTest(args=args):
                code, _, err = self.cli_call("search", "needle", *args)
                self.assertEqual(code, 2)
                self.assertIn("error:", err)
                self.assertNotIn("Traceback", err)
        for backend in backends:
            backend.assert_not_called()

    def test_harness_search_config_and_positional_compatibility(self) -> None:
        h = harness.Harness(config=self.cfg)
        self.harness_ledger.reset_mock()
        self.assertEqual(h.search("needle", "loogle", 3), [self.library])
        self.search.assert_called_with(
            "needle",
            limit=3,
            backend="loogle",
            include_ledger=False,
            refresh_ledger=False,
            config=self.cfg,
        )
        h.search("needle", include_ledger=True, refresh_ledger=True)
        self.search.assert_called_with(
            "needle",
            limit=8,
            backend="both",
            include_ledger=True,
            refresh_ledger=True,
            config=self.cfg,
        )
        self.harness_ledger.assert_not_called()
        h.close()
        self.pool.close.assert_called_once_with()
        self.close_sessions.assert_called_once_with(self.cfg)
        self.local_session.assert_not_called()

    def test_no_log_rejects_every_explicit_ledger_search(self) -> None:
        h = harness.Harness(config=self.cfg, log=False)
        h.search("needle")
        self.search.reset_mock()
        for kwargs in (
            {"backend": "ledger"},
            {"include_ledger": True},
            {"refresh_ledger": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "log=False"):
                h.search("needle", **kwargs)
        self.search.assert_not_called()
        self.harness_ledger.assert_not_called()

    def test_harness_cleanup_even_when_pool_close_fails(self) -> None:
        h = harness.Harness(config=self.cfg, log=False)
        self.pool.close.side_effect = RuntimeError("pool close failed")
        with self.assertRaisesRegex(RuntimeError, "pool close failed"):
            h.close()
        self.close_sessions.assert_called_once_with(self.cfg)

    def test_mcp_schema_still_has_five_tools(self) -> None:
        tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
        self.assertEqual(set(tools), {"verify", "statement", "search", "close", "log"})
        props = tools["search"]["inputSchema"]["properties"]
        self.assertEqual(props["backend"]["enum"], list(S.BACKENDS))
        for name in ("include_ledger", "refresh_ledger"):
            self.assertEqual(props[name]["type"], "boolean")
            self.assertIs(props[name]["default"], False)
        self.assertEqual(tools["log"]["inputSchema"]["properties"]["id"]["minimum"], 1)

    def test_mcp_search_routes_flags_and_config_without_ledger_reads(self) -> None:
        for extra in ({}, {"include_ledger": True, "refresh_ledger": True}, {"backend": "ledger"}):
            with self.subTest(extra=extra):
                response = self.mcp_call("search", {"query": "needle", **extra})
                self.assertFalse(response["isError"])
                self.search.assert_called_with(
                    "needle",
                    limit=8,
                    backend=extra.get("backend", "both"),
                    include_ledger=extra.get("include_ledger", False),
                    refresh_ledger=extra.get("refresh_ledger", False),
                    config=self.cfg,
                )
        self.mcp_ledger.assert_not_called()
        self.mcp_read.assert_not_called()
        self.close_sessions.assert_not_called()

    def test_mcp_invalid_search_options_are_tool_errors(self) -> None:
        backends = self.use_core_search()
        for options in INVALID_SEARCH_OPTIONS:
            with self.subTest(options=options):
                response = self.mcp_call("search", {"query": "needle", **options})
                self.assertTrue(response["isError"])
                self.assertNotIn("Traceback", response["content"][0]["text"])
        for backend in backends:
            backend.assert_not_called()

    def test_mcp_remote_search_does_not_require_local_configuration(self) -> None:
        self.stack.enter_context(patch.object(mcp_server, "_config", None))
        self.use_core_search()
        for backend in ("loogle-remote", "leansearch"):
            with self.subTest(backend=backend):
                response = self.mcp_call("search", {"query": "needle", "backend": backend})
                self.assertFalse(response["isError"])
        self.discover.assert_not_called()
        self.mcp_ledger.assert_not_called()
        self.mcp_read.assert_not_called()
        self.local_session.assert_not_called()

    def test_cli_readonly_source_retrieval_human_and_json(self) -> None:
        code, out, err = self.cli_call("log", "--id", "7")
        self.assertEqual(code, 0)
        self.assertFalse(err)
        self.assertIn("target: saved", out)
        self.assertIn(ROW["source"], out)
        self.assertIn("rejected", out)
        self.cli_read.assert_called_with(self.cfg.ledger_path, 7)
        code, out, _ = self.cli_call("log", "--id", "7", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), ROW)
        self.cli_ledger.assert_not_called()

    def test_cli_rejects_invalid_ids_and_conflicting_options_before_reading(self) -> None:
        cases = [("--id", value) for value in ("0", "-1", "abc", "1.5")]
        cases += [
            ("--id", "7", *extra)
            for extra in (
                ("--limit", "20"),
                ("-n", "1"),
                ("--stats",),
                ("--recall", ""),
                ("--tag", ""),
                ("--status", "verified"),
            )
        ]
        for args in cases:
            with self.subTest(args=args):
                code, _, err = self.cli_call("log", *args)
                self.assertEqual(code, 2)
                self.assertNotIn("Traceback", err)
        self.cli_read.assert_not_called()
        self.cli_ledger.assert_not_called()
        self.discover.assert_not_called()

    def test_cli_readonly_missing_and_database_error(self) -> None:
        self.cli_read.return_value = None
        code, _, err = self.cli_call("log", "--id", "7")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)
        self.cli_read.side_effect = LedgerReadError("malformed database")
        code, _, err = self.cli_call("log", "--id", "7")
        self.assertEqual(code, 1)
        self.assertIn("malformed database", err)
        self.assertNotIn("Traceback", err)
        self.cli_ledger.assert_not_called()

    def test_cli_listing_default_is_unchanged(self) -> None:
        self.cli_ledger.return_value.recent.return_value = [ROW]
        code, out, _ = self.cli_call("log", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [ROW])
        self.cli_ledger.return_value.recent.assert_called_once_with(limit=20, status=None, tag=None)
        self.cli_read.assert_not_called()

    def test_mcp_readonly_source_retrieval(self) -> None:
        response = self.mcp_call("log", {"id": 7})
        self.assertFalse(response["isError"])
        self.assertEqual(json.loads(response["content"][0]["text"]), ROW)
        self.mcp_read.assert_called_once_with(self.cfg.ledger_path, 7)
        self.mcp_ledger.assert_not_called()

    def test_mcp_rejects_invalid_ids_and_listing_conflicts(self) -> None:
        cases = [{"id": value} for value in (0, -1, True, "7", 1.5, None)]
        cases += [
            {"id": 7, key: value}
            for key, value in (
                ("limit", 10),
                ("status", "verified"),
                ("tag", ""),
                ("recall", ""),
                ("stats", False),
            )
        ]
        for args in cases:
            with self.subTest(args=args):
                response = self.mcp_call("log", args)
                self.assertTrue(response["isError"])
                self.assertNotIn("Traceback", response["content"][0]["text"])
        self.mcp_read.assert_not_called()
        self.mcp_ledger.assert_not_called()

    def test_mcp_missing_rows_and_database_errors_are_explicit(self) -> None:
        self.mcp_read.return_value = None
        response = self.mcp_call("log", {"id": 7})
        self.assertTrue(response["isError"])
        self.assertIn("not found", response["content"][0]["text"])
        self.mcp_read.side_effect = LedgerReadError("malformed database")
        response = self.mcp_call("log", {"id": 7})
        self.assertTrue(response["isError"])
        self.assertIn("malformed database", response["content"][0]["text"])
        self.mcp_ledger.assert_not_called()

    def test_mcp_listing_is_unchanged(self) -> None:
        self.mcp_ledger.return_value.recent.return_value = [ROW]
        response = self.mcp_call("log", {})
        self.assertFalse(response["isError"])
        self.assertIn("#7", response["content"][0]["text"])
        self.mcp_ledger.return_value.recent.assert_called_once_with(limit=10, status=None, tag=None)
        self.mcp_read.assert_not_called()

    def test_http_search_routes_flags(self) -> None:
        for extra in ({}, {"include_ledger": True, "refresh_ledger": True}, {"backend": "ledger"}):
            with self.subTest(extra=extra):
                code, out = self.http_call("/search", {"query": "needle", **extra})
                self.assertEqual(code, 200)
                self.assertEqual(out, {"results": [self.library.to_dict()]})
                self.http_harness.search.assert_called_with(
                    "needle",
                    limit=8,
                    backend=extra.get("backend", "both"),
                    include_ledger=extra.get("include_ledger", False),
                    refresh_ledger=extra.get("refresh_ledger", False),
                )
        self.http_read.assert_not_called()

    def test_http_search_core_validation_and_config_routing(self) -> None:
        h = harness.Harness(config=self.cfg)
        self.stack.enter_context(patch.object(http_server, "_harness", h))
        backends = self.use_core_search()
        code, _ = self.http_call("/search", {"query": "needle"})
        self.assertEqual(code, 200)
        backends[0].assert_called_once_with("needle", limit=8, timeout=20.0, config=self.cfg)
        backends[3].assert_not_called()
        for options in (*INVALID_SEARCH_OPTIONS, {"limit": None}, {"limit": "bad"}):
            with self.subTest(options=options):
                code, out = self.http_call("/search", {"query": "needle", **options})
                self.assertEqual(code, 400)
                self.assertIn("error", out)
        self.http_read.assert_not_called()

    def test_http_no_log_prevents_explicit_access_and_reads(self) -> None:
        h = harness.Harness(config=self.cfg, log=False)
        self.stack.enter_context(patch.object(http_server, "_harness", h))
        self.assertEqual(self.http_call("/search", {"query": "needle"})[0], 200)
        self.search.reset_mock()
        for extra in ({"backend": "ledger"}, {"include_ledger": True}, {"refresh_ledger": True}):
            with self.subTest(extra=extra):
                code, out = self.http_call("/search", {"query": "needle", **extra})
                self.assertEqual(code, 400)
                self.assertIn("log=False", out["error"])
        code, out = self.http_call("/ledger/7", method="GET")
        self.assertEqual(code, 400)
        self.assertIn("log=False", out["error"])
        self.assertEqual(
            self.http_call("/ledger", method="GET"), (200, {"rows": [], "stats": None})
        )
        self.search.assert_not_called()
        self.http_read.assert_not_called()
        self.harness_ledger.assert_not_called()

    def test_http_retrieval_and_explicit_database_errors(self) -> None:
        self.assertEqual(self.http_call("/ledger/7", method="GET"), (200, ROW))
        self.http_read.assert_called_once_with(self.cfg.ledger_path, 7)
        self.http_read.return_value = None
        self.assertEqual(self.http_call("/ledger/7", method="GET")[0], 404)
        self.http_read.side_effect = LedgerReadError("unreadable database")
        code, out = self.http_call("/ledger/7", method="GET")
        self.assertEqual(code, 500)
        self.assertIn("unreadable database", out["error"])
        self.http_harness.ledger.get.assert_not_called()

    def test_http_invalid_ids_and_request_body(self) -> None:
        for row_id in ("", "0", "-1", "+1", "abc", "1.5", "7/", "７"):
            with self.subTest(row_id=row_id):
                self.assertEqual(self.http_call(f"/ledger/{row_id}", method="GET")[0], 400)
        self.http_read.assert_not_called()
        for body in ([], True, "not an object"):
            with self.subTest(body=body):
                self.assertEqual(self.http_call("/search", body)[0], 400)
        self.http_harness.search.assert_not_called()

    def test_mcp_cleanup_does_not_create_a_session_or_config(self) -> None:
        self.stack.enter_context(patch.object(mcp_server, "_config", None))
        self.assertEqual(mcp_server.serve(io.StringIO(""), io.StringIO()), 0)
        self.close_sessions.assert_called_once_with(None)
        self.discover.assert_not_called()
        self.local_session.assert_not_called()
        self.mcp_ledger.assert_not_called()

    def test_mcp_cleanup_after_output_failure(self) -> None:
        session = Mock()
        self.stack.enter_context(patch.object(mcp_server, "_session", session))
        out = Mock()
        out.write.side_effect = OSError("output closed")
        with self.assertRaisesRegex(OSError, "output closed"):
            mcp_server.serve(io.StringIO('{"id":1,"method":"ping"}\n'), out)
        session.close.assert_called_once_with()
        self.close_sessions.assert_called_once_with(self.cfg)

    def test_mcp_cleanup_after_session_close_failure(self) -> None:
        session = Mock()
        session.close.side_effect = RuntimeError("session close failed")
        self.stack.enter_context(patch.object(mcp_server, "_session", session))
        with self.assertRaisesRegex(RuntimeError, "session close failed"):
            mcp_server.serve(io.StringIO(""), io.StringIO())
        self.close_sessions.assert_called_once_with(self.cfg)


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LedgerInterfaceTests)
    return int(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())

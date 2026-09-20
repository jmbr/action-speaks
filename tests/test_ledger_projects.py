"""Stable ledger identities and non-destructive project history, using temporary DBs only."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nullius.ledger import (  # noqa: E402
    SCHEMA,
    Ledger,
    LedgerReadError,
    Recollection,
    normalize_statement,
    read_ledger_identity,
    read_ledger_row,
    read_verified_rows,
)
from nullius.ledger_transfer import (  # noqa: E402
    read_entry_reference,
    read_project_rows,
    transfer_entries,
)
from nullius.verify import Check, Verdict  # noqa: E402


def saved_rows(path: Path) -> list[dict]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM verifications ORDER BY id")]


def schema(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()


def fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def directory_state(path: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        file.name: (file.read_bytes(), file.stat().st_mtime_ns, file.stat().st_ctime_ns)
        for file in path.iterdir()
    }


def legacy(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(
            SCHEMA.replace("    physlib_rev   TEXT,\n", "").replace("    cslib_rev     TEXT,\n", "")
        )
        conn.execute(
            "INSERT INTO verifications "
            "(created_at, created_iso, status, verified, target, claim, statement, source, "
            "source_sha256, axioms, checks, failures, toolchain, mathlib_rev, tag) "
            "VALUES (123.5, 'old date', 'verified', 1, 'Original.target', 'old claim', "
            "'  original  statement  ', 'original source', 'original sha', '[]', '[]', '[]', "
            "'old toolchain', 'old revision', 'history')"
        )


class LedgerProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source.sqlite3"
        self.destination = self.root / "destination.sqlite3"
        self.project_id = str(uuid4())

    def history(self, path: Path | None = None) -> Ledger:
        ledger = Ledger(path or self.source)
        for number, status in enumerate(("verified", "rejected", "error"), 1):
            source = f"-- historical source {number}\n"
            verdict = Verdict(
                status,
                f"Original.target{number}",
                f"historical needle {number}",
                checks=[Check("retained_check", status == "verified", "historical detail")],
                axioms=["retained.axiom"],
                statement=f"historical statement {number}",
                provenance={
                    "toolchain": "historical toolchain",
                    "mathlib_rev": "historical mathlib",
                    "physlib_rev": "historical physlib",
                    "cslib_rev": "historical cslib",
                    "project_name": "original name",
                    "root": "/original/project",
                    "extra": "retained extra provenance",
                },
                source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                elapsed=1.25,
            )
            ledger.record(
                verdict,
                source if number != 2 else "",
                tag="history",
                record_kind="project" if number == 2 else "snippet",
                project_id=self.project_id if number == 2 else None,
                module="Original.Module" if number == 2 else None,
                environment_id="old environment" if number == 2 else None,
                derived_from=f"{ledger.identity()['ledger_uuid']}:1" if number == 2 else None,
            )
        return ledger

    def assert_payload(self, originals: list[dict], copies: list[dict]) -> None:
        self.assertEqual(len(originals), len(copies))
        for original, copy in zip(originals, copies):
            for key, value in original.items():
                if key not in ("id", "origin_ledger_uuid", "origin_row_id"):
                    self.assertEqual(value, copy[key], key)

    def test_identity_binding_and_rename(self) -> None:
        ledger = self.history()
        identity = ledger.identity()
        self.assertEqual(str(UUID(identity["ledger_uuid"])), identity["ledger_uuid"])
        self.assertIsNone(identity["project_id"])
        self.assertEqual(Ledger(self.source).identity(), identity)
        before = saved_rows(self.source)
        ledger.bind_project(self.project_id)
        ledger.bind_project(self.project_id.upper())
        with self.assertRaisesRegex(ValueError, "already bound"):
            ledger.bind_project(str(uuid4()))
        for invalid in ("not a uuid", "", None, True, 4):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ledger.bind_project(invalid)
        renamed = self.root / "renamed.sqlite3"
        self.source.rename(renamed)
        self.assertEqual(Ledger(renamed).identity()["ledger_uuid"], identity["ledger_uuid"])
        self.assertEqual(read_ledger_identity(renamed)["project_id"], self.project_id)
        self.assertEqual(saved_rows(renamed), before)
        row = read_ledger_row(renamed, 1)
        self.assertEqual(row["origin"], f"{identity['ledger_uuid']}:1")
        self.assertEqual(row["reference"], row["origin"])
        self.assertEqual(read_verified_rows(renamed)[0]["reference"], row["reference"])
        self.assertEqual(
            read_entry_reference(renamed, row["origin"])["source"], before[0]["source"]
        )

    def test_concurrent_new_and_legacy_initialization(self) -> None:
        for existing in (False, True):
            path = self.root / f"concurrent-{existing}.sqlite3"
            if existing:
                legacy(path)
            barrier = Barrier(6)

            def open_ledger(_: int) -> str:
                barrier.wait(timeout=10)
                return Ledger(path).identity()["ledger_uuid"]

            with ThreadPoolExecutor(max_workers=6) as pool:
                identities = list(pool.map(open_ledger, range(6)))
            self.assertEqual(len(set(identities)), 1)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM ledger_metadata").fetchone()[0], 1
                )

    def test_concurrent_project_binding_never_overwrites(self) -> None:
        ledger = Ledger(self.source)
        barrier = Barrier(2)
        projects = [str(uuid4()), str(uuid4())]

        def bind(project_id: str) -> str | None:
            barrier.wait(timeout=10)
            try:
                ledger.bind_project(project_id)
            except ValueError:
                return None
            return project_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            successes = [result for result in pool.map(bind, projects) if result is not None]
        self.assertEqual(len(successes), 1)
        self.assertEqual(ledger.identity()["project_id"], successes[0])

    def test_legacy_readonly_and_additive_migration(self) -> None:
        self.assertIsNone(read_ledger_identity(self.source))
        self.assertEqual(read_project_rows(self.source), [])
        self.assertIsNone(read_entry_reference(self.source, f"{uuid4()}:1"))
        self.assertFalse(self.source.exists())
        legacy(self.source)
        before, original_schema, original = (
            fingerprint(self.source),
            schema(self.source),
            saved_rows(self.source),
        )
        self.assertEqual(
            read_ledger_identity(self.source), {"ledger_uuid": None, "project_id": None}
        )
        self.assertNotIn("ledger_uuid", read_ledger_row(self.source, 1))
        self.assertEqual(read_verified_rows(self.source), original)
        self.assertEqual(read_project_rows(self.source)[0]["source"], "original source")
        self.assertEqual(fingerprint(self.source), before)
        self.assertEqual(schema(self.source), original_schema)
        identity = Ledger(self.source).identity()
        migrated = saved_rows(self.source)[0]
        for key, value in original[0].items():
            self.assertEqual(migrated[key], value)
        self.assertEqual(migrated["record_kind"], "snippet")
        self.assertIsNone(migrated["provenance"])
        self.assertEqual(migrated["statement_norm"], normalize_statement(migrated["statement"]))
        self.assertIsNone(migrated["origin_row_id"])
        self.assertEqual(
            read_verified_rows(self.source)[0]["origin"], f"{identity['ledger_uuid']}:1"
        )

    def test_readonly_access_never_creates_or_modifies_sidecars(self) -> None:
        ledger = self.history()
        identity = ledger.identity()
        before = directory_state(self.root)
        for _ in range(2):
            self.assertEqual(read_ledger_identity(self.source), identity)
            self.assertEqual(len(read_verified_rows(self.source)), 1)
            self.assertEqual(read_ledger_row(self.source, 2)["record_kind"], "project")
            self.assertEqual(len(read_project_rows(self.source)), 1)
            self.assertIsNotNone(read_entry_reference(self.source, f"{identity['ledger_uuid']}:2"))
            transfer_entries(self.source, self.destination, ids=[1], dry_run=True)
            self.assertEqual(directory_state(self.root), before)

    def test_readonly_access_sees_committed_active_wal_without_mutating_it(self) -> None:
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE keepalive (value TEXT)")
            ledger = self.history()
            ledger.bind_project(self.project_id)
            identity = ledger.identity()
            self.assertGreater(Path(str(self.source) + "-wal").stat().st_size, 0)
            alias = self.root / "alias.sqlite3"
            alias.symlink_to(self.source)
            before = directory_state(self.root)
            for path in (self.source, alias):
                self.assertEqual(read_ledger_identity(path), identity)
                self.assertEqual(len(read_verified_rows(path)), 1)
                self.assertEqual(read_ledger_row(path, 2)["record_kind"], "project")
                self.assertEqual(len(read_project_rows(path, verified_only=False)), 3)
                transfer_entries(path, self.destination, tag="history", dry_run=True)
                self.assertEqual(directory_state(self.root), before)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE ledger_metadata SET project_id = ?", (str(uuid4()),))
            before_uncommitted = directory_state(self.root)
            self.assertEqual(read_ledger_identity(self.source), identity)
            self.assertEqual(directory_state(self.root), before_uncommitted)
            writer.rollback()

    def test_readonly_access_recovers_only_private_rollback_journal(self) -> None:
        legacy(self.source)
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA cache_size=1")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE verifications SET source = hex(randomblob(100000))")
            self.assertTrue(Path(str(self.source) + "-journal").exists())
            before = directory_state(self.root)
            self.assertEqual(read_ledger_row(self.source, 1)["source"], "original source")
            self.assertEqual(directory_state(self.root), before)
            writer.rollback()

    def test_snapshot_retries_concurrent_file_changes(self) -> None:
        ledger = self.history()
        copyfile = shutil.copyfile
        changed = False

        def copy_and_change(source: Path, destination: Path) -> Path:
            nonlocal changed
            result = copyfile(source, destination)
            if source == self.source and not changed:
                changed = True
                ledger.record(Verdict("verified", "new", "new"), "new")
            return result

        with patch("nullius.ledger.shutil.copyfile", side_effect=copy_and_change):
            rows = read_verified_rows(self.source)
        self.assertTrue(changed)
        self.assertEqual([row["id"] for row in rows], [1, 4])

    def test_snapshot_repeated_changes_fail_explicitly(self) -> None:
        ledger = self.history()
        copyfile = shutil.copyfile

        def copy_and_change(source: Path, destination: Path) -> Path:
            result = copyfile(source, destination)
            if source == self.source:
                ledger.record(Verdict("verified", "new", "new"), "new")
            return result

        with (
            patch("nullius.ledger.shutil.copyfile", side_effect=copy_and_change),
            self.assertRaisesRegex(LedgerReadError, "changed repeatedly"),
        ):
            read_verified_rows(self.source)

    def test_malformed_identity_errors(self) -> None:
        self.source.write_text("not sqlite")
        with self.assertRaises(LedgerReadError):
            read_ledger_identity(self.source)
        with self.assertRaises(LedgerReadError):
            read_ledger_identity(self.root)
        loop = self.root / "symlink-loop"
        loop.symlink_to(loop)
        with self.assertRaises(LedgerReadError):
            read_ledger_identity(loop)
        empty = self.root / "empty.sqlite3"
        empty.touch()
        with self.assertRaises(LedgerReadError):
            read_ledger_identity(empty)
        for number, bad_sql in enumerate(
            (
                "UPDATE ledger_metadata SET ledger_uuid = 'invalid'",
                "UPDATE ledger_metadata SET project_id = 'invalid'",
                "DELETE FROM ledger_metadata",
                "DROP TABLE ledger_metadata; CREATE TABLE ledger_metadata (bad TEXT)",
            )
        ):
            with self.subTest(bad_sql=bad_sql):
                path = self.root / f"malformed-{number}.sqlite3"
                Ledger(path)
                with closing(sqlite3.connect(path)) as conn, conn:
                    for command in bad_sql.split(";"):
                        conn.execute(command)
                with self.assertRaises(LedgerReadError):
                    read_ledger_identity(path)

    def test_record_project_fields_and_validation(self) -> None:
        ledger = self.history()
        rows = saved_rows(self.source)
        self.assertEqual(rows[1]["record_kind"], "project")
        self.assertEqual(rows[1]["source"], "")
        self.assertEqual(rows[1]["module"], "Original.Module")
        self.assertEqual(json.loads(rows[1]["provenance"])["extra"], "retained extra provenance")
        verdict = Verdict("verified", "main", "claim")
        for keywords in (
            {"record_kind": "unknown"},
            {"record_kind": "project"},
            {"record_kind": "project", "project_id": self.project_id},
            {
                "record_kind": "project",
                "project_id": self.project_id,
                "module": "Original.Module",
                "environment_id": "",
            },
            {"project_id": "not a uuid"},
            {"derived_from": f"{uuid4()}:0"},
            {"derived_from": "local:1"},
        ):
            with self.subTest(keywords=keywords), self.assertRaises(ValueError):
                ledger.record(verdict, "", **keywords)
        self.assertEqual(saved_rows(self.source), rows)

    def test_failed_project_attempts_can_omit_environment(self) -> None:
        ledger = Ledger(self.source)
        for status in ("rejected", "error"):
            verdict = Verdict(
                status,
                "Original.target",
                "attempt before environment discovery",
                checks=[Check("project_environment", False, "missing build dependencies")],
                provenance={"project_id": self.project_id, "module": "Original.Module"},
            )
            row_id = ledger.record(
                verdict,
                "",
                tag="early-failure",
                record_kind="project",
                project_id=self.project_id,
                module="Original.Module",
            )
            row = read_ledger_row(self.source, row_id)
            self.assertEqual(row["status"], status)
            self.assertEqual(row["verified"], 0)
            self.assertIsNone(row["environment_id"])
            self.assertEqual(json.loads(row["provenance"]), verdict.provenance)
            self.assertEqual(json.loads(row["failures"]), ["project_environment"])
            for project_id, module in ((None, "Original.Module"), (self.project_id, None)):
                with self.assertRaises(ValueError):
                    ledger.record(
                        verdict, "", record_kind="project", project_id=project_id, module=module
                    )
        for environment_id in (None, "", " "):
            with self.subTest(environment_id=environment_id), self.assertRaises(ValueError):
                ledger.record(
                    Verdict("verified", "Original.target", "successful attempt"),
                    "",
                    record_kind="project",
                    project_id=self.project_id,
                    module="Original.Module",
                    environment_id=environment_id,
                )
        original = saved_rows(self.source)
        self.assertEqual(len(original), 2)
        for mode in ("link", "import"):
            destination = self.root / f"early-failure-{mode}.sqlite3"
            transfer_entries(self.source, destination, tag="early-failure", mode=mode)
            self.assert_payload(original, read_project_rows(destination, verified_only=False))

    def test_project_recollection_renders_target_context_not_source_instructions(self) -> None:
        ledger = Ledger(self.source)
        verdict = Verdict(
            "verified",
            "Original.target",
            "project recollection needle",
            statement="historical project statement text",
            provenance={"mathlib_rev": "same-library-revision"},
        )
        row_id = ledger.record(
            verdict,
            "",
            record_kind="project",
            project_id=self.project_id,
            module="Original.Module",
            environment_id="historical-environment",
        )
        hit = ledger.recall(
            statement=verdict.statement,
            current={
                "mathlib_rev": "same-library-revision",
                "environment_id": "different-environment",
            },
        )[0]
        rendered = hit.render()
        for context in (
            self.project_id,
            "Original.Module",
            "Original.target",
            "historical-environment",
            f"{ledger.identity()['ledger_uuid']}:{row_id}",
            "verified (historical)",
            "project context may differ",
            "not a current proof",
            "Recheck module Original.Module target Original.target",
        ):
            self.assertIn(context, rendered)
        self.assertNotIn("these exact revisions", rendered)
        self.assertNotIn("re-verify the recorded source", rendered)
        fuzzy = ledger.recall(text="recollection needle")[0]
        self.assertEqual(fuzzy.kind, "text")
        self.assertIn("candidate only", fuzzy.advice())
        self.assertIn("not evidence", fuzzy.advice())
        self.assertIn("Recheck module", fuzzy.advice())
        copied = self.root / "recalled-copy.sqlite3"
        transfer_entries(self.source, copied, ids=[row_id], mode="import")
        copied_hit = Ledger(copied).recall(statement=verdict.statement)[0]
        self.assertEqual(copied_hit.row["reference"], hit.row["reference"])
        self.assertIn(hit.row["reference"], copied_hit.render())
        stale = Recollection(hit.row, "statement", ["mathlib"])
        self.assertIn("Recorded library revisions differ (mathlib)", stale.advice())

    def test_project_failure_recollection_keeps_rejection_scope(self) -> None:
        ledger = self.history()
        row = read_ledger_row(self.source, 2)
        attempt = Recollection(row, "statement", [])
        self.assertIn("ATTEMPT failed", attempt.advice())
        self.assertIn("Recheck module Original.Module", attempt.advice())
        statement = Recollection({**row, "failures": '["not_vacuous"]'}, "statement", [])
        self.assertIn("recorded project STATEMENT was rejected", statement.advice())
        self.assertIn("different project environment", statement.advice())
        self.assertNotIn("recorded source", statement.advice())
        snippet = Recollection(read_ledger_row(self.source, 1), "statement", [])
        self.assertIn("these exact revisions", snippet.advice())
        self.assertIn("re-verify the recorded source", snippet.advice())
        self.assertNotIn("    project:", snippet.render())
        self.assertEqual(ledger.stats()["total"], 3)

    def test_link_all_statuses_idempotent_and_verified_filter(self) -> None:
        source = self.history()
        originals, before = saved_rows(self.source), fingerprint(self.source)
        result = transfer_entries(self.source, self.destination, tag="history")
        self.assertEqual((result["count"], result["added"]), (3, 3))
        self.assertEqual(saved_rows(self.destination), [])
        linked = read_project_rows(self.destination, verified_only=False)
        self.assert_payload(originals, linked)
        self.assertEqual({r["ledger_uuid"] for r in linked}, {source.identity()["ledger_uuid"]})
        self.assertEqual({r["ledger_path"] for r in linked}, {str(self.source)})
        self.assertEqual(len(read_project_rows(self.destination)), 1)
        self.assertEqual(transfer_entries(self.source, self.destination, ids=[1, 2, 3])["added"], 0)
        self.assertEqual(len(read_project_rows(self.destination, verified_only=False)), 3)
        for row in linked:
            self.assertEqual(row["reference"], row["origin"])
            self.assertEqual(read_entry_reference(self.destination, row["reference"]), row)
        self.assertEqual(fingerprint(self.source), before)
        self.assertEqual(saved_rows(self.source), originals)

    def test_import_preserves_payload_origin_chain_and_recall(self) -> None:
        source = self.history()
        original, before = saved_rows(self.source), fingerprint(self.source)
        destination = Ledger(self.destination)
        destination_project = str(uuid4())
        destination.bind_project(destination_project)
        destination.record(Verdict("verified", "unrelated", "unrelated"), "unrelated")
        destination.recall(text="unrelated")
        result = transfer_entries(self.source, self.destination, ids=[1, 2, 3], mode="import")
        copies = saved_rows(self.destination)[1:]
        self.assertEqual((result["count"], result["added"]), (3, 3))
        self.assert_payload(original, copies)
        self.assertEqual([row["id"] for row in copies], [2, 3, 4])
        self.assertEqual(copies[1]["project_id"], self.project_id)
        self.assertEqual(destination.identity()["project_id"], destination_project)
        copied_row = read_ledger_row(self.destination, 2)
        self.assertEqual(copied_row["reference"], f"{source.identity()['ledger_uuid']}:1")
        self.assertEqual(copied_row["ledger_uuid"], destination.identity()["ledger_uuid"])
        self.assertEqual(
            {row["origin_ledger_uuid"] for row in copies}, {source.identity()["ledger_uuid"]}
        )
        self.assertEqual(
            transfer_entries(self.source, self.destination, tag="history", mode="import")["added"],
            0,
        )
        self.assertTrue(destination.recall(source=original[0]["source"]))
        self.assertTrue(destination.recall(statement=original[0]["statement"]))
        self.assertTrue(destination.recall(text="historical needle"))
        third = self.root / "third.sqlite3"
        transfer_entries(self.destination, third, ids=[2, 3, 4], mode="import")
        third_rows = saved_rows(third)
        self.assert_payload(original, third_rows)
        self.assertEqual(
            [(row["origin_ledger_uuid"], row["origin_row_id"]) for row in copies],
            [(row["origin_ledger_uuid"], row["origin_row_id"]) for row in third_rows],
        )
        for row in read_project_rows(third, verified_only=False):
            self.assertEqual(row["reference"], row["origin"])
            self.assertEqual(row["ledger_uuid"], read_ledger_identity(third)["ledger_uuid"])
            self.assertEqual(read_entry_reference(third, row["reference"]), row)
        local_ref = f"{destination.identity()['ledger_uuid']}:2"
        self.assertEqual(
            read_entry_reference(self.destination, local_ref)["source"], original[0]["source"]
        )
        self.assertEqual(fingerprint(self.source), before)
        self.assertEqual(saved_rows(self.source), original)

    def test_import_deduplicates_native_origin_and_links(self) -> None:
        self.history()
        transfer_entries(self.source, self.destination, tag="history", mode="import")
        self.assertEqual(
            transfer_entries(self.destination, self.source, tag="history", mode="import")["added"],
            0,
        )
        linked = self.root / "linked.sqlite3"
        transfer_entries(self.destination, linked, tag="history")
        for row in read_project_rows(linked, verified_only=False):
            self.assertEqual(
                row["ledger_uuid"], read_ledger_identity(self.destination)["ledger_uuid"]
            )
            self.assertEqual(
                row["reference"],
                f"{read_ledger_identity(self.source)['ledger_uuid']}:{row['origin_row_id']}",
            )
            self.assertEqual(read_entry_reference(linked, row["reference"]), row)
        self.assertEqual(transfer_entries(self.source, linked, tag="history")["added"], 0)
        transfer_entries(self.source, linked, tag="history", mode="import")
        self.assertEqual(len(read_project_rows(linked, verified_only=False)), 3)
        self.assertEqual(saved_rows(self.source)[0]["origin_ledger_uuid"], None)

    def test_legacy_transfer_only_assigns_source_uuid(self) -> None:
        legacy(self.source)
        original = saved_rows(self.source)
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("CREATE TABLE projects (name TEXT)")
            conn.execute("CREATE TABLE snapshots (payload TEXT)")
            conn.execute("INSERT INTO projects VALUES ('old project name')")
            conn.execute("INSERT INTO snapshots VALUES ('private snapshot')")
        old_schema = schema(self.source)
        result = transfer_entries(self.source, self.destination, ids=[1], mode="import")
        self.assertEqual(result["count"], 1)
        self.assertEqual(saved_rows(self.source), original)
        self.assertEqual(
            [entry for entry in schema(self.source) if entry[0] != "ledger_metadata"], old_schema
        )
        self.assertEqual(
            saved_rows(self.destination)[0]["origin_ledger_uuid"],
            read_ledger_identity(self.source)["ledger_uuid"],
        )
        self.assertFalse(
            {"projects", "snapshots"} & {entry[0] for entry in schema(self.destination)}
        )

    def test_dry_run_no_writes_and_legacy_uuid_preview(self) -> None:
        legacy(self.source)
        legacy(self.destination)
        source_before = fingerprint(self.source), schema(self.source)
        destination_before = fingerprint(self.destination), schema(self.destination)
        absent = self.root / "absent-directory" / "new.sqlite3"
        for destination in (self.destination, absent):
            for mode in ("link", "import"):
                preview = transfer_entries(
                    self.source, destination, ids=[1], mode=mode, dry_run=True
                )
                self.assertEqual(preview["count"], 1)
                self.assertEqual(preview["action"], f"would-{mode}")
                self.assertEqual(preview["origins"], [None])
                self.assertTrue(preview["would_assign_uuid"])
                self.assertIn("would-assign-UUID: source", preview["notes"])
                self.assertFalse(absent.parent.exists())
        self.assertEqual((fingerprint(self.source), schema(self.source)), source_before)
        self.assertEqual(
            (fingerprint(self.destination), schema(self.destination)), destination_before
        )

    def test_identified_dry_run_preserves_rows_and_known_origins(self) -> None:
        source = self.history()
        destination = Ledger(self.destination)
        source_before, destination_before = fingerprint(self.source), fingerprint(self.destination)
        preview = transfer_entries(
            self.source, self.destination, ids=[2], mode="import", dry_run=True
        )
        self.assertEqual(preview["origins"], [f"{source.identity()['ledger_uuid']}:2"])
        self.assertEqual(preview["rows"][0]["status"], "rejected")
        self.assertFalse(preview["would_assign_uuid"])
        self.assertEqual(preview["destination_identity"], destination.identity())
        self.assertEqual(fingerprint(self.source), source_before)
        self.assertEqual(fingerprint(self.destination), destination_before)
        self.assertEqual(saved_rows(self.destination), [])

    def test_missing_selection_does_not_assign_legacy_uuid(self) -> None:
        legacy(self.source)
        before = fingerprint(self.source), schema(self.source)
        for dry_run in (False, True):
            for options in ({"ids": [1, 2]}, {"tag": "absent"}):
                with self.assertRaises(ValueError):
                    transfer_entries(self.source, self.destination, dry_run=dry_run, **options)
                self.assertFalse(self.destination.exists())
        self.assertEqual((fingerprint(self.source), schema(self.source)), before)

    def test_concurrent_transfers_are_idempotent(self) -> None:
        legacy(self.source)
        for mode in ("link", "import"):
            destination = self.root / f"concurrent-{mode}.sqlite3"
            barrier = Barrier(6)

            def transfer(_: int) -> dict:
                barrier.wait(timeout=10)
                return transfer_entries(self.source, destination, ids=[1], mode=mode)

            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(transfer, range(6)))
            self.assertEqual(sum(result["added"] for result in results), 1)
            self.assertEqual(len({result["origins"][0] for result in results}), 1)
            self.assertEqual(len(read_project_rows(destination)), 1)
        self.assertNotIn("statement_norm", saved_rows(self.source)[0])

    def test_missing_moved_replaced_or_changed_link_source_is_explicit(self) -> None:
        self.history()
        transfer_entries(self.source, self.destination, ids=[2])
        reference = read_project_rows(self.destination, verified_only=False)[0]["origin"]
        moved = self.root / "moved.sqlite3"
        self.source.rename(moved)
        for reader in (
            lambda: read_project_rows(self.destination),
            lambda: read_entry_reference(self.destination, reference),
        ):
            with self.assertRaisesRegex(LedgerReadError, "missing or moved"):
                reader()
        replacement = self.history()
        with self.assertRaisesRegex(LedgerReadError, "identity mismatch"):
            read_project_rows(self.destination)
        replacement_uuid = replacement.identity()["ledger_uuid"]
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute(
                "UPDATE ledger_metadata SET ledger_uuid = ?",
                (read_ledger_identity(moved)["ledger_uuid"],),
            )
            conn.execute(
                "UPDATE verifications SET origin_ledger_uuid = ?, origin_row_id = 9 WHERE id = 2",
                (replacement_uuid,),
            )
        with self.assertRaisesRegex(LedgerReadError, "origin mismatch"):
            read_project_rows(self.destination)
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("DELETE FROM verifications WHERE id = 2")
        with self.assertRaisesRegex(LedgerReadError, "row 2 is missing"):
            read_project_rows(self.destination)

    def test_relink_repairs_a_moved_source_without_changing_origin(self) -> None:
        self.history()
        transfer_entries(self.source, self.destination, ids=[1])
        reference = read_project_rows(self.destination)[0]["reference"]
        moved = self.root / "relocated.sqlite3"
        self.source.rename(moved)
        repaired = transfer_entries(moved, self.destination, ids=[1])
        self.assertEqual(repaired["added"], 0)
        self.assertEqual(repaired["updated_links"], 1)
        self.assertEqual(repaired["skipped"], 0)
        self.assertEqual(read_project_rows(self.destination)[0]["reference"], reference)
        self.assertEqual(transfer_entries(moved, self.destination, ids=[1])["updated_links"], 0)

    def test_import_replaces_links_with_self_contained_history(self) -> None:
        self.history()
        transfer_entries(self.source, self.destination, tag="history")
        references = [
            r["reference"] for r in read_project_rows(self.destination, verified_only=False)
        ]
        copied = transfer_entries(self.source, self.destination, tag="history", mode="import")
        self.assertEqual(copied["removed_links"], 3)
        self.source.unlink()
        rows = read_project_rows(self.destination, verified_only=False)
        self.assertEqual([r["reference"] for r in rows], references)
        self.assertTrue(all(not r.get("linked") for r in rows))

    def test_selection_and_reference_validation(self) -> None:
        self.history()
        for options in (
            {},
            {"ids": []},
            {"ids": [0]},
            {"ids": [-1]},
            {"ids": [True]},
            {"ids": [1.0]},
            {"ids": [1, 1]},
            {"ids": [2**64]},
            {"ids": "1"},
            {"ids": [1], "tag": "history"},
            {"tag": ""},
            {"tag": " "},
            {"tag": True},
            {"ids": [1], "mode": "move"},
            {"ids": [1], "dry_run": "false"},
            {"ids": [1, 99]},
            {"tag": "no matching tag"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                transfer_entries(self.source, self.destination, **options)
            self.assertFalse(self.destination.exists())
        with self.assertRaises(LedgerReadError):
            transfer_entries(self.root / "missing.sqlite3", self.destination, ids=[1])
        with self.assertRaises(ValueError):
            transfer_entries(self.source, self.source, ids=[1])
        for reference in ("1", "bad:1", f"{uuid4()}:0", f"{uuid4()}:-1", f"{uuid4()}:True", None):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                read_entry_reference(self.source, reference)
        self.assertIsNone(read_entry_reference(self.source, f"{uuid4()}:1"))

    def test_verified_requires_status_and_flag(self) -> None:
        self.history()
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("UPDATE verifications SET verified = 1 WHERE status = 'error'")
            conn.execute("UPDATE verifications SET verified = 0 WHERE status = 'verified'")
        transfer_entries(self.source, self.destination, tag="history")
        self.assertEqual(read_project_rows(self.destination), [])
        self.assertEqual(len(read_project_rows(self.destination, verified_only=False)), 3)

    def test_destination_transaction_rolls_back_on_import_failure(self) -> None:
        self.history()
        legacy(self.destination)
        with closing(sqlite3.connect(self.destination)) as conn:
            conn.execute(
                "CREATE TRIGGER refuse_second BEFORE INSERT ON verifications "
                "WHEN NEW.target = 'Original.target2' BEGIN "
                "SELECT RAISE(ABORT, 'fixture refuses second row'); END"
            )
        original, original_schema = saved_rows(self.destination), schema(self.destination)
        with self.assertRaisesRegex(LedgerReadError, "fixture refuses second row"):
            transfer_entries(self.source, self.destination, tag="history", mode="import")
        self.assertEqual(saved_rows(self.destination), original)
        self.assertEqual(schema(self.destination), original_schema)
        self.assertEqual(
            read_ledger_identity(self.destination), {"ledger_uuid": None, "project_id": None}
        )

    def test_destination_links_roll_back_on_failure(self) -> None:
        self.history()
        transfer_entries(self.source, self.destination, ids=[1])
        before = read_project_rows(self.destination, verified_only=False)
        with closing(sqlite3.connect(self.destination)) as conn:
            conn.execute(
                "CREATE TRIGGER refuse_third BEFORE INSERT ON entry_links "
                "WHEN NEW.source_row_id = 3 BEGIN "
                "SELECT RAISE(ABORT, 'fixture refuses third row'); END"
            )
        with self.assertRaisesRegex(LedgerReadError, "fixture refuses third row"):
            transfer_entries(self.source, self.destination, ids=[2, 3])
        self.assertEqual(read_project_rows(self.destination, verified_only=False), before)

    def test_unknown_history_fields_are_not_silently_dropped(self) -> None:
        self.history()
        legacy(self.destination)
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("ALTER TABLE verifications ADD COLUMN future_payload TEXT")
            conn.execute("UPDATE verifications SET future_payload = 'preserve this too'")
        before = saved_rows(self.destination), schema(self.destination)
        with self.assertRaisesRegex(LedgerReadError, "unrecognized verification columns"):
            transfer_entries(self.source, self.destination, ids=[1], mode="import")
        self.assertEqual((saved_rows(self.destination), schema(self.destination)), before)


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LedgerProjectTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

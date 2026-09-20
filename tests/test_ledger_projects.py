"""Stable ledger identities and non-destructive project history, using temporary DBs only."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from action_speaks.ledger import (  # noqa: E402
    SCHEMA,
    Ledger,
    LedgerReadError,
    Recollection,
    normalize_statement,
    read_ledger_identity,
    read_ledger_row,
    read_verified_rows,
)
from action_speaks.ledger_transfer import (  # noqa: E402
    read_entry_reference,
    read_project_rows,
    transfer_entries,
)
from action_speaks.verify import Check, Verdict  # noqa: E402


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


class TestLedgerProject:
    @pytest.fixture(autouse=True)
    def setup(self) -> Iterator[None]:
        with tempfile.TemporaryDirectory() as directory:
            self.root = Path(directory)
            self.source = self.root / "source.sqlite3"
            self.destination = self.root / "destination.sqlite3"
            self.project_id = str(uuid4())
            yield

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
        assert len(originals) == len(copies)
        for original, copy in zip(originals, copies):
            for key, value in original.items():
                if key not in ("id", "origin_ledger_uuid", "origin_row_id"):
                    assert value == copy[key], key

    def test_identity_binding_and_rename(self) -> None:
        ledger = self.history()
        identity = ledger.identity()
        assert str(UUID(identity["ledger_uuid"])) == identity["ledger_uuid"]
        assert identity["project_id"] is None
        assert Ledger(self.source).identity() == identity
        before = saved_rows(self.source)
        ledger.bind_project(self.project_id)
        ledger.bind_project(self.project_id.upper())
        with pytest.raises(ValueError, match="already bound"):
            ledger.bind_project(str(uuid4()))
        for invalid in ("not a uuid", "", None, True, 4):
            with pytest.raises(ValueError):
                ledger.bind_project(invalid)
        renamed = self.root / "renamed.sqlite3"
        self.source.rename(renamed)
        assert Ledger(renamed).identity()["ledger_uuid"] == identity["ledger_uuid"]
        assert read_ledger_identity(renamed)["project_id"] == self.project_id
        assert saved_rows(renamed) == before
        row = read_ledger_row(renamed, 1)
        assert row["origin"] == f"{identity['ledger_uuid']}:1"
        assert row["reference"] == row["origin"]
        assert read_verified_rows(renamed)[0]["reference"] == row["reference"]
        assert read_entry_reference(renamed, row["origin"])["source"] == before[0]["source"]

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
            assert len(set(identities)) == 1
            with closing(sqlite3.connect(path)) as conn:
                assert conn.execute("SELECT COUNT(*) FROM ledger_metadata").fetchone()[0] == 1

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
        assert len(successes) == 1
        assert ledger.identity()["project_id"] == successes[0]

    def test_legacy_readonly_and_additive_migration(self) -> None:
        assert read_ledger_identity(self.source) is None
        assert read_project_rows(self.source) == []
        assert read_entry_reference(self.source, f"{uuid4()}:1") is None
        assert not self.source.exists()
        legacy(self.source)
        before, original_schema, original = (
            fingerprint(self.source),
            schema(self.source),
            saved_rows(self.source),
        )
        assert read_ledger_identity(self.source) == {"ledger_uuid": None, "project_id": None}
        assert "ledger_uuid" not in read_ledger_row(self.source, 1)
        assert read_verified_rows(self.source) == original
        assert read_project_rows(self.source)[0]["source"] == "original source"
        assert fingerprint(self.source) == before
        assert schema(self.source) == original_schema
        identity = Ledger(self.source).identity()
        migrated = saved_rows(self.source)[0]
        for key, value in original[0].items():
            assert migrated[key] == value
        assert migrated["record_kind"] == "snippet"
        assert migrated["provenance"] is None
        assert migrated["statement_norm"] == normalize_statement(migrated["statement"])
        assert migrated["origin_row_id"] is None
        assert read_verified_rows(self.source)[0]["origin"] == f"{identity['ledger_uuid']}:1"

    def test_readonly_access_never_creates_or_modifies_sidecars(self) -> None:
        ledger = self.history()
        identity = ledger.identity()
        before = directory_state(self.root)
        for _ in range(2):
            assert read_ledger_identity(self.source) == identity
            assert len(read_verified_rows(self.source)) == 1
            assert read_ledger_row(self.source, 2)["record_kind"] == "project"
            assert len(read_project_rows(self.source)) == 1
            assert read_entry_reference(self.source, f"{identity['ledger_uuid']}:2") is not None
            transfer_entries(self.source, self.destination, ids=[1], dry_run=True)
            assert directory_state(self.root) == before

    def test_readonly_access_sees_committed_active_wal_without_mutating_it(self) -> None:
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE keepalive (value TEXT)")
            ledger = self.history()
            ledger.bind_project(self.project_id)
            identity = ledger.identity()
            assert Path(str(self.source) + "-wal").stat().st_size > 0
            alias = self.root / "alias.sqlite3"
            alias.symlink_to(self.source)
            before = directory_state(self.root)
            for path in (self.source, alias):
                assert read_ledger_identity(path) == identity
                assert len(read_verified_rows(path)) == 1
                assert read_ledger_row(path, 2)["record_kind"] == "project"
                assert len(read_project_rows(path, verified_only=False)) == 3
                transfer_entries(path, self.destination, tag="history", dry_run=True)
                assert directory_state(self.root) == before
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE ledger_metadata SET project_id = ?", (str(uuid4()),))
            before_uncommitted = directory_state(self.root)
            assert read_ledger_identity(self.source) == identity
            assert directory_state(self.root) == before_uncommitted
            writer.rollback()

    def test_readonly_access_recovers_only_private_rollback_journal(self) -> None:
        legacy(self.source)
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA cache_size=1")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE verifications SET source = hex(randomblob(100000))")
            assert Path(str(self.source) + "-journal").exists()
            before = directory_state(self.root)
            assert read_ledger_row(self.source, 1)["source"] == "original source"
            assert directory_state(self.root) == before
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

        with patch("action_speaks.ledger.shutil.copyfile", side_effect=copy_and_change):
            rows = read_verified_rows(self.source)
        assert changed
        assert [row["id"] for row in rows] == [1, 4]

    def test_snapshot_repeated_changes_fail_explicitly(self) -> None:
        ledger = self.history()
        copyfile = shutil.copyfile

        def copy_and_change(source: Path, destination: Path) -> Path:
            result = copyfile(source, destination)
            if source == self.source:
                ledger.record(Verdict("verified", "new", "new"), "new")
            return result

        with (
            patch("action_speaks.ledger.shutil.copyfile", side_effect=copy_and_change),
            pytest.raises(LedgerReadError, match="changed repeatedly"),
        ):
            read_verified_rows(self.source)

    def test_malformed_identity_errors(self) -> None:
        self.source.write_text("not sqlite")
        with pytest.raises(LedgerReadError):
            read_ledger_identity(self.source)
        with pytest.raises(LedgerReadError):
            read_ledger_identity(self.root)
        loop = self.root / "symlink-loop"
        loop.symlink_to(loop)
        with pytest.raises(LedgerReadError):
            read_ledger_identity(loop)
        empty = self.root / "empty.sqlite3"
        empty.touch()
        with pytest.raises(LedgerReadError):
            read_ledger_identity(empty)
        for number, bad_sql in enumerate(
            (
                "UPDATE ledger_metadata SET ledger_uuid = 'invalid'",
                "UPDATE ledger_metadata SET project_id = 'invalid'",
                "DELETE FROM ledger_metadata",
                "DROP TABLE ledger_metadata; CREATE TABLE ledger_metadata (bad TEXT)",
            )
        ):
            path = self.root / f"malformed-{number}.sqlite3"
            Ledger(path)
            with closing(sqlite3.connect(path)) as conn, conn:
                for command in bad_sql.split(";"):
                    conn.execute(command)
            with pytest.raises(LedgerReadError):
                read_ledger_identity(path)

    def test_record_project_fields_and_validation(self) -> None:
        ledger = self.history()
        rows = saved_rows(self.source)
        assert rows[1]["record_kind"] == "project"
        assert rows[1]["source"] == ""
        assert rows[1]["module"] == "Original.Module"
        assert json.loads(rows[1]["provenance"])["extra"] == "retained extra provenance"
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
            with pytest.raises(ValueError):
                ledger.record(verdict, "", **keywords)
        assert saved_rows(self.source) == rows

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
            assert row["status"] == status
            assert row["verified"] == 0
            assert row["environment_id"] is None
            assert json.loads(row["provenance"]) == verdict.provenance
            assert json.loads(row["failures"]) == ["project_environment"]
            for project_id, module in ((None, "Original.Module"), (self.project_id, None)):
                with pytest.raises(ValueError):
                    ledger.record(
                        verdict, "", record_kind="project", project_id=project_id, module=module
                    )
        for environment_id in (None, "", " "):
            with pytest.raises(ValueError):
                ledger.record(
                    Verdict("verified", "Original.target", "successful attempt"),
                    "",
                    record_kind="project",
                    project_id=self.project_id,
                    module="Original.Module",
                    environment_id=environment_id,
                )
        original = saved_rows(self.source)
        assert len(original) == 2
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
            assert context in rendered
        assert "these exact revisions" not in rendered
        assert "re-verify the recorded source" not in rendered
        fuzzy = ledger.recall(text="recollection needle")[0]
        assert fuzzy.kind == "text"
        assert "candidate only" in fuzzy.advice()
        assert "not evidence" in fuzzy.advice()
        assert "Recheck module" in fuzzy.advice()
        copied = self.root / "recalled-copy.sqlite3"
        transfer_entries(self.source, copied, ids=[row_id], mode="import")
        copied_hit = Ledger(copied).recall(statement=verdict.statement)[0]
        assert copied_hit.row["reference"] == hit.row["reference"]
        assert hit.row["reference"] in copied_hit.render()
        stale = Recollection(hit.row, "statement", ["mathlib"])
        assert "Recorded library revisions differ (mathlib)" in stale.advice()

    def test_project_failure_recollection_keeps_rejection_scope(self) -> None:
        ledger = self.history()
        row = read_ledger_row(self.source, 2)
        attempt = Recollection(row, "statement", [])
        assert "ATTEMPT failed" in attempt.advice()
        assert "Recheck module Original.Module" in attempt.advice()
        statement = Recollection({**row, "failures": '["not_vacuous"]'}, "statement", [])
        assert "recorded project STATEMENT was rejected" in statement.advice()
        assert "different project environment" in statement.advice()
        assert "recorded source" not in statement.advice()
        snippet = Recollection(read_ledger_row(self.source, 1), "statement", [])
        assert "these exact revisions" in snippet.advice()
        assert "re-verify the recorded source" in snippet.advice()
        assert "    project:" not in snippet.render()
        assert ledger.stats()["total"] == 3

    def test_link_all_statuses_idempotent_and_verified_filter(self) -> None:
        source = self.history()
        originals, before = saved_rows(self.source), fingerprint(self.source)
        result = transfer_entries(self.source, self.destination, tag="history")
        assert (result["count"], result["added"]) == (3, 3)
        assert saved_rows(self.destination) == []
        linked = read_project_rows(self.destination, verified_only=False)
        self.assert_payload(originals, linked)
        assert {r["ledger_uuid"] for r in linked} == {source.identity()["ledger_uuid"]}
        assert {r["ledger_path"] for r in linked} == {str(self.source)}
        assert len(read_project_rows(self.destination)) == 1
        assert transfer_entries(self.source, self.destination, ids=[1, 2, 3])["added"] == 0
        assert len(read_project_rows(self.destination, verified_only=False)) == 3
        for row in linked:
            assert row["reference"] == row["origin"]
            assert read_entry_reference(self.destination, row["reference"]) == row
        assert fingerprint(self.source) == before
        assert saved_rows(self.source) == originals

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
        assert (result["count"], result["added"]) == (3, 3)
        self.assert_payload(original, copies)
        assert [row["id"] for row in copies] == [2, 3, 4]
        assert copies[1]["project_id"] == self.project_id
        assert destination.identity()["project_id"] == destination_project
        copied_row = read_ledger_row(self.destination, 2)
        assert copied_row["reference"] == f"{source.identity()['ledger_uuid']}:1"
        assert copied_row["ledger_uuid"] == destination.identity()["ledger_uuid"]
        assert {row["origin_ledger_uuid"] for row in copies} == {source.identity()["ledger_uuid"]}
        assert (
            transfer_entries(self.source, self.destination, tag="history", mode="import")["added"]
            == 0
        )
        assert destination.recall(source=original[0]["source"])
        assert destination.recall(statement=original[0]["statement"])
        assert destination.recall(text="historical needle")
        third = self.root / "third.sqlite3"
        transfer_entries(self.destination, third, ids=[2, 3, 4], mode="import")
        third_rows = saved_rows(third)
        self.assert_payload(original, third_rows)
        assert [(row["origin_ledger_uuid"], row["origin_row_id"]) for row in copies] == [
            (row["origin_ledger_uuid"], row["origin_row_id"]) for row in third_rows
        ]
        for row in read_project_rows(third, verified_only=False):
            assert row["reference"] == row["origin"]
            assert row["ledger_uuid"] == read_ledger_identity(third)["ledger_uuid"]
            assert read_entry_reference(third, row["reference"]) == row
        local_ref = f"{destination.identity()['ledger_uuid']}:2"
        assert read_entry_reference(self.destination, local_ref)["source"] == original[0]["source"]
        assert fingerprint(self.source) == before
        assert saved_rows(self.source) == original

    def test_import_deduplicates_native_origin_and_links(self) -> None:
        self.history()
        transfer_entries(self.source, self.destination, tag="history", mode="import")
        assert (
            transfer_entries(self.destination, self.source, tag="history", mode="import")["added"]
            == 0
        )
        linked = self.root / "linked.sqlite3"
        transfer_entries(self.destination, linked, tag="history")
        for row in read_project_rows(linked, verified_only=False):
            assert row["ledger_uuid"] == read_ledger_identity(self.destination)["ledger_uuid"]
            assert (
                row["reference"]
                == f"{read_ledger_identity(self.source)['ledger_uuid']}:{row['origin_row_id']}"
            )
            assert read_entry_reference(linked, row["reference"]) == row
        assert transfer_entries(self.source, linked, tag="history")["added"] == 0
        transfer_entries(self.source, linked, tag="history", mode="import")
        assert len(read_project_rows(linked, verified_only=False)) == 3
        assert saved_rows(self.source)[0]["origin_ledger_uuid"] is None

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
        assert result["count"] == 1
        assert saved_rows(self.source) == original
        assert [
            entry for entry in schema(self.source) if entry[0] != "ledger_metadata"
        ] == old_schema
        assert (
            saved_rows(self.destination)[0]["origin_ledger_uuid"]
            == read_ledger_identity(self.source)["ledger_uuid"]
        )
        assert not {"projects", "snapshots"} & {entry[0] for entry in schema(self.destination)}

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
                assert preview["count"] == 1
                assert preview["action"] == f"would-{mode}"
                assert preview["origins"] == [None]
                assert preview["would_assign_uuid"]
                assert "would-assign-UUID: source" in preview["notes"]
                assert not absent.parent.exists()
        assert (fingerprint(self.source), schema(self.source)) == source_before
        assert (fingerprint(self.destination), schema(self.destination)) == destination_before

    def test_identified_dry_run_preserves_rows_and_known_origins(self) -> None:
        source = self.history()
        destination = Ledger(self.destination)
        source_before, destination_before = fingerprint(self.source), fingerprint(self.destination)
        preview = transfer_entries(
            self.source, self.destination, ids=[2], mode="import", dry_run=True
        )
        assert preview["origins"] == [f"{source.identity()['ledger_uuid']}:2"]
        assert preview["rows"][0]["status"] == "rejected"
        assert not preview["would_assign_uuid"]
        assert preview["destination_identity"] == destination.identity()
        assert fingerprint(self.source) == source_before
        assert fingerprint(self.destination) == destination_before
        assert saved_rows(self.destination) == []

    def test_missing_selection_does_not_assign_legacy_uuid(self) -> None:
        legacy(self.source)
        before = fingerprint(self.source), schema(self.source)
        for dry_run in (False, True):
            for options in ({"ids": [1, 2]}, {"tag": "absent"}):
                with pytest.raises(ValueError):
                    transfer_entries(self.source, self.destination, dry_run=dry_run, **options)
                assert not self.destination.exists()
        assert (fingerprint(self.source), schema(self.source)) == before

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
            assert sum(result["added"] for result in results) == 1
            assert len({result["origins"][0] for result in results}) == 1
            assert len(read_project_rows(destination)) == 1
        assert "statement_norm" not in saved_rows(self.source)[0]

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
            with pytest.raises(LedgerReadError, match="missing or moved"):
                reader()
        replacement = self.history()
        with pytest.raises(LedgerReadError, match="identity mismatch"):
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
        with pytest.raises(LedgerReadError, match="origin mismatch"):
            read_project_rows(self.destination)
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("DELETE FROM verifications WHERE id = 2")
        with pytest.raises(LedgerReadError, match="row 2 is missing"):
            read_project_rows(self.destination)

    def test_relink_repairs_a_moved_source_without_changing_origin(self) -> None:
        self.history()
        transfer_entries(self.source, self.destination, ids=[1])
        reference = read_project_rows(self.destination)[0]["reference"]
        moved = self.root / "relocated.sqlite3"
        self.source.rename(moved)
        repaired = transfer_entries(moved, self.destination, ids=[1])
        assert repaired["added"] == 0
        assert repaired["updated_links"] == 1
        assert repaired["skipped"] == 0
        assert read_project_rows(self.destination)[0]["reference"] == reference
        assert transfer_entries(moved, self.destination, ids=[1])["updated_links"] == 0

    def test_import_replaces_links_with_self_contained_history(self) -> None:
        self.history()
        transfer_entries(self.source, self.destination, tag="history")
        references = [
            r["reference"] for r in read_project_rows(self.destination, verified_only=False)
        ]
        copied = transfer_entries(self.source, self.destination, tag="history", mode="import")
        assert copied["removed_links"] == 3
        self.source.unlink()
        rows = read_project_rows(self.destination, verified_only=False)
        assert [r["reference"] for r in rows] == references
        assert all(not r.get("linked") for r in rows)

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
            with pytest.raises(ValueError):
                transfer_entries(self.source, self.destination, **options)
            assert not self.destination.exists()
        with pytest.raises(LedgerReadError):
            transfer_entries(self.root / "missing.sqlite3", self.destination, ids=[1])
        with pytest.raises(ValueError):
            transfer_entries(self.source, self.source, ids=[1])
        for reference in ("1", "bad:1", f"{uuid4()}:0", f"{uuid4()}:-1", f"{uuid4()}:True", None):
            with pytest.raises(ValueError):
                read_entry_reference(self.source, reference)
        assert read_entry_reference(self.source, f"{uuid4()}:1") is None

    def test_verified_requires_status_and_flag(self) -> None:
        self.history()
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("UPDATE verifications SET verified = 1 WHERE status = 'error'")
            conn.execute("UPDATE verifications SET verified = 0 WHERE status = 'verified'")
        transfer_entries(self.source, self.destination, tag="history")
        assert read_project_rows(self.destination) == []
        assert len(read_project_rows(self.destination, verified_only=False)) == 3

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
        with pytest.raises(LedgerReadError, match="fixture refuses second row"):
            transfer_entries(self.source, self.destination, tag="history", mode="import")
        assert saved_rows(self.destination) == original
        assert schema(self.destination) == original_schema
        assert read_ledger_identity(self.destination) == {"ledger_uuid": None, "project_id": None}

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
        with pytest.raises(LedgerReadError, match="fixture refuses third row"):
            transfer_entries(self.source, self.destination, ids=[2, 3])
        assert read_project_rows(self.destination, verified_only=False) == before

    def test_unknown_history_fields_are_not_silently_dropped(self) -> None:
        self.history()
        legacy(self.destination)
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("ALTER TABLE verifications ADD COLUMN future_payload TEXT")
            conn.execute("UPDATE verifications SET future_payload = 'preserve this too'")
        before = saved_rows(self.destination), schema(self.destination)
        with pytest.raises(LedgerReadError, match="unrecognized verification columns"):
            transfer_entries(self.source, self.destination, ids=[1], mode="import")
        assert (saved_rows(self.destination), schema(self.destination)) == before

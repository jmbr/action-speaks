"""Read-only ledger access, including legacy databases."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nullius.ledger import LedgerReadError, read_ledger_row, read_verified_rows  # noqa: E402


@pytest.fixture
def directory() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as name:
        yield Path(name)


@pytest.fixture
def populated(directory: Path) -> Path:
    """A ledger holding one row of each status, written out of ID order."""
    path = directory / "ledger.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE verifications (id INTEGER PRIMARY KEY, source TEXT, target TEXT, "
            "status TEXT, verified INTEGER, source_sha256 TEXT, statement TEXT)"
        )
        for row_id, status, verified in (
            (3, "verified", 1),
            (1, "rejected", 0),
            (2, "error", 1),
        ):
            conn.execute(
                "INSERT INTO verifications VALUES (?, 'source', 'main', ?, ?, 'sha', 'True')",
                (row_id, status, verified),
            )
        conn.commit()
    return path


def fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_missing_ledger_reads_empty_without_creating_it(directory: Path) -> None:
    path = directory / "ledger.sqlite3"
    assert read_verified_rows(path) == []
    assert read_ledger_row(path, 1) is None
    assert not path.exists()


def test_reads_select_verified_rows_and_leave_the_file_untouched(populated: Path) -> None:
    before = fingerprint(populated)
    assert [row["id"] for row in read_verified_rows(populated)] == [3]
    assert read_ledger_row(populated, 1)["status"] == "rejected"
    assert read_ledger_row(populated, 4) is None
    assert fingerprint(populated) == before


@pytest.mark.parametrize("invalid", [0, -1, True])
def test_invalid_row_ids_are_rejected(populated: Path, invalid: object) -> None:
    with pytest.raises(ValueError):
        read_ledger_row(populated, invalid)


@pytest.mark.parametrize("kind", ["not-a-database", "empty", "directory"])
def test_malformed_ledgers_are_reported(directory: Path, kind: str) -> None:
    if kind == "not-a-database":
        path = directory / "broken.sqlite3"
        path.write_text("not a database")
    elif kind == "empty":
        path = directory / "empty.sqlite3"
        with closing(sqlite3.connect(path)):
            pass
    else:
        path = directory
    with pytest.raises(LedgerReadError):
        read_verified_rows(path)

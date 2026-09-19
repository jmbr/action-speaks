"""Read-only ledger access, including legacy databases."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nullius.ledger import LedgerReadError, read_ledger_row, read_verified_rows  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ledger.sqlite3"
        assert read_verified_rows(path) == []
        assert read_ledger_row(path, 1) is None
        assert not path.exists()
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
        before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        assert [row["id"] for row in read_verified_rows(path)] == [3]
        assert read_ledger_row(path, 1)["status"] == "rejected"
        assert read_ledger_row(path, 4) is None
        assert before == (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for invalid in (0, -1, True):
            try:
                read_ledger_row(path, invalid)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid ID accepted")
        broken = Path(directory) / "broken.sqlite3"
        broken.write_text("not a database")
        empty = Path(directory) / "empty.sqlite3"
        with closing(sqlite3.connect(empty)):
            pass
        for bad in (broken, empty, Path(directory)):
            try:
                read_verified_rows(bad)
            except LedgerReadError:
                pass
            else:
                raise AssertionError(f"malformed ledger accepted: {bad}")
    print("Read-only ledger access: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Append-only ledger of verification results.

A verdict is only useful if it can be produced on demand later. The ledger records what was
claimed, what Lean source was checked, what the verifier concluded, and — crucially — the
toolchain and Mathlib revision it was checked against, since a proof that verifies today
against one Mathlib may not even elaborate against another.

Rows are never updated or deleted by this module. Re-verifying the same source appends a new
row, so a claim's history (including any regression) stays visible.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .verify import Verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS verifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL    NOT NULL,
    created_iso   TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    verified      INTEGER NOT NULL,
    target        TEXT,
    claim         TEXT,
    statement     TEXT,
    source        TEXT    NOT NULL,
    source_sha256 TEXT    NOT NULL,
    axioms        TEXT    NOT NULL,
    checks        TEXT    NOT NULL,
    failures      TEXT    NOT NULL,
    toolchain     TEXT,
    mathlib_rev   TEXT,
    physlib_rev   TEXT,
    elapsed       REAL,
    tag           TEXT
);
CREATE INDEX IF NOT EXISTS idx_ver_sha    ON verifications(source_sha256);
CREATE INDEX IF NOT EXISTS idx_ver_status ON verifications(status);
CREATE INDEX IF NOT EXISTS idx_ver_target ON verifications(target);
CREATE INDEX IF NOT EXISTS idx_ver_time   ON verifications(created_at);
"""


@dataclass
class Ledger:
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Add columns introduced after a ledger was first created.

        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing ledger, so a new provenance
        field would silently be dropped for everyone with history. Rows predating a column
        keep NULL there, which is the honest answer: that verification genuinely did not
        record it.
        """
        have = {r[1] for r in c.execute("PRAGMA table_info(verifications)")}
        for col, decl in (("physlib_rev", "TEXT"),):
            if col not in have:
                c.execute(f"ALTER TABLE verifications ADD COLUMN {col} {decl}")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def record(self, verdict: Verdict, source: str, tag: str | None = None) -> int:
        now = time.time()
        failures = [c.name for c in verdict.checks if not c.passed and c.fatal]
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO verifications
                   (created_at, created_iso, status, verified, target, claim, statement,
                    source, source_sha256, axioms, checks, failures, toolchain, mathlib_rev,
                    physlib_rev, elapsed, tag)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    now,
                    time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                    verdict.status,
                    int(verdict.verified),
                    verdict.target,
                    verdict.claim,
                    verdict.statement,
                    source,
                    verdict.source_sha256,
                    json.dumps(verdict.axioms),
                    json.dumps(
                        [
                            {
                                "name": ch.name,
                                "passed": ch.passed,
                                "fatal": ch.fatal,
                                "detail": ch.detail,
                            }
                            for ch in verdict.checks
                        ]
                    ),
                    json.dumps(failures),
                    verdict.provenance.get("toolchain"),
                    verdict.provenance.get("mathlib_rev"),
                    verdict.provenance.get("physlib_rev"),
                    verdict.elapsed,
                    tag,
                ),
            )
            return int(cur.lastrowid or 0)

    def get(self, row_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM verifications WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM verifications"
        args: list[Any] = []
        if status:
            q += " WHERE status = ?"
            args.append(status)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def by_hash(self, sha256: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM verifications WHERE source_sha256 = ? ORDER BY id DESC",
                    (sha256,),
                ).fetchall()
            ]

    def stats(self) -> dict[str, Any]:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
            verified = c.execute(
                "SELECT COUNT(*) FROM verifications WHERE verified = 1"
            ).fetchone()[0]
            rows = c.execute(
                """SELECT status, COUNT(*) n FROM verifications GROUP BY status"""
            ).fetchall()
        return {
            "total": total,
            "verified": verified,
            "by_status": {r["status"]: r["n"] for r in rows},
            "path": str(self.path),
        }

    def export(self, rows: Iterable[dict[str, Any]] | None = None) -> str:
        data = list(rows) if rows is not None else self.recent(limit=10_000)
        return json.dumps(data, indent=2, ensure_ascii=False)

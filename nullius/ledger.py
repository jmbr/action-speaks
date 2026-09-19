"""Append-only ledger of verification results.

A verdict is only useful if it can be produced on demand later. The ledger records what was
claimed, what Lean source was checked, what the verifier concluded, and — crucially — the
toolchain and Mathlib revision it was checked against, since a proof that verifies today
against one Mathlib may not even elaborate against another.

Rows are never updated or deleted by this module. Re-verifying the same source appends a new
row, so a claim's history (including any regression) stays visible.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .verify import Verdict


class LedgerReadError(RuntimeError):
    """An existing ledger cannot be read without changing it."""


def _read_rows(path: Path, row_id: int | None = None) -> list[dict[str, Any]]:
    path = Path(path).absolute()
    try:
        path.stat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LedgerReadError(f"cannot access ledger {path}: {exc}") from exc
    required = {"id", "source", "target", "status", "verified", "source_sha256", "statement"}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(verifications)")}
            missing = required - columns
            if missing:
                raise LedgerReadError(
                    f"ledger {path} is missing columns: {', '.join(sorted(missing))}"
                )
            if row_id is None:
                query = (
                    "SELECT * FROM verifications WHERE status = 'verified' "
                    "AND verified = 1 ORDER BY id"
                )
                args = ()
            else:
                query = "SELECT * FROM verifications WHERE id = ?"
                args = (row_id,)
            return [dict(row) for row in conn.execute(query, args)]
    except sqlite3.Error as exc:
        raise LedgerReadError(f"cannot read ledger {path}: {exc}") from exc


def read_verified_rows(path: Path) -> list[dict[str, Any]]:
    """Read a snapshot of verified rows without creating or migrating the database."""
    return _read_rows(path)


def read_ledger_row(path: Path, row_id: int) -> dict[str, Any] | None:
    """Retrieve an attempt, including its source; a missing ledger or ID returns None."""
    if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
        raise ValueError("ledger ID must be a positive integer")
    rows = _read_rows(path, row_id)
    return rows[0] if rows else None


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
    cslib_rev     TEXT,
    elapsed       REAL,
    tag           TEXT
);
CREATE INDEX IF NOT EXISTS idx_ver_sha    ON verifications(source_sha256);
CREATE INDEX IF NOT EXISTS idx_ver_status ON verifications(status);
CREATE INDEX IF NOT EXISTS idx_ver_target ON verifications(target);
CREATE INDEX IF NOT EXISTS idx_ver_time   ON verifications(created_at);
"""

# Checks whose failure is a property of the *statement* rather than of the attempt made on
# it. Everything else - a tactic that did not work, a banned construct, a citation of an
# incomplete result - says nothing about whether the claim is provable, and must not be
# reported as if it did. Attempt-level failures dominate in practice.
STATEMENT_LEVEL_CHECKS = frozenset(
    {"not_vacuous", "hypotheses_used", "statement_sorry_free", "is_theorem"}
)

_UNIVERSE = re.compile(r"\bu_\d+\b")
_INST_NAME = re.compile(r"\binst(?:✝[\u00b9\u00b2\u00b3\u2070-\u2079]*|_\d+)")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_']{2,}")


def normalize_statement(statement: str | None) -> str:
    """A canonical key for "the same theorem, however it was proved".

    The elaborated statement is Lean's normal form for a declaration's type, so two
    different proofs of one theorem agree here where their sources do not. Only
    presentational noise is stripped: universe metavariable indices and generated instance
    binder names.

    Deliberately conservative, since a false "you already proved this" is the failure mode
    this tool exists to prevent. It is not alpha-equivalence: renaming a bound variable
    defeats it, which costs a missed match rather than a wrong one.
    """
    if not statement:
        return ""
    s = _UNIVERSE.sub("u", statement)
    s = _INST_NAME.sub("inst", s)
    return " ".join(s.split())


def _drift(row: dict[str, Any], current: dict[str, str] | None) -> list[str]:
    """Which pinned revisions have moved since this row was written.

    Returns the names only; what the drift *means* depends on the verdict, and that
    judgement belongs to `Recollection.advice`.
    """
    if not current:
        return []
    moved = []
    for col in ("toolchain", "mathlib_rev", "physlib_rev", "cslib_rev"):
        was, now = row.get(col), current.get(col)
        if was and now and was != now:
            moved.append(col.removesuffix("_rev"))
    return moved


@dataclass
class Recollection:
    """One prior verification that bears on what is about to be attempted.

    A recollection is a *pointer*, never a verdict. It says that some source was submitted
    before and what happened to it; it does not say that the claim in hand is proved, and
    the rendered text is written so that it cannot be read that way.
    """

    row: dict[str, Any]
    kind: str  # "source" | "statement" | "text"
    drift: list[str]

    KIND_LABEL = {
        "source": "identical source",
        "statement": "identical elaborated statement",
        "text": "similar wording (candidate only)",
    }

    @property
    def verified(self) -> bool:
        return bool(self.row.get("verified"))

    @property
    def stale(self) -> bool:
        return bool(self.drift)

    @property
    def failures(self) -> list[str]:
        try:
            return list(json.loads(self.row.get("failures") or "[]"))
        except json.JSONDecodeError:
            return []

    @property
    def statement_level(self) -> bool:
        """True when the recorded failure was a defect of the statement itself."""
        return any(f in STATEMENT_LEVEL_CHECKS for f in self.failures)

    def advice(self) -> str:
        """What this row means for the attempt about to be made.

        The asymmetry is deliberate. For a verification, revision drift *weakens* the row -
        a proof accepted against one Mathlib may not elaborate against another. For a
        rejection it *strengthens* the case for trying again: libraries gain lemmas, and
        Physlib completes results that were placeholders when the rejection was recorded.
        """
        if self.kind == "text":
            # Matched on wording, so this may be a different theorem entirely. Saying
            # "proved before" here would be the very error the verifier exists to catch.
            return (
                "Related earlier work, matched on wording alone - it may be a different "
                "theorem. Compare the statement above against your claim; it is not "
                "evidence for it."
            )
        if self.verified:
            if self.stale:
                return (
                    "Proved here before, but the libraries have moved since "
                    f"({', '.join(self.drift)}). It may no longer elaborate: re-verify the "
                    "recorded source rather than citing this row."
                )
            return (
                "Proved here before, at these exact revisions. This is a pointer, not "
                "standing proof: re-verify the recorded source (milliseconds) and cite the "
                "fresh verdict."
            )
        if self.statement_level:
            fails = ", ".join(f for f in self.failures if f in STATEMENT_LEVEL_CHECKS)
            return (
                f"The STATEMENT itself was rejected ({fails}) - not the proof of it. "
                "Restate the claim so its hypotheses can be satisfied and do real work; "
                "a better proof of the same statement will fail the same way."
            )
        fails = ", ".join(self.failures) or self.row.get("status", "not verified")
        tail = (
            " The libraries have moved since then "
            f"({', '.join(self.drift)}), so it may well succeed now."
            if self.stale
            else ""
        )
        return (
            f"A previous ATTEMPT failed ({fails}). That is a fact about that proof, not "
            "about the claim: nothing here says it is unprovable, and it is not a reason "
            f"to give up. Try a different approach.{tail}"
        )

    def render(self, width: int = 110) -> str:
        r = self.row
        mark = "verified" if self.verified else (r.get("status") or "rejected")
        head = f"#{r['id']} [{mark}] {r.get('created_iso', '')} {r.get('target') or '-'}"
        if r.get("tag"):
            head += f"  tag: {r['tag']}"
        lines = [head, f"    matched: {self.KIND_LABEL.get(self.kind, self.kind)}"]
        if r.get("claim"):
            claim = str(r["claim"])
            lines.append(f"    claim: {claim[:width]}{'...' if len(claim) > width else ''}")
        if r.get("statement"):
            stmt = str(r["statement"])
            lines.append(f"    statement: {stmt[:width]}{'...' if len(stmt) > width else ''}")
        lines.append(f"    -> {self.advice()}")
        return "\n".join(lines)


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
        for col, decl in (
            ("physlib_rev", "TEXT"),
            ("cslib_rev", "TEXT"),
            ("statement_norm", "TEXT"),
        ):
            if col not in have:
                c.execute(f"ALTER TABLE verifications ADD COLUMN {col} {decl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ver_stmt ON verifications(statement_norm)")
        # Backfill the normalised statement for rows written before the column existed,
        # so recall works on existing history rather than only on new entries.
        pending = c.execute(
            "SELECT id, statement FROM verifications "
            "WHERE statement_norm IS NULL AND statement IS NOT NULL"
        ).fetchall()
        for row in pending:
            c.execute(
                "UPDATE verifications SET statement_norm = ? WHERE id = ?",
                (normalize_statement(row[1]), row[0]),
            )

    @staticmethod
    def _fts_ready(c: sqlite3.Connection) -> bool:
        """Create and populate the full-text index, reporting whether it is usable.

        The index keeps its own copy of the text columns rather than using FTS5's
        external-content mode, where `COUNT(*)` is answered from the content table: an empty
        index then reports the right row count and never rebuilds itself.

        FTS5 is compiled into most SQLite builds but not guaranteed, and only the fuzziest
        tier of recall needs it, so a missing one degrades to a LIKE scan rather than
        failing.
        """
        try:
            row = c.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'verifications_fts'"
            ).fetchone()
            if row and "content=" in (row[0] or ""):
                c.execute("DROP TABLE verifications_fts")  # the broken external-content layout
                row = None
            if not row:
                c.execute(
                    "CREATE VIRTUAL TABLE verifications_fts USING fts5(claim, statement, target)"
                )
            indexed = c.execute("SELECT COUNT(*) FROM verifications_fts").fetchone()[0]
            total = c.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
            if indexed != total:
                c.execute("DELETE FROM verifications_fts")
                c.execute(
                    "INSERT INTO verifications_fts(rowid, claim, statement, target) "
                    "SELECT id, COALESCE(claim, ''), COALESCE(statement, ''), "
                    "COALESCE(target, '') FROM verifications"
                )
            return True
        except sqlite3.Error:
            return False

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
                    physlib_rev, cslib_rev, elapsed, tag, statement_norm)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    verdict.provenance.get("cslib_rev"),
                    verdict.elapsed,
                    tag,
                    normalize_statement(verdict.statement),
                ),
            )
            row_id = int(cur.lastrowid or 0)
            try:
                c.execute(
                    "INSERT INTO verifications_fts(rowid, claim, statement, target) "
                    "VALUES (?,?,?,?)",
                    (row_id, verdict.claim or "", verdict.statement or "", verdict.target or ""),
                )
            except sqlite3.Error:
                # No FTS5 here, or no index yet; `_fts_ready` rebuilds when it next runs.
                pass
            return row_id

    def get(self, row_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM verifications WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None

    def recent(
        self, limit: int = 20, status: str | None = None, tag: str | None = None
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM verifications"
        args: list[Any] = []
        where = []
        if status:
            where.append("status = ?")
            args.append(status)
        if tag:
            where.append("tag = ?")
            args.append(tag)
        if where:
            q += " WHERE " + " AND ".join(where)
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

    def recall(
        self,
        source: str | None = None,
        statement: str | None = None,
        text: str | None = None,
        limit: int = 4,
        current: dict[str, str] | None = None,
    ) -> list[Recollection]:
        """Prior verifications bearing on what is about to be attempted.

        Three tiers, in decreasing confidence: the identical `source` by hash; the same
        elaborated `statement`, which matches one theorem however it was written; and a
        loose full-text match on `text`, restricted to verified rows so that a fuzzy hit on
        a failed attempt cannot discourage work for no reason.

        Results are pointers to re-verify, never evidence in themselves.
        """
        seen: set[int] = set()
        out: list[Recollection] = []
        with self._conn() as c:
            self._fts_ready(c)

            def take(rows: Iterable[sqlite3.Row], kind: str) -> None:
                for r in rows:
                    d = dict(r)
                    if d["id"] in seen or len(out) >= limit:
                        continue
                    seen.add(d["id"])
                    out.append(Recollection(d, kind, _drift(d, current)))

            if source:
                sha = hashlib.sha256(source.encode()).hexdigest()
                take(
                    c.execute(
                        "SELECT * FROM verifications WHERE source_sha256 = ? "
                        "ORDER BY verified DESC, id DESC LIMIT ?",
                        (sha, limit),
                    ),
                    "source",
                )

            norm = normalize_statement(statement)
            if norm:
                take(
                    c.execute(
                        "SELECT * FROM verifications WHERE statement_norm = ? "
                        "ORDER BY verified DESC, id DESC LIMIT ?",
                        (norm, limit),
                    ),
                    "statement",
                )

            if text and len(out) < limit:
                take(self._text_search(c, text, limit), "text")
        return out[:limit]

    @staticmethod
    def _text_search(c: sqlite3.Connection, text: str, limit: int) -> list[sqlite3.Row]:
        """Verified rows whose claim or statement reads like `text`.

        The query is rebuilt from word characters only, because an unescaped `-` or `*` is
        FTS5 syntax and would raise on ordinary prose.
        """
        words = [w.lower() for w in _WORD.findall(text)][:12]
        if not words:
            return []
        try:
            return list(
                c.execute(
                    "SELECT v.* FROM verifications_fts f JOIN verifications v ON v.id = f.rowid "
                    "WHERE verifications_fts MATCH ? AND v.verified = 1 "
                    "ORDER BY rank LIMIT ?",
                    (" OR ".join(words), limit),
                ).fetchall()
            )
        except sqlite3.Error:
            longest = sorted(words, key=len, reverse=True)[:3]
            clause = " OR ".join(["claim LIKE ? OR statement LIKE ?"] * len(longest))
            args: list[Any] = []
            for w in longest:
                args += [f"%{w}%", f"%{w}%"]
            return list(
                c.execute(
                    f"SELECT * FROM verifications WHERE verified = 1 AND ({clause}) "
                    "ORDER BY id DESC LIMIT ?",
                    (*args, limit),
                ).fetchall()
            )

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

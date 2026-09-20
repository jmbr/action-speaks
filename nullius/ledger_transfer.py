"""Non-destructive links and origin-preserving imports of selected ledger history.

The source snapshot is read before the destination transaction. These are separate
databases, not a cross-database atomic operation; source history is never changed.
"""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

from .ledger import (
    Ledger,
    LedgerReadError,
    _enrich_row,
    _entry_reference,
    _read_connection,
    _read_identity,
    _uuid,
    normalize_statement,
)

_LINK_SCHEMA = """
CREATE TABLE IF NOT EXISTS entry_links (
    origin_ledger_uuid TEXT NOT NULL,
    origin_row_id INTEGER NOT NULL CHECK(origin_row_id > 0),
    source_ledger_uuid TEXT NOT NULL,
    source_row_id INTEGER NOT NULL CHECK(source_row_id > 0),
    source_path TEXT NOT NULL,
    PRIMARY KEY (origin_ledger_uuid, origin_row_id)
)
"""
_LINK_COLUMNS = {
    "origin_ledger_uuid",
    "origin_row_id",
    "source_ledger_uuid",
    "source_row_id",
    "source_path",
}


def _read_links(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'entry_links'").fetchone():
        return []
    columns = {r[1] for r in conn.execute("PRAGMA table_info(entry_links)")}
    if not _LINK_COLUMNS <= columns:
        raise LedgerReadError("ledger entry_links is missing required columns")
    links = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM entry_links ORDER BY origin_ledger_uuid, origin_row_id"
        )
    ]
    for link in links:
        try:
            for name in ("origin_ledger_uuid", "source_ledger_uuid"):
                link[name] = _uuid(link[name], name)
            for name in ("origin_row_id", "source_row_id"):
                value = link[name]
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"{name} must be a positive integer")
            if (
                not isinstance(link["source_path"], str)
                or not Path(link["source_path"]).is_absolute()
            ):
                raise ValueError("linked source path must be absolute")
        except ValueError as exc:
            raise LedgerReadError(f"invalid ledger entry link: {exc}") from exc
    return links


def _linked_row(link: dict[str, Any], conn: sqlite3.Connection | None) -> dict[str, Any]:
    source = Path(link["source_path"])
    if conn is None:
        raise LedgerReadError(f"linked source is missing or moved: {source}")
    identity = _read_identity(conn)
    if identity["ledger_uuid"] != link["source_ledger_uuid"]:
        raise LedgerReadError(
            f"linked source identity mismatch at {source}: expected "
            f"{link['source_ledger_uuid']}, found {identity['ledger_uuid']}"
        )
    saved = conn.execute(
        "SELECT * FROM verifications WHERE id = ?", (link["source_row_id"],)
    ).fetchone()
    if saved is None:
        raise LedgerReadError(f"linked source row {link['source_row_id']} is missing from {source}")
    row = _enrich_row(dict(saved), identity)
    if (row["origin_ledger_uuid"], row["origin_row_id"]) != (
        link["origin_ledger_uuid"],
        link["origin_row_id"],
    ):
        raise LedgerReadError(f"linked source origin mismatch at {source}:{link['source_row_id']}")
    row.update(ledger_path=str(source), linked=True)
    return row


def _history_rows(path: Path) -> list[dict[str, Any]]:
    path = Path(path).absolute()
    with _read_connection(path) as conn:
        if conn is None:
            return []
        identity = _read_identity(conn)
        rows = [
            _enrich_row(dict(row), identity)
            for row in conn.execute("SELECT * FROM verifications ORDER BY id")
        ]
        for row in rows:
            row["ledger_path"] = str(path)
        links = _read_links(conn)
    # Resolve every link before filtering or deduplicating. A local copy or a status filter
    # must not hide a broken association and silently present an incomplete history.
    with ExitStack() as readers:
        connections: dict[Path, sqlite3.Connection | None] = {}
        for link in links:
            source = Path(link["source_path"]).resolve()
            if source not in connections:
                connections[source] = readers.enter_context(_read_connection(source))
            rows.append(_linked_row(link, connections[source]))
    return rows


def read_project_rows(path: Path, *, verified_only: bool = True) -> list[dict[str, Any]]:
    """Merge local and linked rows, preserving source IDs and deduplicating origins.

    Both snippet and project records are returned; callers choose the relevant kind.
    """
    if not isinstance(verified_only, bool):
        raise ValueError("verified_only must be a boolean")
    seen: set[str | tuple[str, int]] = set()
    result = []
    for row in _history_rows(path):
        if verified_only and not (row["status"] == "verified" and row["verified"] == 1):
            continue
        origin = row.get("origin") or (row["ledger_path"], row["id"])
        if origin not in seen:
            seen.add(origin)
            result.append(row)
    return result


def read_entry_reference(path: Path, reference: str) -> dict[str, Any] | None:
    """Resolve a stable UUID:ID origin or a containing ledger's local row reference."""
    ledger_uuid, row_id = _entry_reference(reference)
    rows = _history_rows(path)
    for row in rows:
        if (row.get("origin_ledger_uuid"), row.get("origin_row_id")) == (ledger_uuid, row_id):
            return row
    for row in rows:
        if (row.get("ledger_uuid"), row["id"]) == (ledger_uuid, row_id):
            return row
    return None


def _validate_selection(ids: list[int] | None, tag: str | None) -> None:
    if (ids is None) == (tag is None):
        raise ValueError("select either explicit IDs or a tag, but not both")
    if ids is not None:
        if not isinstance(ids, list) or not ids:
            raise ValueError("IDs must be a nonempty list of positive integers")
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 < value <= 9223372036854775807
            for value in ids
        ):
            raise ValueError("IDs must be positive SQLite integers, not booleans")
        if len(ids) != len(set(ids)):
            raise ValueError("selected IDs must not contain duplicates")
    elif not isinstance(tag, str) or not tag.strip():
        raise ValueError("tag must be a nonempty string")


def _select_rows(
    conn: sqlite3.Connection, ids: list[int] | None, tag: str | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    identity = _read_identity(conn)
    if ids is not None:
        query = "SELECT * FROM verifications WHERE id IN (" + ",".join("?" for _ in ids) + ")"
        saved = conn.execute(query + " ORDER BY id", ids).fetchall()
        missing = set(ids) - {row["id"] for row in saved}
        if missing:
            raise ValueError(f"source ledger is missing selected IDs: {sorted(missing)}")
    else:
        saved = conn.execute(
            "SELECT * FROM verifications WHERE tag = ? ORDER BY id", (tag,)
        ).fetchall()
        if not saved:
            raise ValueError(f"source ledger has no entries with tag {tag!r}")
    return identity, [_enrich_row(dict(row), identity) for row in saved]


def _source_snapshot(
    path: Path, ids: list[int] | None, tag: str | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with _read_connection(path) as conn:
        if conn is None:
            raise LedgerReadError(f"source ledger is missing: {path}")
        return _select_rows(conn, ids, tag)


def _assign_source_identity(
    path: Path, ids: list[int] | None, tag: str | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    # mode=rw will not recreate a source moved between selection and UUID assignment.
    # Do not use Ledger(path): it would migrate historical rows and enable WAL.
    try:
        with (
            closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)) as conn,
            conn,
        ):
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            _select_rows(conn, ids, tag)
            Ledger._ensure_identity(conn)
            return _select_rows(conn, ids, tag)
    except sqlite3.Error as exc:
        raise LedgerReadError(f"cannot assign source ledger identity at {path}: {exc}") from exc


def _has_local_origin(
    conn: sqlite3.Connection, identity: dict[str, Any], origin_uuid: str, origin_id: int
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM verifications WHERE "
            "(origin_ledger_uuid = ? AND origin_row_id = ?) OR "
            "(id = ? AND origin_ledger_uuid IS NULL AND origin_row_id IS NULL AND ? = ?) LIMIT 1",
            (origin_uuid, origin_id, origin_id, identity["ledger_uuid"], origin_uuid),
        ).fetchone()
        is not None
    )


def _import_row(conn: sqlite3.Connection, row: dict[str, Any], columns: set[str]) -> None:
    unknown = row.keys() - columns - {"ledger_uuid", "origin", "reference"}
    if unknown:
        raise LedgerReadError(
            f"cannot import unrecognized verification columns: {', '.join(sorted(unknown))}"
        )
    values = {name: row[name] for name in columns - {"id"} if name in row}
    if values.get("statement_norm") is None:
        values["statement_norm"] = normalize_statement(row["statement"])
    names = sorted(values)
    quoted_names = ['"' + name.replace('"', '""') + '"' for name in names]
    conn.execute(
        "INSERT INTO verifications ("
        + ", ".join(quoted_names)
        + ") VALUES ("
        + ",".join("?" for _ in names)
        + ")",
        [values[name] for name in names],
    )


def _apply_transfer(
    source: Path,
    destination: Path,
    source_identity: dict[str, Any],
    rows: list[dict[str, Any]],
    mode: str,
) -> tuple[dict[str, Any], int, int, int, bool]:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination, timeout=30)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            needs_uuid = _read_identity(conn)["ledger_uuid"] is None
            Ledger._initialize(conn)
            identity = _read_identity(conn)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(verifications)")}
            if mode == "link":
                conn.execute(_LINK_SCHEMA)
            linked_origins = {
                (link["origin_ledger_uuid"], link["origin_row_id"]): link
                for link in _read_links(conn)
            }
            added = 0
            updated = 0
            removed = 0
            for row in rows:
                origin = (row["origin_ledger_uuid"], row["origin_row_id"])
                local = _has_local_origin(conn, identity, *origin)
                if mode == "link":
                    if origin in linked_origins:
                        previous = linked_origins[origin]
                        new_location = (source_identity["ledger_uuid"], row["id"], str(source))
                        if new_location != (
                            previous["source_ledger_uuid"],
                            previous["source_row_id"],
                            previous["source_path"],
                        ):
                            conn.execute(
                                "UPDATE entry_links SET source_ledger_uuid=?, source_row_id=?, "
                                "source_path=? WHERE origin_ledger_uuid=? AND origin_row_id=?",
                                (*new_location, *origin),
                            )
                            updated += 1
                        continue
                    if local:
                        continue
                    conn.execute(
                        "INSERT INTO entry_links "
                        "(origin_ledger_uuid, origin_row_id, source_ledger_uuid, source_row_id, "
                        "source_path) VALUES (?,?,?,?,?)",
                        (*origin, source_identity["ledger_uuid"], row["id"], str(source)),
                    )
                    linked_origins[origin] = {
                        "source_ledger_uuid": source_identity["ledger_uuid"],
                        "source_row_id": row["id"],
                        "source_path": str(source),
                    }
                    added += 1
                else:
                    if not local:
                        _import_row(conn, row, columns)
                        added += 1
                    if origin in linked_origins:
                        # A copied entry no longer needs its source to remain available.
                        conn.execute(
                            "DELETE FROM entry_links WHERE origin_ledger_uuid=? "
                            "AND origin_row_id=?",
                            origin,
                        )
                        removed += 1
            # Recall maintains its own index. Reuse its rebuild path so imports are visible
            # even when a destination had a populated index before this transaction.
            if mode == "import" and added:
                Ledger._fts_ready(conn)
            return identity, added, updated, removed, needs_uuid
    except (sqlite3.Error, OSError) as exc:
        raise LedgerReadError(f"cannot {mode} entries into ledger {destination}: {exc}") from exc


def transfer_entries(
    source: Path,
    destination: Path,
    *,
    ids: list[int] | None = None,
    tag: str | None = None,
    mode: str = "link",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Link or import exactly selected local rows, including unsuccessful attempts.

    A legacy source receives only UUID metadata, and only on an explicit non-dry operation.
    Imported rows retain their original payload and origin even through subsequent copies.
    """
    _validate_selection(ids, tag)
    if mode not in ("link", "import"):
        raise ValueError("mode must be 'link' or 'import'")
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    source, destination = Path(source).absolute(), Path(destination).absolute()
    try:
        same_file = source.resolve() == destination.resolve() or (
            source.exists() and destination.exists() and source.samefile(destination)
        )
    except OSError as exc:
        raise LedgerReadError(f"cannot access transfer paths: {exc}") from exc
    if same_file:
        raise ValueError("source and destination must be different ledger files")
    source_identity, rows = _source_snapshot(source, ids, tag)
    source_needs_uuid = source_identity["ledger_uuid"] is None
    added = None
    updated = None
    removed = None
    if dry_run:
        with _read_connection(destination) as conn:
            destination_identity = _read_identity(conn) if conn is not None else None
            if conn is not None:
                _read_links(conn)
        destination_needs_uuid = (
            destination_identity is None or destination_identity["ledger_uuid"] is None
        )
    else:
        if source_needs_uuid:
            source_identity, rows = _assign_source_identity(source, ids, tag)
        destination_identity, added, updated, removed, destination_needs_uuid = _apply_transfer(
            source, destination, source_identity, rows, mode
        )
    assignments = []
    if source_needs_uuid:
        assignments.append("source")
    if destination_needs_uuid:
        assignments.append("destination")
    return {
        "action": f"would-{mode}" if dry_run else mode,
        "mode": mode,
        "dry_run": dry_run,
        "count": len(rows),
        "added": added,
        "updated_links": updated,
        "removed_links": removed,
        "skipped": len(rows) - added - (updated or 0) if added is not None else None,
        "rows": rows,
        "origins": [row.get("origin") for row in rows],
        "source": str(source),
        "destination": str(destination),
        "source_identity": source_identity,
        "destination_identity": destination_identity,
        "would_assign_uuid": bool(dry_run and assignments),
        "uuid_assignments": assignments,
        "notes": [f"would-assign-UUID: {name}" for name in assignments] if dry_run else [],
    }

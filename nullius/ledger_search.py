"""Explicit preparation and read-only Loogle search of ledger theorems."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import ROOT, Config, ConfigError
from .ledger import LedgerReadError, read_verified_rows
from .repl import ReplError, Session
from .search import LoogleSession, SearchResult
from .verify import Verifier

FORMAT_VERSION = 1
EXPORT_MARKER = "NULLIUS_LEDGER_EXPORT "
RESULT_PREFIX = "NulliusLedgerResults."
_sessions: dict[tuple[str, ...], LoogleSession] = {}
_session_lock = threading.RLock()


class LedgerSearchError(RuntimeError):
    pass


class BuildFailure(LedgerSearchError):
    """A compiler failure for a candidate; unlike a timeout, this can be reported per entry."""


def _digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LedgerSearchError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LedgerSearchError(f"expected an object in {path}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LedgerSearchError("ledger refresh timed out; no new snapshot was published")
    return remaining


def _run(
    command: list[str],
    cwd: Path,
    deadline: float,
    config: Config,
    *,
    environment: dict[str, str] | None = None,
) -> str:
    env = {**os.environ, **(environment or {}), "ELAN_TOOLCHAIN": config.toolchain()}
    env.setdefault("LEAN_NUM_THREADS", str(config.lean_threads))
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    ) as proc:
        try:
            output, _ = proc.communicate(timeout=_remaining(deadline))
        except (subprocess.TimeoutExpired, LedgerSearchError) as exc:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise LedgerSearchError("ledger refresh timed out; child processes stopped") from exc
        if proc.returncode:
            raise BuildFailure(f"{Path(command[0]).name} failed:\n{output[-6000:]}")
        return output


@contextmanager
def _build_lock(path: Path, deadline: float) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.1, _remaining(deadline)))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _loogle_dir(config: Config) -> Path:
    if not config.loogle_bin or not config.loogle_bin.is_file():
        raise LedgerSearchError("local Loogle is unavailable; run scripts/build-loogle.sh")
    if len(config.loogle_bin.resolve().parents) < 4:
        raise LedgerSearchError("ledger indexing requires a built Loogle checkout")
    directory = config.loogle_bin.resolve().parents[3]
    if not (directory / "Loogle.lean").is_file():
        raise LedgerSearchError(
            "ledger indexing requires the checkout containing the Loogle binary"
        )
    return directory


def _package_paths(config: Config) -> list[tuple[dict[str, Any], Path]]:
    manifest = _read_json(config.lean_dir / "lake-manifest.json")
    if not isinstance(manifest.get("packages"), list):
        raise LedgerSearchError("invalid Lake manifest: packages must be a list")
    packages = []
    for package in manifest["packages"]:
        if (
            not isinstance(package, dict)
            or not isinstance(package.get("name"), str)
            or package.get("type") not in ("path", "git")
        ):
            raise LedgerSearchError("invalid dependency entry in Lake manifest")
        if package["type"] == "path":
            if not isinstance(package.get("dir"), str):
                raise LedgerSearchError("path dependency is missing its directory")
            path = config.lean_dir / package["dir"]
        else:
            package_dir = manifest.get("packagesDir")
            if not isinstance(package_dir, str):
                raise LedgerSearchError("Lake manifest is missing packagesDir")
            path = config.lean_dir / package_dir / package["name"].strip("«»")
            if package.get("subDir"):
                if not isinstance(package["subDir"], str):
                    raise LedgerSearchError("dependency subDir must be a path")
                path /= package["subDir"]
        if not path.is_dir():
            raise LedgerSearchError(f"missing built dependency: {path}")
        package = dict(package)
        package.setdefault(
            "configFile", "lakefile.toml" if (path / "lakefile.toml").is_file() else "lakefile.lean"
        )
        packages.append((package, path.resolve()))
    return packages


def _tree_states(roots: list[Path]) -> list[tuple[str, int, int, int]]:
    states = []
    for root in sorted(set(roots)):
        for directory, children, files in os.walk(root):
            children[:] = sorted(
                n
                for n in children
                if n
                not in (
                    ".git",
                    ".lake",
                    ".nullius",
                    ".venv",
                    "__pycache__",
                    "build",
                    "dist",
                )
            )
            for name in sorted(files):
                if (
                    name.endswith((".lean", ".toml", ".json", ".py", ".sh", ".c", ".h"))
                    or name == "lean-toolchain"
                ):
                    path = Path(directory) / name
                    stat = path.stat()
                    states.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        compiled = root / ".lake/build/lib/lean"
        if compiled.is_dir():
            for directory, children, files in os.walk(compiled):
                children.sort()
                for name in sorted(files):
                    if name.endswith(
                        (".olean", ".olean.private", ".olean.server", ".trace", ".so", ".dylib")
                    ):
                        path = Path(directory) / name
                        stat = path.stat()
                        states.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return states


def _environment_key(config: Config) -> str:
    """Include file state as well as pins, so local library edits invalidate snapshots."""
    roots = [config.lean_dir.resolve(), _loogle_dir(config)]
    roots += [path for _, path in _package_paths(config)]
    states = _tree_states(roots)
    policy = [
        _file_hash(ROOT / file)
        for file in (
            "nullius/verify.py",
            "nullius/guard.py",
            "nullius/repl.py",
            "nullius/ledger_search.py",
            "scripts/ledger_index.lean",
        )
    ]
    assert config.loogle_bin is not None
    binary = config.loogle_bin.stat()
    repl = config.repl_bin.stat()
    return _digest(
        [
            FORMAT_VERSION,
            states,
            policy,
            config.loogle_module,
            str(config.lake_bin),
            binary.st_size,
            binary.st_mtime_ns,
            binary.st_ctime_ns,
            str(config.repl_bin.resolve()),
            repl.st_size,
            repl.st_mtime_ns,
            repl.st_ctime_ns,
        ]
    )


def _cache_dir(config: Config) -> Path:
    root = config.ledger_index_dir or (config.lean_dir.parent / "build/ledger-search")
    return root.resolve() / _digest(str(config.ledger_path.resolve()))[:24]


def _session_owner(config: Config) -> tuple[str, ...]:
    return (
        str(config.lean_dir.resolve()),
        config.project_id or str(config.ledger_path.resolve()),
        "project" if config.project_id else str(_cache_dir(config)),
        str(config.loogle_bin),
    )


def _group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row.get("source"), str) or not isinstance(row.get("target"), str):
            raise LedgerSearchError(f"ledger #{row.get('id')} is missing source or target text")
        key = _digest([row["source"], row["target"]])
        grouped.setdefault(key, []).append(row)
    return grouped


def _write_if_changed(path: Path, text: str) -> None:
    if path.exists() and path.read_text() == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _prepare_workspace(workspace: Path, config: Config) -> None:
    loogle = _loogle_dir(config)
    _write_if_changed(workspace / "lean-toolchain", config.toolchain() + "\n")
    _write_if_changed(
        workspace / "lakefile.toml",
        'name = "nulliusLedger"\nversion = "0.1.0"\n'
        '[[lean_lib]]\nname = "Ledger"\n'
        f'[[require]]\nname = "nullius"\npath = {json.dumps(str(config.lean_dir.resolve()))}\n'
        f'[[require]]\nname = "loogle"\npath = {json.dumps(str(loogle))}\n',
    )
    packages = []
    for pkg, path in _package_paths(config):
        packages.append(
            {
                "name": pkg["name"],
                "type": "path",
                "dir": str(path),
                "inherited": True,
                "configFile": pkg["configFile"],
                "manifestFile": pkg.get("manifestFile", "lake-manifest.json"),
                "scope": pkg.get("scope", ""),
            }
        )
    for name, path, file in (
        ("nullius", config.lean_dir.resolve(), "lakefile.toml"),
        ("loogle", loogle, "lakefile.lean"),
    ):
        packages.append(
            {
                "name": name,
                "type": "path",
                "dir": str(path),
                "inherited": False,
                "configFile": file,
                "manifestFile": "lake-manifest.json",
                "scope": "",
            }
        )
    _write_if_changed(
        workspace / "lake-manifest.json",
        json.dumps(
            {
                "version": "1.2.0",
                "name": "nulliusLedger",
                "lakeDir": ".lake",
                "packagesDir": ".lake/packages",
                "packages": packages,
            }
        ),
    )


def _lake(config: Config, workspace: Path) -> list[str]:
    return [str(config.lake_bin), "-d", str(workspace), "--offline", "--no-cache"]


def _artifacts(workspace: Path, module: str) -> dict[str, str]:
    stem = workspace / ".lake/build/lib/lean" / module.replace(".", "/")
    return {
        str(path): _file_hash(path)
        for suffix in (".olean", ".olean.private", ".olean.server", ".trace")
        if (path := stem.with_suffix(suffix)).is_file()
    }


def _intact(artifacts: Any) -> bool:
    if not isinstance(artifacts, dict) or not all(
        isinstance(path, str) and isinstance(expected, str) for path, expected in artifacts.items()
    ):
        raise LedgerSearchError("malformed artifact metadata; refresh the ledger index")
    return bool(artifacts) and all(
        Path(path).is_file() and _file_hash(Path(path)) == expected
        for path, expected in artifacts.items()
    )


def _load_snapshot(
    cache: Path,
    environment: str,
    groups: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    pointer = cache / "current.json"
    if not pointer.exists():
        return None
    snapshot = _read_json(pointer)
    if snapshot.get("format") != FORMAT_VERSION or snapshot.get("environment") != environment:
        return None
    if snapshot.get("candidates") != sorted(groups):
        return None
    if not _intact(snapshot.get("artifacts", {})):
        return None
    if not isinstance(snapshot.get("entries"), list) or not isinstance(
        snapshot.get("excluded"), list
    ):
        raise LedgerSearchError("malformed ledger index metadata; refresh required")
    for entry in snapshot["entries"]:
        if (
            not isinstance(entry, dict)
            or not all(
                isinstance(entry.get(k), str)
                for k in (
                    "key",
                    "module",
                    "name",
                    "statement",
                    "source_sha256",
                )
            )
            or not entry["name"].startswith(RESULT_PREFIX)
            or not isinstance(entry.get("warnings"), list)
            or entry["key"] not in groups
        ):
            raise LedgerSearchError("malformed ledger target metadata; refresh required")
    for entry in snapshot["excluded"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("key"), str)
            or not isinstance(entry.get("reason"), str)
            or entry["key"] not in groups
        ):
            raise LedgerSearchError("malformed ledger exclusion metadata; refresh required")
    if not all(
        isinstance(snapshot.get(k), str)
        for k in (
            "generation",
            "workspace",
            "module",
            "index_file",
        )
    ) or not isinstance(snapshot.get("provenance"), dict):
        raise LedgerSearchError("malformed ledger snapshot metadata; refresh required")
    runtime = snapshot.get("runtime_env")
    if not isinstance(runtime, dict) or not all(
        k in ("LEAN_PATH", "LEAN_SYSROOT", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")
        and isinstance(v, str)
        for k, v in runtime.items()
    ):
        raise LedgerSearchError("malformed ledger runtime environment; refresh required")
    return snapshot


def _export_entry(
    key: str,
    row: dict[str, Any],
    config: Config,
    workspace: Path,
    verifier: Verifier,
    deadline: float,
) -> dict[str, Any]:
    source, target = row["source"], row["target"]
    actual_hash = hashlib.sha256(source.encode()).hexdigest()
    if row["source_sha256"] != actual_hash:
        raise BuildFailure("recorded source hash does not match the stored source")
    verdict = verifier.verify(source, target=target, timeout=_remaining(deadline))
    if not verdict.verified:
        raise BuildFailure(verdict.feedback())
    module = f"Ledger.E{key}"
    text = (
        "import Mathlib\nimport Physlib\nimport Cslib\nimport Nullius.LedgerExport\n\n"
        "#ledger_begin\n" + source + "\n\n"
        f'#ledger_export {json.dumps(target, ensure_ascii=False)} "E{key}"\n'
    )
    _write_if_changed(workspace / "Ledger" / f"E{key}.lean", text)
    output = _run(
        [*_lake(config, workspace), "--rehash", "build", module], config.lean_dir, deadline, config
    )
    records = []
    for line in output.splitlines():
        if EXPORT_MARKER in line:
            records.append(json.loads(line.split(EXPORT_MARKER, 1)[1]))
    if len(records) != 1 or not records[0].get("name", "").startswith(RESULT_PREFIX):
        raise BuildFailure("export did not return exactly one audited target")
    return {
        "key": key,
        "module": module,
        "name": records[0]["name"],
        "statement": records[0]["statement"],
        "axioms": records[0]["axioms"],
        "source_sha256": actual_hash,
        "warnings": verdict.warnings,
        "artifacts": _artifacts(workspace, module),
    }


def _refresh(
    config: Config,
    cache: Path,
    groups: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    if not math.isfinite(config.ledger_refresh_timeout) or config.ledger_refresh_timeout <= 0:
        raise LedgerSearchError("ledger refresh timeout must be finite and positive")
    deadline = time.monotonic() + config.ledger_refresh_timeout
    with _build_lock(cache / "build.lock", deadline):
        repairs: list[str] = []
        environment = _environment_key(config)
        try:
            current = _load_snapshot(cache, environment, groups)
        except LedgerSearchError as exc:
            # Refresh may rebuild derived cache metadata, never the source ledger.
            repairs.append(str(exc))
            current = None
        if current is not None:
            return current
        # Build the adapter against the existing project, never update its dependencies.
        _run(
            [*_lake(config, config.lean_dir), "build", "NulliusAll", "Nullius.LedgerExport"],
            config.lean_dir,
            deadline,
            config,
        )
        environment = _environment_key(config)
        workspace = cache / environment / "workspace"
        _prepare_workspace(workspace, config)
        entries: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        session = Session(config)
        try:
            verifier = Verifier(session, config)
            for key, rows in sorted(groups.items()):
                entry_path = cache / environment / "entries" / f"{key}.json"
                try:
                    entry = _read_json(entry_path) if entry_path.exists() else None
                    intact = entry is not None and _intact(entry.get("artifacts", {}))
                except LedgerSearchError as exc:
                    repairs.append(str(exc))
                    entry, intact = None, False
                if entry is not None and intact:
                    entries.append(entry)
                    continue
                try:
                    entry = _export_entry(key, rows[0], config, workspace, verifier, deadline)
                except BuildFailure as exc:
                    excluded.append({"key": key, "reason": str(exc)})
                    continue
                _atomic_json(entry_path, entry)
                entries.append(entry)
        finally:
            session.close()
        generation = _digest([environment, sorted(groups), entries, excluded])
        module = f"Ledger.S{generation}"
        root_source = "import NulliusAll\n" + "".join(
            f"import {entry['module']}\n" for entry in entries
        )
        _write_if_changed(workspace / "Ledger" / f"S{generation}.lean", root_source)
        _run([*_lake(config, workspace), "build", module], config.lean_dir, deadline, config)
        directory = cache / environment / "snapshots" / generation
        directory.mkdir(parents=True, exist_ok=True)
        targets = directory / "targets.json"
        _write_if_changed(targets, json.dumps([entry["name"] for entry in entries]))
        index_file = directory / "ledger.loogle-index"
        trace = workspace / ".lake/build/lib/lean/Ledger" / f"S{generation}.trace"
        _run(
            [
                *_lake(config, workspace),
                "env",
                "lean",
                "--run",
                str(ROOT / "scripts/ledger_index.lean"),
                module,
                str(trace),
                str(index_file),
                str(targets),
            ],
            config.lean_dir,
            deadline,
            config,
        )
        env_code = (
            "import os,json; print(json.dumps({k:os.environ[k] for k in "
            "('LEAN_PATH','LEAN_SYSROOT','LD_LIBRARY_PATH','DYLD_LIBRARY_PATH') "
            "if k in os.environ}))"
        )
        runtime_env = json.loads(
            _run(
                [*_lake(config, workspace), "env", sys.executable, "-c", env_code],
                config.lean_dir,
                deadline,
                config,
            )
        )
        artifacts = _artifacts(workspace, module)
        artifacts[str(index_file)] = _file_hash(index_file)
        for entry in entries:
            artifacts.update(entry["artifacts"])
        # A concurrent library edit must not produce a cache labeled as current.
        if _environment_key(config) != environment:
            raise LedgerSearchError("library files changed during refresh; retry the refresh")
        snapshot = {
            "format": FORMAT_VERSION,
            "environment": environment,
            "generation": generation,
            "candidates": sorted(groups),
            "entries": entries,
            "excluded": excluded,
            "workspace": str(workspace),
            "module": module,
            "index_file": str(index_file),
            "runtime_env": runtime_env,
            "artifacts": artifacts,
            "provenance": config.provenance(),
            "repairs": repairs,
        }
        _atomic_json(directory / "manifest.json", snapshot)
        _atomic_json(cache / "current.json", snapshot)
        return snapshot


def _query(
    query: str,
    limit: int,
    timeout: float,
    config: Config,
    snapshot: dict[str, Any],
) -> SearchResult:
    owner = _session_owner(config)
    scope = snapshot.get("scope", "snippet")
    family = (*owner, scope)
    key = (*family, snapshot["generation"])
    with _session_lock:
        for previous in list(_sessions):
            if previous[:-1] == family and previous != key:
                _sessions.pop(previous).close()
        if key not in _sessions:
            env = {**os.environ, **snapshot["runtime_env"]}
            _sessions[key] = LoogleSession(
                binary=config.loogle_bin,
                lake_bin=config.lake_bin,
                lean_dir=Path(snapshot["workspace"]),
                module=snapshot["module"],
                index_mode="read",
                index_file=Path(snapshot["index_file"]),
                environment=env,
                direct=True,
            )
        session = _sessions[key]
        # Filter inside Loogle, before its 200-result cap and before Python's limit.
        result = session.query(f'"{RESULT_PREFIX}", {query}', limit=limit, timeout=timeout)
    result.query = query
    result.backend = "loogle-ledger"
    return result


def search_ledger(
    query: str,
    limit: int = 8,
    timeout: float = 20.0,
    *,
    refresh: bool = False,
    config: Config | None = None,
) -> SearchResult:
    try:
        config = config or Config.discover()
        if config.project_id:
            from .ledger_transfer import read_project_rows

            rows = read_project_rows(config.ledger_path)
        else:
            rows = read_verified_rows(config.ledger_path)
        rows = [r for r in rows if (r.get("record_kind") or "snippet") == "snippet"]
        if not rows:
            return SearchResult(query, "loogle-ledger", note="No verified snippet entries.")
        groups = _group_rows(rows)
        cache = _cache_dir(config)
        snapshot = (
            _refresh(config, cache, groups)
            if refresh
            else _load_snapshot(cache, _environment_key(config), groups)
        )
        if snapshot is None:
            return SearchResult(
                query,
                "loogle-ledger",
                error=(
                    "ledger index is missing or outdated; "
                    "use --refresh-ledger (refresh_ledger=true)"
                ),
            )
        result = _query(query, limit, timeout, config, snapshot)
        by_name = {entry["name"]: entry for entry in snapshot["entries"]}
        for hit in result.hits:
            if hit.name not in by_name:
                raise LedgerSearchError("ledger index returned a target outside its manifest")
            entry = by_name[hit.name]
            matching_rows = groups[entry["key"]]
            latest = matching_rows[-1]
            hit.source = "loogle-ledger"
            exported_name = hit.name
            hit.name = latest["target"]
            hit.ledger = {
                "ids": [r["id"] for r in matching_rows],
                "references": [r["reference"] for r in matching_rows if r.get("reference")],
                "project_id": config.project_id,
                "record_kind": "snippet",
                "target": latest["target"],
                "claim": latest.get("claim"),
                "tag": latest.get("tag"),
                "source_sha256": entry["source_sha256"],
                "exported_name": exported_name,
                "statement": entry["statement"],
                "warnings": entry["warnings"],
                "recorded_provenance": {
                    k: latest.get(k)
                    for k in (
                        "toolchain",
                        "mathlib_rev",
                        "physlib_rev",
                        "cslib_rev",
                    )
                },
                "indexed_provenance": snapshot["provenance"],
            }
        excluded = [
            {"ids": [r["id"] for r in groups[e["key"]]], "reason": e["reason"]}
            for e in snapshot["excluded"]
        ]
        result.index = {
            "generation": snapshot["generation"],
            "targets": len(snapshot["entries"]),
            "excluded": excluded,
            "repairs": snapshot.get("repairs", []),
        }
        if snapshot.get("repairs"):
            result.note += "\nCache metadata was rebuilt: " + "; ".join(snapshot["repairs"])
        if excluded:
            explanation = "; ".join(f"{e['ids']}: {e['reason']}" for e in excluded)
            result.note += f"\nExcluded {len(excluded)} candidate(s): {explanation}"
            if not snapshot["entries"]:
                result.error = "No candidate passed rechecking/export. " + explanation
        return result
    except (LedgerSearchError, LedgerReadError, ConfigError, ReplError, OSError, ValueError) as exc:
        return SearchResult(query, "loogle-ledger", error=str(exc))


def close_ledger_sessions(config: Config | None = None) -> None:
    with _session_lock:
        for key in list(_sessions):
            if config is None or key[:-2] == _session_owner(config):
                _sessions.pop(key).close()

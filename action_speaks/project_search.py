"""Search current project declarations referenced by its ledger."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from .config import ROOT, Config
from .ledger import LedgerReadError
from .ledger_search import (
    EXPORT_MARKER,
    LedgerSearchError,
    _artifacts,
    _atomic_json,
    _build_lock,
    _digest,
    _environment_key,
    _file_hash,
    _lake,
    _load_snapshot,
    _prepare_workspace,
    _query,
    _read_json,
    _remaining,
    _run,
    _write_if_changed,
)
from .project_check import (
    dependency_paths,
    environment_id,
    project_root,
    verify_project,
)
from .search import SearchResult


def _groups(config: Config) -> dict[str, list[dict[str, Any]]]:
    from .ledger_transfer import read_project_rows

    rows = read_project_rows(config.ledger_path)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("record_kind") != "project" or row.get("project_id") != config.project_id:
            continue
        if not isinstance(row.get("module"), str) or not isinstance(row.get("target"), str):
            raise LedgerSearchError("project ledger entry is missing module or target")
        key = _digest([row["module"], row["target"]])
        groups.setdefault(key, []).append(row)
    return groups


def _key(config: Config) -> str:
    return _digest(
        [
            environment_id(config),
            _environment_key(config),
            _file_hash(ROOT / "action_speaks/project_search.py"),
        ]
    )


def _workspace(config: Config, path: Path) -> None:
    _prepare_workspace(path, config)
    root = project_root(config, execution=True)
    project_manifest = _read_json(root / "lake-manifest.json")
    name = project_manifest.get("name")
    if not isinstance(name, str) or not name:
        raise LedgerSearchError("project manifest must record its package name")
    manifest = _read_json(path / "lake-manifest.json")
    existing = {p["name"]: p for p in manifest["packages"]}
    if name in existing and Path(existing[name]["dir"]).resolve() != root:
        raise LedgerSearchError(f"project package name {name} conflicts with a verifier dependency")
    for pkg, directory in dependency_paths(config):
        if pkg["name"] not in existing:
            entry = {
                "name": pkg["name"],
                "type": "path",
                "dir": str(directory),
                "inherited": True,
                "configFile": pkg["configFile"],
                "manifestFile": "lake-manifest.json",
                "scope": pkg.get("scope", ""),
            }
            manifest["packages"].append(entry)
            existing[pkg["name"]] = entry
    if name not in existing:
        manifest["packages"].append(
            {
                "name": name,
                "type": "path",
                "dir": str(root),
                "inherited": False,
                "configFile": "lakefile.toml"
                if (root / "lakefile.toml").exists()
                else "lakefile.lean",
                "manifestFile": "lake-manifest.json",
                "scope": "",
            }
        )
        text = (path / "lakefile.toml").read_text()
        text += f"\n[[require]]\nname = {json.dumps(name)}\npath = {json.dumps(str(root))}\n"
        _write_if_changed(path / "lakefile.toml", text)
    _write_if_changed(path / "lake-manifest.json", json.dumps(manifest))


def _refresh(
    config: Config,
    cache: Path,
    groups: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    project_root(config, execution=True)
    deadline = time.monotonic() + config.ledger_refresh_timeout
    with _build_lock(cache / "build.lock", deadline):
        existing = _load_snapshot(cache, _key(config), groups)
        if existing is not None:
            return existing
        _run(
            [*_lake(config, config.lean_dir), "build", "ActionSpeaks.LedgerExport"],
            config.lean_dir,
            deadline,
            config,
        )
        before = _key(config)
        accepted = []
        excluded = []
        for key, rows in sorted(groups.items()):
            row = rows[-1]
            verdict = verify_project(
                config, row["module"], row["target"], timeout=_remaining(deadline)
            )
            if not verdict.verified:
                excluded.append({"key": key, "reason": verdict.feedback()})
                continue
            accepted.append((key, row, verdict))
        workspace = cache / before / "workspace"
        _workspace(config, workspace)
        module = "Ledger.P" + _digest([before, sorted(groups)])
        text = "import ActionSpeaksAll\n"
        text += "".join(f"import {m}\n" for m in sorted({r["module"] for _, r, _ in accepted}))
        text += "import ActionSpeaks.LedgerExport\n\n"
        for key, row, _ in accepted:
            target = json.dumps(row["target"], ensure_ascii=False)
            text += f'#ledger_begin\n#ledger_export {target} "E{key}"\n'
        _write_if_changed(workspace / Path(module.replace(".", "/") + ".lean"), text)
        output = _run(
            [*_lake(config, workspace), "build", module], config.lean_dir, deadline, config
        )
        exported = {}
        for line in output.splitlines():
            if EXPORT_MARKER in line:
                record = json.loads(line.split(EXPORT_MARKER, 1)[1])
                for key, _, _ in accepted:
                    if record["name"].startswith(f"ActionSpeaksLedgerResults.E{key}."):
                        if key in exported:
                            raise LedgerSearchError("duplicate exported project target")
                        exported[key] = record
        if set(exported) != {key for key, _, _ in accepted}:
            raise LedgerSearchError("project export did not return the expected targets")
        entries = [
            {
                "key": key,
                "module": row["module"],
                "name": exported[key]["name"],
                "statement": verdict.statement,
                "source_sha256": verdict.source_sha256,
                "warnings": verdict.warnings,
                "provenance": verdict.provenance,
            }
            for key, row, verdict in accepted
        ]
        targets = workspace / "targets.json"
        _write_if_changed(targets, json.dumps([e["name"] for e in entries]))
        index_file = workspace / "project.loogle-index"
        trace = workspace / ".lake/build/lib/lean" / (module.replace(".", "/") + ".trace")
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
        code = (
            "import os,json; print(json.dumps({k:os.environ[k] for k in "
            "('LEAN_PATH','LEAN_SYSROOT','LD_LIBRARY_PATH','DYLD_LIBRARY_PATH') "
            "if k in os.environ}))"
        )
        env = json.loads(
            _run(
                [*_lake(config, workspace), "env", sys.executable, "-c", code],
                config.lean_dir,
                deadline,
                config,
            )
        )
        if _key(config) != before:
            raise LedgerSearchError("project changed during index refresh; retry after building it")
        artifacts = _artifacts(workspace, module)
        artifacts[str(index_file)] = _file_hash(index_file)
        snapshot = {
            "format": 1,
            "environment": before,
            "generation": _digest([before, sorted(groups)]),
            "candidates": sorted(groups),
            "entries": entries,
            "excluded": excluded,
            "workspace": str(workspace),
            "module": module,
            "index_file": str(index_file),
            "runtime_env": env,
            "artifacts": artifacts,
            "provenance": config.provenance(),
            "scope": "project",
        }
        _atomic_json(cache / "current.json", snapshot)
        return snapshot


def search_project_ledger(
    query: str,
    limit: int,
    timeout: float,
    *,
    config: Config,
    refresh: bool = False,
) -> SearchResult:
    try:
        groups = _groups(config)
        if not groups:
            return SearchResult(
                query,
                "project-ledger",
                note="No verified target references belonging to the selected project.",
            )
        project_root(config, execution=True)
        cache = (
            config.ledger_index_dir or project_root(config) / ".action-speaks/indexes"
        ) / "project"
        snapshot = (
            _refresh(config, cache, groups)
            if refresh
            else _load_snapshot(cache, _key(config), groups)
        )
        if snapshot is None:
            return SearchResult(
                query, "project-ledger", error="project index needs --refresh-ledger"
            )
        result = _query(query, limit, timeout, config, snapshot)
        result.backend = "project-ledger"
        entries = {e["name"]: e for e in snapshot["entries"]}
        for hit in result.hits:
            entry = entries[hit.name]
            history = groups[entry["key"]]
            row = history[-1]
            hit.name = row["target"]
            hit.source = "project-ledger"
            hit.signature = entry["statement"]
            hit.ledger = {
                "ids": [r["id"] for r in history],
                "references": [r.get("reference") for r in history if r.get("reference")],
                "record_kind": "project",
                "project_id": config.project_id,
                "project_name": config.project_name,
                "module": row["module"],
                "target": row["target"],
                "indexed_provenance": entry["provenance"],
                "history": [
                    {
                        "reference": r.get("reference"),
                        "environment_id": r.get("environment_id"),
                        "statement": r.get("statement"),
                    }
                    for r in history
                ],
            }
        exclusions = [
            {"references": [r.get("reference") for r in groups[e["key"]]], "reason": e["reason"]}
            for e in snapshot["excluded"]
        ]
        result.index = {"targets": len(entries), "excluded": exclusions}
        result.note += (
            "\nCurrent project declarations; historical ledger entries retain their original "
            "statements and environments. Reverify the project target before reuse."
        )
        if exclusions:
            result.note += "\nExcluded: " + "; ".join(e["reason"] for e in exclusions)
            if not entries:
                result.error = result.note
        return result
    except (LedgerSearchError, LedgerReadError, ValueError, OSError) as exc:
        return SearchResult(query, "project-ledger", error=str(exc))

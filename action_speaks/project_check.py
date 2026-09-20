"""Check named declarations in registered Lean projects, without source snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import ROOT, Config
from .ledger_search import (
    BuildFailure,
    LedgerSearchError,
    _digest,
    _package_paths,
    _read_json,
    _run,
    _tree_states,
)
from .verify import Check, Status, Verdict

MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
MARKER = "ACTION_SPEAKS_PROJECT "


def project_root(config: Config, *, execution: bool = False) -> Path:
    if config.project_root is None or config.project_id is None:
        raise ValueError("select a registered project with --project")
    if execution and not config.project_trusted:
        raise ValueError("project execution is not approved; register it with --trust first")
    root = config.project_root.resolve()
    if (root / "lean-toolchain").read_text().strip() != config.toolchain():
        raise ValueError("project and verifier must use the same Lean toolchain")
    if not (root / "lake-manifest.json").is_file():
        raise ValueError("project has no lake-manifest.json; prepare its locked dependencies first")
    return root


def module_source(config: Config, module: str) -> Path:
    if not MODULE.fullmatch(module):
        raise ValueError("module must be a dotted Lean module name")
    path = project_root(config) / (module.replace(".", "/") + ".lean")
    if not path.is_file():
        trace = (
            project_root(config) / ".lake/build/lib/lean" / (module.replace(".", "/") + ".trace")
        )
        if trace.is_file():
            for item in _read_json(trace).get("inputs", []):
                if isinstance(item, list) and item and isinstance(item[0], str):
                    candidate = Path(item[0])
                    if (
                        candidate.suffix == ".lean"
                        and candidate.is_file()
                        and candidate.resolve().is_relative_to(project_root(config))
                    ):
                        return candidate
        raise ValueError(f"project module source not found: {path}")
    return path


def dependency_paths(config: Config) -> list[tuple[dict[str, Any], Path]]:
    return _package_paths(replace(config, lean_dir=project_root(config)))


def source_files(root: Path) -> list[Path]:
    files = []
    for directory, children, names in os.walk(root):
        children[:] = sorted(
            n
            for n in children
            if n
            not in (
                ".git",
                ".lake",
                ".action-speaks",
                ".venv",
                "__pycache__",
                "build",
                "dist",
            )
        )
        for name in sorted(names):
            if (
                name.endswith((".lean", ".toml", ".json", ".py", ".sh", ".c", ".h"))
                or name == "lean-toolchain"
            ):
                files.append(Path(directory) / name)
    return sorted(files)


def _source_hash(root: Path) -> str:
    h = hashlib.sha256()
    for file in source_files(root):
        h.update(str(file.relative_to(root)).encode())
        h.update(hashlib.sha256(file.read_bytes()).digest())
    return h.hexdigest()


def check_dependencies(config: Config) -> None:
    """Shared library names must refer to the verifier's sources, not substitutes."""
    base = {pkg["name"]: path for pkg, path in _package_paths(config)}
    for pkg, path in dependency_paths(config):
        other = base.get(pkg["name"])
        if other is not None and path != other and _source_hash(path) != _source_hash(other):
            raise ValueError(
                f"project dependency {pkg['name']} differs from the verifier; "
                "use compatible, unmodified dependency sources"
            )


def environment_id(config: Config) -> str:
    root = project_root(config)
    roots = {root, *(path for _, path in dependency_paths(config))}
    roots.update(path for _, path in _package_paths(config))
    roots.add(config.lean_dir.resolve())
    policy = [
        hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "scripts/project_audit.lean",
            "action_speaks/project_check.py",
            "action_speaks/verify.py",
            "lean/ActionSpeaks/Audit.lean",
        )
    ]
    return _digest([config.project_id, config.toolchain(), _tree_states(list(roots)), policy])


def _git_info(root: Path) -> dict[str, str]:
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if head.returncode:
        return {
            "git_commit": "",
            "git_dirty": "unknown",
            "git_status_error": head.stderr.strip(),
        }
    status = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude).action-speaks",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if status.returncode:
        raise ValueError(f"cannot determine project Git state: {status.stderr.strip()}")
    return {"git_commit": head.stdout.strip(), "git_dirty": str(bool(status.stdout)).lower()}


def _lake(config: Config, root: Path) -> list[str]:
    return [str(config.lake_bin), "-d", str(root), "--offline", "--no-cache"]


def runtime_environment(config: Config, deadline: float) -> dict[str, str]:
    root = project_root(config, execution=True)
    code = (
        "import os,json; print(json.dumps({k:os.environ[k] for k in "
        "('LEAN_PATH','LEAN_SYSROOT','LD_LIBRARY_PATH','DYLD_LIBRARY_PATH') "
        "if k in os.environ}))"
    )
    base = json.loads(
        _run(
            [*_lake(config, config.lean_dir), "env", sys.executable, "-c", code],
            config.lean_dir,
            deadline,
            config,
        )
    )
    project = json.loads(
        _run(
            [*_lake(config, root), "env", sys.executable, "-c", code],
            root,
            deadline,
            config,
        )
    )
    result = dict(base)
    result["ACTION_SPEAKS_PROJECT_LEAN_PATH"] = project.get("LEAN_PATH", "")
    for key in ("LEAN_PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        paths = list(
            dict.fromkeys(
                p
                for text in (base.get(key, ""), project.get(key, ""))
                for p in text.split(os.pathsep)
                if p
            )
        )
        if paths:
            result[key] = os.pathsep.join(paths)
    return result


def verify_project(
    config: Config,
    module: str,
    target: str,
    *,
    claim: str | None = None,
    require_nontrivial: bool = False,
    build: bool = False,
    timeout: float | None = None,
) -> Verdict:
    if not isinstance(build, bool) or not isinstance(require_nontrivial, bool):
        raise ValueError("build and require_nontrivial must be booleans")
    root = project_root(config, execution=True)
    if not MODULE.fullmatch(module):
        raise ValueError("module must be a dotted Lean module name")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("a target declaration is required")
    duration = config.command_timeout if timeout is None else timeout
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("timeout must be finite and positive")
    start = time.monotonic()
    deadline = start + duration
    v = Verdict(
        status=Status.ERROR,
        target=target,
        claim=claim,
        provenance=config.provenance(),
    )
    prepared = False
    try:
        v.provenance.update(
            {
                "environment_id": environment_id(config),
                "module": module,
                **_git_info(root),
                "require_nontrivial": str(require_nontrivial).lower(),
            }
        )
        check_dependencies(config)
        command = [*_lake(config, root), "--rehash"]
        if not build:
            command += ["--no-build"]
        _run([*command, "build", module], root, deadline, config)
        prepared = True
        source = module_source(config, module)
        v.source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        env = runtime_environment(config, deadline)
        relative = Path(module.replace(".", "/") + ".olean")
        expected = root / ".lake/build/lib/lean" / relative
        found = next(
            (
                Path(p) / relative
                for p in env["LEAN_PATH"].split(os.pathsep)
                if (Path(p) / relative).is_file()
            ),
            None,
        )
        if found is None or found.resolve() != expected.resolve():
            raise ValueError("project module is missing or shadows a verifier dependency")
        before = environment_id(config)
        v.provenance.update(
            {
                "environment_id": before,
                "module": module,
                "source_path": str(source.relative_to(root)),
                **_git_info(root),
                "lockfile_sha256": hashlib.sha256(
                    (root / "lake-manifest.json").read_bytes()
                ).hexdigest(),
                "require_nontrivial": str(require_nontrivial).lower(),
            }
        )
        prefix = _run(
            [*_lake(config, config.lean_dir), "env", "lean", "--print-prefix"],
            config.lean_dir,
            deadline,
            config,
        ).strip()
        env["ACTION_SPEAKS_PROJECT_LEAN_PATH"] += os.pathsep + str(Path(prefix) / "lib/lean")
        output = _run(
            [
                str(Path(prefix) / "bin/lean"),
                "--run",
                str(ROOT / "scripts/project_audit.lean"),
                module,
                target,
                str(require_nontrivial).lower(),
            ],
            root,
            deadline,
            config,
            environment=env,
        )
        records = [
            json.loads(line[len(MARKER) :])
            for line in output.splitlines()
            if line.startswith(MARKER)
        ]
        if len(records) != 1:
            raise ValueError("project auditor did not return exactly one result")
        record = records[0]
        v.checks.append(Check("elaboration", True))
        if record.get("environment_error"):
            v.checks.append(Check("dependency_compatibility", False, record["environment_error"]))
        elif record.get("replay_error"):
            v.checks.append(Check("kernel_replay", False, record["replay_error"]))
        else:
            v.checks.append(Check("kernel_replay", True, "replayed project declaration groups"))
            v.target = record["target"]
            v.statement = record["statement"]
            v.statement_explicit = record["statement_explicit"]
            v.axioms = record["axioms"]
            v.checks.extend(
                Check(c["name"], c["passed"], c.get("detail", ""), fatal=c.get("fatal", True))
                for c in record["checks"]
            )
        if environment_id(config) != before:
            v.checks.append(
                Check("environment_current", False, "project changed during verification")
            )
        v.status = (
            Status.ERROR
            if record.get("environment_error")
            else Status.REJECTED
            if v.failures
            else Status.VERIFIED
        )
    except (LedgerSearchError, ValueError, OSError, subprocess.SubprocessError) as exc:
        detail = str(exc)
        if isinstance(exc, BuildFailure) and not build and not prepared:
            detail += "\nIf build artifacts are missing or stale, retry with --build."
        v.checks.append(Check("project_environment", False, detail))
    v.elapsed = time.monotonic() - start
    return v

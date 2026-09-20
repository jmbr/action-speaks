"""Stable project identities and local registration; no project code is executed."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

from .ledger import Ledger, LedgerReadError, read_ledger_identity


class ProjectError(ValueError):
    """Project configuration, registration, or ledger identity is inconsistent."""


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    root: Path
    trusted: bool = False
    aliases: tuple[str, ...] = ()

    @property
    def ledger_path(self) -> Path:
        return self.root / ".action-speaks" / "ledger.sqlite3"

    @property
    def index_dir(self) -> Path:
        return self.root / ".action-speaks" / "indexes"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "root": str(self.root),
            "trusted": self.trusted,
            "aliases": list(self.aliases),
            "ledger_path": str(self.ledger_path),
            "index_dir": str(self.index_dir),
        }


def _path(value: str | Path) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectError(f"cannot resolve path {value!s}: {exc}") from exc


def _registry_path(override: Path | None) -> Path:
    if override is not None:
        return _path(override)
    configured = os.environ.get("ACTION_SPEAKS_PROJECT_REGISTRY")
    if configured:
        return _path(configured)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return _path(base / "action-speaks" / "projects.json")


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ProjectError("project ID must be a UUID string")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ProjectError(f"invalid project UUID: {value!r}") from exc


def _name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in (".", "..", "~")
        or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ProjectError("project names and aliases must be nonempty labels, not paths")
    try:
        UUID(value)
    except ValueError:
        return value
    raise ProjectError("project names and aliases cannot be UUIDs")


def _read_json(path: Path, *, optional: bool = False) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if optional and not path.is_symlink():
            return None
        raise ProjectError(f"missing file {path}; register the project first") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectError(f"cannot read project JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError(f"project JSON {path} must be an object")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ProjectError(f"unsupported schema_version in {path}; expected 1")
    return data


def _check_labels(project: Project, projects: dict[str, Project]) -> None:
    labels = {project.name, *project.aliases}
    for other in projects.values():
        if other.id == project.id:
            continue
        shared = labels.intersection((other.name, *other.aliases))
        if shared:
            raise ProjectError(
                f"project label {sorted(shared)[0]!r} is already registered to {other.id}"
            )
        if project.root == other.root:
            raise ProjectError(f"project root {project.root} is already registered to {other.id}")


def _read_registry(path: Path) -> dict[str, Project]:
    data = _read_json(path, optional=True)
    if data is None:
        return {}
    entries = data.get("projects")
    if not isinstance(entries, dict):
        raise ProjectError(f"registry {path} must contain a projects object keyed by UUID")
    projects: dict[str, Project] = {}
    for key, value in entries.items():
        project_id = _uuid(key)
        if project_id != key or not isinstance(value, dict):
            raise ProjectError(f"invalid project entry {key!r} in registry {path}")
        root = value.get("root")
        if (
            not isinstance(root, str)
            or not Path(root).is_absolute()
            or ".." in Path(root).parts
            or "\x00" in root
        ):
            raise ProjectError(f"registry root for {key} must be an absolute resolved path")
        aliases = value.get("aliases", [])
        if not isinstance(aliases, list):
            raise ProjectError(f"registry aliases for {key} must be a list")
        names = tuple(_name(alias) for alias in aliases)
        name = _name(value.get("name"))
        if len(set(names)) != len(names) or name in names:
            raise ProjectError(f"duplicate project names or aliases for {key}")
        trusted = value.get("trusted", False)
        if not isinstance(trusted, bool):
            raise ProjectError(f"registry trusted flag for {key} must be a boolean")
        project = Project(project_id, name, Path(root), trusted, names)
        _check_labels(project, projects)
        projects[key] = project
    return projects


def _validate_root(root: Path) -> None:
    try:
        if not root.is_dir():
            raise ProjectError(f"project root does not exist or is not a directory: {root}")
        if not (root / "lean-toolchain").is_file():
            raise ProjectError(f"project root {root} must contain lean-toolchain")
        if not any((root / name).is_file() for name in ("lakefile.toml", "lakefile.lean")):
            raise ProjectError(f"project root {root} must contain lakefile.toml or lakefile.lean")
        metadata = root / ".action-speaks"
        if metadata.is_symlink() or (metadata.exists() and not metadata.is_dir()):
            raise ProjectError(f"project metadata must be a local directory: {metadata}")
        for name in ("project.json", "ledger.sqlite3"):
            path = metadata / name
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ProjectError(f"project metadata must be a regular local file: {path}")
    except OSError as exc:
        raise ProjectError(f"cannot inspect project root {root}: {exc}") from exc


def _config(root: Path, *, optional: bool = False) -> tuple[str, str] | None:
    data = _read_json(root / ".action-speaks" / "project.json", optional=optional)
    if data is None:
        return None
    return _uuid(data.get("id")), _name(data.get("name"))


def _check_ledger(project: Project, *, binding: bool = False) -> None:
    try:
        identity = read_ledger_identity(project.ledger_path)
    except LedgerReadError as exc:
        raise ProjectError(f"cannot read project ledger: {exc}") from exc
    if identity is None:
        return
    project_id = identity["project_id"]
    if project_id is not None and project_id != project.id:
        raise ProjectError(
            f"ledger {project.ledger_path} belongs to project {project_id}, not {project.id}"
        )
    if project_id is None and not binding:
        raise ProjectError(f"ledger {project.ledger_path} is unbound; register the project first")


def _checked(project: Project) -> Project:
    if _path(project.root) != project.root:
        raise ProjectError(
            f"registered root {project.root} now resolves elsewhere; register the new path"
        )
    _validate_root(project.root)
    config = _config(project.root)
    if config != (project.id, project.name):
        raise ProjectError(
            f"project ID/name at {project.root} does not match registry; "
            "restore its identity or explicitly register it again"
        )
    _check_ledger(project)
    return project


def _select(selector: str | Path, projects: dict[str, Project]) -> Project:
    if isinstance(selector, str):
        for project in projects.values():
            if selector in (project.id, project.name, *project.aliases):
                return _checked(project)
        try:
            project_id = str(UUID(selector))
        except ValueError:
            pass
        else:
            if project_id in projects:
                return _checked(projects[project_id])
        if not selector.strip():
            raise ProjectError("project selector must be a name, UUID, or registered path")
    root = _path(selector)
    for project in projects.values():
        if project.root == root:
            return _checked(project)
    if root.is_dir():
        _validate_root(root)
        config = _config(root, optional=True)
        if config is not None and config[0] in projects:
            raise ProjectError(
                f"project {config[0]} is registered at {projects[config[0]].root}, not {root}; "
                "register the new path to relocate it"
            )
    raise ProjectError(f"project {selector!s} is not registered; register its path first")


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ProjectError(f"project lock must not be a symbolic link: {path}")
        with path.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
    except OSError as exc:
        raise ProjectError(f"cannot update project registration at {path}: {exc}") from exc


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        temp = Path(f.name)
        try:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _write_config(project: Project) -> None:
    path = project.root / ".action-speaks" / "project.json"
    data = {"schema_version": 1, "id": project.id, "name": project.name}
    if _read_json(path, optional=True) != data:
        _atomic_json(path, data)


def _write_registry(path: Path, projects: dict[str, Project]) -> None:
    entries = {
        key: {
            "name": project.name,
            "root": str(project.root),
            "trusted": project.trusted,
            "aliases": list(project.aliases),
        }
        for key, project in projects.items()
    }
    data = {"schema_version": 1, "projects": entries}
    if _read_json(path, optional=True) != data:
        _atomic_json(path, data)


def _registry_outside_metadata(path: Path, root: Path) -> None:
    if path.is_relative_to(root / ".action-speaks"):
        raise ProjectError("local project registry must be outside project .action-speaks metadata")


def _renamed(project: Project, name: str) -> Project:
    aliases = list(project.aliases)
    if project.name != name:
        aliases.append(project.name)
    return replace(
        project, name=name, aliases=tuple(a for a in dict.fromkeys(aliases) if a != name)
    )


def register_project(
    root: Path,
    name: str | None = None,
    *,
    trust: bool = False,
    relocate: bool = False,
    registry_path: Path | None = None,
) -> Project:
    """Register or explicitly reconcile identity; retain names as local aliases."""
    root = _path(root)
    _validate_root(root)
    if name is not None:
        name = _name(name)
    if not isinstance(trust, bool) or not isinstance(relocate, bool):
        raise ProjectError("trust and relocate must be booleans")
    registry = _registry_path(registry_path)
    _registry_outside_metadata(registry, root)
    with _lock(Path(str(registry) + ".lock")):
        projects = _read_registry(registry)
        with _lock(root / ".action-speaks" / "project.lock"):
            _validate_root(root)
            config = _config(root, optional=True)
            occupant = next((p for p in projects.values() if p.root == root), None)
            if occupant is not None and (config is None or config[0] != occupant.id):
                raise ProjectError(f"registered root {root} has a missing or different project ID")
            project_id, old_name = config or (str(uuid4()), _name(name or root.name))
            existing = projects.get(project_id)
            if existing is not None and existing.root != root and not relocate:
                try:
                    existing.root.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise ProjectError(
                        f"project {project_id} is already registered at {existing.root}; "
                        "use relocate=True (--relocate) to select this copy"
                    )
            project = (
                replace(existing, root=root, trusted=existing.trusted or trust)
                if existing is not None
                else Project(project_id, old_name, root, trust)
            )
            project = _renamed(project, old_name)
            project = _renamed(project, name if name is not None else old_name)
            _check_labels(project, projects)
            _check_ledger(project, binding=True)
            # Persist identity before binding so a failed registration can reuse its UUID.
            # Config and registry are separate writes; readers reject interrupted updates.
            _write_config(project)
            try:
                Ledger(project.ledger_path).bind_project(project.id)
            except (LedgerReadError, sqlite3.Error, OSError, ValueError) as exc:
                raise ProjectError(
                    f"cannot bind project ledger {project.ledger_path}: {exc}"
                ) from exc
            projects[project.id] = project
            _write_registry(registry, projects)
            return project


def resolve_project(selector: str | Path, *, registry_path: Path | None = None) -> Project:
    """Resolve a registered name, alias, UUID, or exact root, without writing files."""
    return _select(selector, _read_registry(_registry_path(registry_path)))


def rename_project(
    selector: str | Path, name: str, *, registry_path: Path | None = None
) -> Project:
    """Change the local display name, retaining its old name and all ledger rows."""
    name = _name(name)
    registry = _registry_path(registry_path)
    with _lock(Path(str(registry) + ".lock")):
        projects = _read_registry(registry)
        selected = _select(selector, projects)
        _registry_outside_metadata(registry, selected.root)
        with _lock(selected.root / ".action-speaks" / "project.lock"):
            _checked(selected)
            project = _renamed(selected, name)
            _check_labels(project, projects)
            _write_config(project)
            projects[project.id] = project
            _write_registry(registry, projects)
            return project


def list_projects(*, registry_path: Path | None = None) -> list[Project]:
    """List local registrations, including roots awaiting explicit relocation."""
    return sorted(
        _read_registry(_registry_path(registry_path)).values(), key=lambda p: (p.name, p.id)
    )

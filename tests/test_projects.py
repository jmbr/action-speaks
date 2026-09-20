"""Project registration tests use temporary Lean roots and real temporary ledgers."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nullius.ledger import SCHEMA, Ledger, read_ledger_identity  # noqa: E402
from nullius.projects import (  # noqa: E402
    ProjectError,
    list_projects,
    register_project,
    rename_project,
    resolve_project,
)
from nullius.verify import Verdict  # noqa: E402


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def ledger_identity(path: Path) -> dict[str, Any]:
    identity = read_ledger_identity(path)
    assert identity is not None
    return identity


class TestProject:
    @pytest.fixture(autouse=True)
    def setup(self) -> Iterator[None]:
        with tempfile.TemporaryDirectory() as directory:
            self.base = Path(directory).resolve()
            self.registry = self.base / "registry" / "projects.json"
            self.root = self.lean_project("original")
            with patch.dict(os.environ, {"NULLIUS_PROJECT_REGISTRY": str(self.registry)}):
                yield

    def lean_project(self, name: str, *, lean_lakefile: bool = False) -> Path:
        root = self.base / name
        root.mkdir()
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "lean/lean-toolchain", root / "lean-toolchain"
        )
        (root / ("lakefile.lean" if lean_lakefile else "lakefile.toml")).write_text("")
        return root

    def config(self, root: Path | None = None) -> Path:
        return (root or self.root) / ".nullius" / "project.json"

    def test_create_and_idempotent_registration(self) -> None:
        with patch("subprocess.run", side_effect=AssertionError("must not run a command")):
            project = register_project(self.root, "mechanics")
            assert register_project(self.root) == project
        assert str(UUID(project.id)) == project.id
        assert project.root == self.root
        assert not project.trusted
        assert project.aliases == ()
        assert project.ledger_path == self.root / ".nullius" / "ledger.sqlite3"
        assert project.index_dir == self.root / ".nullius" / "indexes"
        assert not project.index_dir.exists()
        assert json.loads(self.config().read_text()) == {
            "schema_version": 1,
            "id": project.id,
            "name": "mechanics",
        }
        assert ledger_identity(project.ledger_path)["project_id"] == project.id
        assert json.loads(json.dumps(project.to_dict()))["root"] == str(self.root)
        for selector in (project.id, project.id.upper(), "mechanics", self.root, str(self.root)):
            assert resolve_project(selector) == project
        second = register_project(self.lean_project("other", lean_lakefile=True))
        assert second.id != project.id
        assert list_projects() == [project, second]

    def test_trust_only_comes_from_registry(self) -> None:
        (self.root / ".nullius").mkdir()
        self.config().write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": str(uuid4()),
                    "name": "mechanics",
                    "trusted": True,
                    "root": "/untrusted/path",
                }
            )
        )
        project = register_project(self.root)
        assert not project.trusted
        assert "trusted" not in json.loads(self.config().read_text())
        assert "root" not in json.loads(self.config().read_text())
        forged = json.loads(self.config().read_text())
        forged["trusted"] = True
        self.config().write_text(json.dumps(forged))
        assert not resolve_project(project.id).trusted
        assert register_project(self.root, trust=True).trusted
        assert register_project(self.root).trusted
        assert rename_project(project.id, "continuum").trusted
        separate = self.base / "other-registry.json"
        assert not register_project(self.root, registry_path=separate).trusted

    def test_rename_preserves_identity_aliases_and_history(self) -> None:
        project = register_project(self.root, "mechanics")
        ledger = Ledger(project.ledger_path)
        ledger.record(Verdict("rejected", "old.target", "old claim"), "-- historical source")
        identity = ledger.identity()
        before = snapshot(self.root)
        renamed = rename_project("mechanics", "continuum")
        assert renamed.id == project.id
        assert renamed.aliases == ("mechanics",)
        assert ledger.identity() == identity
        assert snapshot(self.root)[".nullius/ledger.sqlite3"] == before[".nullius/ledger.sqlite3"]
        for selector in ("mechanics", "continuum", project.id, self.root):
            assert resolve_project(selector) == renamed
        third = rename_project("mechanics", "fields")
        assert third.aliases == ("mechanics", "continuum")
        restored = rename_project("fields", "mechanics")
        assert restored.aliases == ("continuum", "fields")
        before = snapshot(self.base)
        assert rename_project("mechanics", "mechanics") == restored
        assert snapshot(self.base) == before

    def test_register_can_rename_without_replacing_identity(self) -> None:
        original = register_project(self.root, "old")
        renamed = register_project(self.root, "new")
        assert renamed.id == original.id
        assert renamed.aliases == ("old",)
        assert resolve_project("old") == renamed

    def test_move_requires_registration_and_preserves_ids(self) -> None:
        project = register_project(self.root, "mechanics")
        identity = read_ledger_identity(project.ledger_path)
        moved = self.base / "moved"
        self.root.rename(moved)
        before = snapshot(self.base)
        for selector in (project.id, project.name, self.root, moved):
            with pytest.raises(ProjectError):
                resolve_project(selector)
        assert snapshot(self.base) == before
        assert list_projects() == [project]
        relocated = register_project(moved)
        assert relocated.id == project.id
        assert relocated.root == moved
        assert resolve_project("mechanics") == relocated
        assert read_ledger_identity(relocated.ledger_path) == identity

    def test_existing_clone_requires_relocate(self) -> None:
        project = register_project(self.root, "mechanics")
        clone = self.base / "clone"
        shutil.copytree(self.root, clone)
        before = snapshot(self.base)
        with pytest.raises(ProjectError, match="relocate"):
            register_project(clone)
        assert snapshot(self.base) == before
        relocated = register_project(clone, relocate=True)
        assert relocated.id == project.id
        assert resolve_project("mechanics").root == clone
        with pytest.raises(ProjectError):
            resolve_project(self.root)
        with pytest.raises(ProjectError):
            register_project(self.root)

    def test_inaccessible_old_root_is_not_assumed_missing(self) -> None:
        project = register_project(self.root)
        clone = self.base / "clone"
        shutil.copytree(self.root, clone)
        lstat = Path.lstat

        def inaccessible(path: Path) -> os.stat_result:
            if path == self.root:
                raise PermissionError("cannot inspect old root")
            return lstat(path)

        with (
            patch.object(Path, "lstat", inaccessible),
            pytest.raises(ProjectError, match="cannot inspect old root"),
        ):
            register_project(clone)
        assert list_projects() == [project]

    def test_foreign_ledger_is_never_rebound(self) -> None:
        project = register_project(self.root, "mechanics")
        other = register_project(self.lean_project("other"))
        shutil.copyfile(other.ledger_path, project.ledger_path)
        before = snapshot(self.base)
        for operation in (
            lambda: resolve_project(project.id),
            lambda: rename_project(project.id, "new-name"),
            lambda: register_project(self.root),
        ):
            with pytest.raises(ProjectError, match="belongs to project"):
                operation()
        assert snapshot(self.base) == before
        assert ledger_identity(project.ledger_path)["project_id"] == other.id

    def test_bound_ledger_without_config_is_not_adopted(self) -> None:
        ledger_path = self.root / ".nullius" / "ledger.sqlite3"
        ledger = Ledger(ledger_path)
        project_id = str(uuid4())
        ledger.bind_project(project_id)
        with pytest.raises(ProjectError):
            register_project(self.root)
        assert not self.config().exists()
        assert not self.registry.exists()
        assert ledger.identity()["project_id"] == project_id

    def test_label_and_alias_collisions_do_not_change_files(self) -> None:
        project = register_project(self.root, "old")
        renamed = rename_project(project.id, "new")
        other = register_project(self.lean_project("other"))
        before = snapshot(self.base)
        for name in ("old", "new"):
            with pytest.raises(ProjectError, match="already registered"):
                rename_project(other.id, name)
            with pytest.raises(ProjectError, match="already registered"):
                register_project(other.root, name)
        assert snapshot(self.base) == before
        assert resolve_project("old") == renamed

    def test_missing_and_invalid_roots(self) -> None:
        for root in (self.base / "missing", self.root / "lean-toolchain"):
            with pytest.raises(ProjectError):
                register_project(root)
        (self.root / "lean-toolchain").unlink()
        with pytest.raises(ProjectError, match="lean-toolchain"):
            register_project(self.root)
        (self.root / "lean-toolchain").write_text("")
        (self.root / "lakefile.toml").unlink()
        with pytest.raises(ProjectError, match="lakefile"):
            register_project(self.root)
        assert not self.registry.exists()
        assert not (self.root / ".nullius").exists()

    def test_plain_path_is_not_implicit_registration(self) -> None:
        before = snapshot(self.base)
        with pytest.raises(ProjectError, match="not registered"):
            resolve_project(self.root)
        assert list_projects() == []
        assert snapshot(self.base) == before
        assert not self.registry.parent.exists()
        assert not (self.root / ".nullius").exists()

    def test_readonly_resolve_never_recreates_or_migrates_ledger(self) -> None:
        project = register_project(self.root)
        before = snapshot(self.base)
        with patch("nullius.projects.Ledger", side_effect=AssertionError("no writable ledger")):
            assert resolve_project(project.id) == project
            assert list_projects() == [project]
        assert snapshot(self.base) == before
        project.ledger_path.unlink()
        before = snapshot(self.base)
        assert resolve_project(project.id) == project
        assert snapshot(self.base) == before
        with closing(sqlite3.connect(project.ledger_path)) as connection:
            connection.executescript(SCHEMA)
        before = snapshot(self.base)
        with pytest.raises(ProjectError, match="unbound"):
            resolve_project(project.id)
        assert snapshot(self.base) == before
        register_project(self.root)
        assert ledger_identity(project.ledger_path)["project_id"] == project.id

    def test_disk_identity_and_name_mismatches(self) -> None:
        project = register_project(self.root)
        original = json.loads(self.config().read_text())
        for changed in (
            dict(original, id=str(uuid4())),
            dict(original, name="manually-edited"),
        ):
            self.config().write_text(json.dumps(changed))
            before = snapshot(self.base)
            with pytest.raises(ProjectError, match="does not match"):
                resolve_project(project.id)
            assert snapshot(self.base) == before
        self.config().unlink()
        with pytest.raises(ProjectError, match="missing"):
            resolve_project(project.id)
        with pytest.raises(ProjectError, match="missing"):
            register_project(self.root)
        assert not self.config().exists()

    def test_bad_project_files_and_names(self) -> None:
        (self.root / ".nullius").mkdir()
        bad_values = (
            "not json",
            "[]",
            '{"schema_version":2,"id":"bad","name":"x"}',
            '{"schema_version":true,"id":"bad","name":"x"}',
            json.dumps({"schema_version": 1, "id": "bad", "name": "x"}),
            json.dumps({"schema_version": 1, "id": str(uuid4()), "name": ""}),
        )
        for contents in bad_values:
            self.config().write_text(contents)
            with pytest.raises(ProjectError):
                register_project(self.root)
            assert self.config().read_text() == contents
            assert not self.registry.exists()
        self.config().unlink()
        for name in ("", " ", ".", "..", "~", "a/b", "a\\b", "a\nb", str(uuid4())):
            with pytest.raises(ProjectError):
                register_project(self.root, name)
        invalid_flags: list[dict[str, Any]] = [{"trust": "yes"}, {"relocate": 1}]
        for keyword in invalid_flags:
            with pytest.raises(ProjectError):
                register_project(self.root, **keyword)

    def test_malformed_registry_is_not_overwritten(self) -> None:
        self.registry.parent.mkdir()
        for data in ("[]", "bad json", '{"schema_version":1,"projects":[]}'):
            self.registry.write_text(data)
            for operation in (lambda: register_project(self.root), list_projects):
                with pytest.raises(ProjectError):
                    operation()
            assert self.registry.read_text() == data
            assert not self.config().exists()

    def test_invalid_registry_entries_are_explicit_errors(self) -> None:
        project = register_project(self.root)
        original = json.loads(self.registry.read_text())
        entry = original["projects"][project.id]
        invalid_entries = [
            {project.id: dict(entry, root="relative/path")},
            {project.id: dict(entry, trusted="true")},
            {project.id: dict(entry, aliases="old")},
            {project.id: dict(entry, aliases=["old", "old"])},
            {project.id: dict(entry, aliases=[project.name])},
            {"bad-uuid": entry},
            {project.id: entry, str(uuid4()): dict(entry, name="other")},
            {project.id: entry, str(uuid4()): dict(entry, root=str(self.base / "elsewhere"))},
        ]
        for entries in invalid_entries:
            self.registry.write_text(json.dumps(dict(original, projects=entries)))
            before = snapshot(self.base)
            for operation in (
                list_projects,
                lambda: resolve_project(project.id),
                lambda: register_project(self.root),
            ):
                with pytest.raises(ProjectError):
                    operation()
            assert snapshot(self.base) == before

    def test_malformed_ledger_and_metadata_paths(self) -> None:
        metadata = self.root / ".nullius"
        metadata.write_text("not a directory")
        with pytest.raises(ProjectError):
            register_project(self.root)
        metadata.unlink()
        metadata.mkdir()
        self.config().mkdir()
        with pytest.raises(ProjectError):
            register_project(self.root)
        self.config().rmdir()
        ledger = metadata / "ledger.sqlite3"
        ledger.write_text("not sqlite")
        with pytest.raises(ProjectError, match="ledger"):
            register_project(self.root)
        assert not self.config().exists()
        assert ledger.read_text() == "not sqlite"

    def test_registry_cannot_overwrite_project_metadata(self) -> None:
        for filename in ("project.json", "ledger.sqlite3", "local-registry.json"):
            with pytest.raises(ProjectError, match="outside"):
                register_project(self.root, registry_path=self.root / ".nullius" / filename)
        assert not (self.root / ".nullius").exists()

    def test_symlink_root_is_canonical_but_metadata_links_are_rejected(self) -> None:
        alias = self.base / "root-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        project = register_project(alias)
        assert project.root == self.root
        assert resolve_project(alias) == project
        other = self.lean_project("other")
        (other / ".nullius").symlink_to(self.root / ".nullius", target_is_directory=True)
        before = snapshot(self.root)
        with pytest.raises(ProjectError, match="local directory"):
            register_project(other, "other")
        assert snapshot(self.root) == before

    def test_registry_defaults_and_explicit_override(self) -> None:
        with patch.dict(
            os.environ, {"NULLIUS_PROJECT_REGISTRY": "", "XDG_CONFIG_HOME": str(self.base)}
        ):
            project = register_project(self.root)
            assert (self.base / "nullius" / "projects.json").is_file()
            assert resolve_project(project.id) == project
        assert not self.registry.exists()
        explicit = self.base / "explicit.json"
        same = register_project(self.root, registry_path=explicit)
        assert resolve_project(project.id, registry_path=explicit) == same
        assert not self.registry.exists()
        with (
            patch.dict(os.environ, {"NULLIUS_PROJECT_REGISTRY": "", "XDG_CONFIG_HOME": ""}),
            patch("nullius.projects.Path.home", return_value=self.base / "home"),
        ):
            assert list_projects() == []
            register_project(self.root)
            assert (self.base / "home" / ".config" / "nullius" / "projects.json").is_file()

    def test_concurrent_registration_keeps_all_projects_and_one_id(self) -> None:
        barrier = Barrier(4)

        def register(_: int) -> str:
            barrier.wait(timeout=10)
            return register_project(self.root).id

        with ThreadPoolExecutor(max_workers=4) as pool:
            assert len(set(pool.map(register, range(4)))) == 1
        roots = [self.lean_project(f"parallel-{i}") for i in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            projects = list(pool.map(register_project, roots))
        assert len(list_projects()) == 5
        for project in projects:
            assert resolve_project(project.id) == project

    def test_two_registries_share_one_project_identity(self) -> None:
        registries = [self.base / f"registry-{i}.json" for i in range(4)]
        barrier = Barrier(len(registries))

        def register(registry: Path) -> str:
            barrier.wait(timeout=10)
            return register_project(self.root, registry_path=registry).id

        with ThreadPoolExecutor(max_workers=len(registries)) as pool:
            identities = list(pool.map(register, registries))
        assert len(set(identities)) == 1
        for registry in registries:
            assert resolve_project(self.root, registry_path=registry).id == identities[0]

    def test_failed_new_registration_retains_one_uuid_for_retry(self) -> None:
        with (
            patch("nullius.projects.Ledger.bind_project", side_effect=ValueError("binding failed")),
            pytest.raises(ProjectError, match="binding failed"),
        ):
            register_project(self.root)
        project_id = json.loads(self.config().read_text())["id"]
        assert not self.registry.exists()
        with pytest.raises(ProjectError, match="not registered"):
            resolve_project(self.root)
        assert register_project(self.root).id == project_id

    def test_failed_registry_write_is_detected_and_explicitly_recoverable(self) -> None:
        project = register_project(self.root, "old")
        original_registry = self.registry.read_bytes()
        replace_file = os.replace

        def fail_registry(source: Path, destination: Path) -> None:
            if destination == self.registry:
                raise OSError("simulated registry write failure")
            replace_file(source, destination)

        with (
            patch("nullius.projects.os.replace", side_effect=fail_registry),
            pytest.raises(ProjectError, match="simulated registry write failure"),
        ):
            rename_project(project.id, "new")
        assert self.registry.read_bytes() == original_registry
        assert json.loads(self.config().read_text())["name"] == "new"
        with pytest.raises(ProjectError, match="does not match"):
            resolve_project(project.id)
        recovered = register_project(self.root)
        assert recovered.id == project.id
        assert recovered.name == "new"
        assert recovered.aliases == ("old",)
        assert resolve_project("old") == recovered

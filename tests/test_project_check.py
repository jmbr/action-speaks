"""Project targets retain their own structures and undergo full kernel replay."""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.lean

from nullius import project_search as PS  # noqa: E402
from nullius import search as S  # noqa: E402
from nullius.config import Config  # noqa: E402
from nullius.ledger import Ledger  # noqa: E402
from nullius.project_check import environment_id, verify_project  # noqa: E402
from nullius.repl import Session  # noqa: E402
from nullius.verify import Verifier  # noqa: E402

PROJECT_ID = "67357f2f-4635-4726-948b-18ee6c8c1341"
MODEL = """namespace Demo
structure Box where
  value : Nat
end Demo
"""
GOOD = """import Demo.Model
namespace Demo
theorem value_eq (b : Box) : b.value = b.value := rfl
theorem vacuous (h : False) : True := True.intro
theorem unused (n : Nat) (h : 0 < n) : n = n := rfl
theorem incomplete : False := by sorry
end Demo
"""
FORGED = """import Lean
open Lean Elab Command in
run_cmd do
  let env <- getEnv
  let decl := Declaration.thmDecl {
    name := `Forged.bad, levelParams := [], type := mkConst ``False,
    value := mkConst ``True.intro }
  match env.addDeclCore 0 decl none false with
  | .ok env' => modifyEnv fun _ => env'
  | .error _ => throwError "could not install test declaration"
"""


def write_project(root: Path, config: Config) -> Config:
    (root / "lean-toolchain").write_text(config.toolchain() + "\n")
    (root / "lakefile.toml").write_text(
        'name = "projectFixture"\nversion = "0.1.0"\n'
        '[[lean_lib]]\nname = "Demo"\n[[lean_lib]]\nname = "Forged"\n'
    )
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "name": "projectFixture",
                "lakeDir": ".lake",
                "packagesDir": ".lake/packages",
                "packages": [],
            }
        )
    )
    (root / "Demo.lean").write_text(GOOD)
    (root / "Demo").mkdir()
    (root / "Demo/Model.lean").write_text(MODEL)
    (root / "Forged.lean").write_text(FORGED)
    return replace(
        config,
        project_root=root,
        project_id=PROJECT_ID,
        project_name="fixture",
        project_trusted=True,
        ledger_path=root / ".nullius/ledger.sqlite3",
        ledger_index_dir=root / ".nullius/indexes",
        ledger_refresh_timeout=300,
    )


@pytest.fixture(scope="module")
def base() -> Config:
    return Config.discover()


@pytest.fixture
def unbuilt(base: Config) -> Iterator[Config]:
    """A project whose sources are present but never compiled."""
    with tempfile.TemporaryDirectory(prefix="nullius-project-check-") as directory:
        yield write_project(Path(directory), base)


@pytest.fixture(scope="module")
def built(base: Config) -> Iterator[Config]:
    """Built once: `lake build` dominates the cost of this module."""
    with tempfile.TemporaryDirectory(prefix="nullius-project-check-") as directory:
        cfg = write_project(Path(directory), base)
        verdict = verify_project(cfg, "Demo", "Demo.value_eq", build=True)
        assert verdict.verified, verdict.render()
        yield cfg
        S.close_sessions(cfg)


def test_untrusted_project_is_rejected_before_anything_runs(unbuilt: Config) -> None:
    with patch("nullius.project_check._run", side_effect=AssertionError("unapproved execution")):
        with pytest.raises(ValueError):
            verify_project(replace(unbuilt, project_trusted=False), "Demo", "Demo.value_eq")


def test_unbuilt_project_asks_for_a_build_rather_than_building(unbuilt: Config) -> None:
    stale = verify_project(unbuilt, "Demo", "Demo.value_eq")
    assert not stale.verified
    assert "--build" in stale.render()


def test_built_target_verifies_with_its_own_structures(built: Config) -> None:
    verdict = verify_project(built, "Demo", "Demo.value_eq", build=True)
    assert verdict.verified, verdict.render()
    assert "Demo.Box" in verdict.statement
    assert verdict.provenance["environment_id"]


@pytest.mark.parametrize(
    ("target", "failure", "strict"),
    [
        ("Demo.vacuous", "not_vacuous", False),
        ("Demo.unused", "hypotheses_used", True),
        ("Demo.incomplete", "trusted_axioms", False),
    ],
)
def test_faulty_target_is_caught_by_its_check(
    built: Config, target: str, failure: str, strict: bool
) -> None:
    verdict = verify_project(built, "Demo", target, require_nontrivial=strict)
    assert not verdict.verified, verdict.render()
    assert failure in [check.name for check in verdict.failures], verdict.render()


def test_forged_declaration_is_caught_by_kernel_replay(built: Config) -> None:
    forged = verify_project(built, "Forged", "Forged.bad", build=True)
    assert not forged.verified, forged.render()
    assert "kernel_replay" in [check.name for check in forged.failures], forged.render()


def test_environment_identity_survives_a_rename(built: Config) -> None:
    assert environment_id(built) == environment_id(replace(built, project_name="renamed"))


def test_project_search_reads_the_ledger_without_building_or_checking(
    base: Config, built: Config
) -> None:
    """Search must answer from the cache alone once refreshed, and notice a changed source."""
    if not base.loogle_bin or not base.loogle_bin.exists():
        pytest.skip("no local loogle built (scripts/build-loogle.sh)")
    good = verify_project(built, "Demo", "Demo.value_eq", build=True)
    ledger = Ledger(built.ledger_path)
    ledger.bind_project(PROJECT_ID)
    row = ledger.record(
        good,
        "",
        record_kind="project",
        project_id=PROJECT_ID,
        module="Demo",
        environment_id=good.provenance["environment_id"],
    )
    count = ledger.stats()["total"]

    assert PS.search_project_ledger("Demo.Box", 8, 20, config=built).error
    refreshed = PS.search_project_ledger("Demo.Box", 8, 20, config=built, refresh=True)
    assert refreshed.hits and not refreshed.error, refreshed.render()
    assert refreshed.hits[0].name == "Demo.value_eq"
    assert refreshed.hits[0].ledger["ids"] == [row]

    with (
        patch("nullius.project_search._run", side_effect=AssertionError("implicit build")),
        patch(
            "nullius.project_search.verify_project",
            side_effect=AssertionError("implicit check"),
        ),
    ):
        assert PS.search_project_ledger("Demo.Box", 8, 20, config=built).hits
    assert ledger.stats()["total"] == count

    source = "theorem saved : True := True.intro"
    with Session(base) as session:
        snippet = Verifier(session).verify(source)
    assert snippet.verified
    ledger.record(snippet, source, project_id=PROJECT_ID)
    mixed = S.search("Demo.Box", backend="ledger", refresh_ledger=True, config=built)
    assert all(result.error is None for result in mixed), [r.render() for r in mixed]
    snippet_result = next(r for r in mixed if r.backend == "loogle-ledger")
    assert snippet_result.index["query_applicable"] is False
    assert "query not applicable" in snippet_result.render()
    assert any(result.hits for result in mixed if result.backend == "project-ledger")

    (built.project_root / "Demo.lean").write_text(GOOD + "\n-- changed source\n")
    assert PS.search_project_ledger("Demo.Box", 8, 20, config=built).error
    assert not list(built.project_root.glob(".nullius/snapshots"))

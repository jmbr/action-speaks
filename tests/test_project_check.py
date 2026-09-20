"""Project targets retain their own structures and undergo full kernel replay."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def fixture(root: Path, config: Config) -> Config:
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


def main() -> int:
    base = Config.discover()
    with tempfile.TemporaryDirectory(prefix="nullius-project-check-") as directory:
        cfg = fixture(Path(directory), base)
        with patch(
            "nullius.project_check._run", side_effect=AssertionError("unapproved execution")
        ):
            try:
                verify_project(replace(cfg, project_trusted=False), "Demo", "Demo.value_eq")
            except ValueError:
                pass
            else:
                raise AssertionError("untrusted project accepted")
        stale = verify_project(cfg, "Demo", "Demo.value_eq")
        assert not stale.verified and "--build" in stale.render()
        good = verify_project(cfg, "Demo", "Demo.value_eq", build=True)
        assert good.verified, good.render()
        assert "Demo.Box" in good.statement
        assert good.provenance["environment_id"]
        for target, failure, strict in (
            ("Demo.vacuous", "not_vacuous", False),
            ("Demo.unused", "hypotheses_used", True),
            ("Demo.incomplete", "trusted_axioms", False),
        ):
            verdict = verify_project(cfg, "Demo", target, require_nontrivial=strict)
            assert not verdict.verified, verdict.render()
            assert failure in [c.name for c in verdict.failures], verdict.render()
        forged = verify_project(cfg, "Forged", "Forged.bad", build=True)
        assert not forged.verified, forged.render()
        assert "kernel_replay" in [c.name for c in forged.failures], forged.render()
        state = environment_id(cfg)
        assert state == environment_id(replace(cfg, project_name="renamed"))
        ledger = Ledger(cfg.ledger_path)
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
        if base.loogle_bin and base.loogle_bin.exists():
            initial = PS.search_project_ledger("Demo.Box", 8, 20, config=cfg)
            assert initial.error
            built = PS.search_project_ledger("Demo.Box", 8, 20, config=cfg, refresh=True)
            assert built.hits and not built.error, built.render()
            assert built.hits[0].name == "Demo.value_eq"
            assert built.hits[0].ledger["ids"] == [row]
            with (
                patch("nullius.project_search._run", side_effect=AssertionError("implicit build")),
                patch(
                    "nullius.project_search.verify_project",
                    side_effect=AssertionError("implicit check"),
                ),
            ):
                assert PS.search_project_ledger("Demo.Box", 8, 20, config=cfg).hits
            assert ledger.stats()["total"] == count
            source = "theorem saved : True := True.intro"
            with Session(base) as session:
                snippet = Verifier(session).verify(source)
            assert snippet.verified
            ledger.record(snippet, source, project_id=PROJECT_ID)
            mixed = S.search("Demo.Box", backend="ledger", refresh_ledger=True, config=cfg)
            assert all(r.error is None for r in mixed), [r.render() for r in mixed]
            snippet_result = next(r for r in mixed if r.backend == "loogle-ledger")
            assert snippet_result.index["query_applicable"] is False
            assert "query not applicable" in snippet_result.render()
            assert any(r.hits for r in mixed if r.backend == "project-ledger")
            (cfg.project_root / "Demo.lean").write_text(GOOD + "\n-- changed source\n")
            assert PS.search_project_ledger("Demo.Box", 8, 20, config=cfg).error
        S.close_sessions(cfg)
        assert not list(cfg.project_root.glob(".nullius/snapshots"))
    print("Project checks, declaration-group replay, and current-project search: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

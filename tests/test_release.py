"""Release configuration and workflow stay aligned."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent


def config() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["semantic_release"]


def workflow() -> str:
    return (ROOT / ".gitea/workflows/release.yml").read_text()


def hooks() -> str:
    return (ROOT / ".pre-commit-config.yaml").read_text()


def test_semantic_release_is_the_only_version_engine() -> None:
    semantic = config()
    commitizen = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["commitizen"]
    assert semantic["commit_parser"] == "conventional"
    assert semantic["version_variables"] == ["action_speaks/__init__.py:__version__"]
    assert "version_files" not in commitizen


def test_below_one_point_zero_a_breaking_change_stays_minor() -> None:
    semantic = config()
    commitizen = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["commitizen"]
    assert semantic["allow_zero_version"] is True
    assert semantic["major_on_zero"] is False
    assert commitizen["major_version_zero"] is True


def test_hooks_are_installed_at_every_stage_they_declare() -> None:
    """A hook declared for a stage `prek install` does not wire up silently never runs."""
    text = hooks()
    declared = text.split("default_install_hook_types:")[1].split("\n")[0]
    for stage in ("pre-commit", "commit-msg", "pre-push"):
        assert stage in declared
    assert "id: commitizen\n        stages: [commit-msg]" in text
    assert "stages: [pre-push]" in text


def test_each_hook_runs_at_one_stage() -> None:
    """Unstaged hooks run at every installed stage, repeating the commit checks twice more."""
    text = hooks()
    assert "\ndefault_stages: [pre-commit]\n" in text
    for hook in ("end-of-file-fixer", "trailing-whitespace", "check-added-large-files"):
        assert f"id: {hook}\n        stages: [pre-commit]" in text


def test_push_runs_the_checks_the_release_workflow_runs() -> None:
    noxfile = (ROOT / "noxfile.py").read_text()
    defaults = noxfile.split("nox.options.sessions = [")[1].split("]")[0]
    released = workflow().split(".venv/bin/nox -s ")[1].split("\n")[0].split()
    for session in released:
        assert f'"{session}"' in defaults
    assert "exec .venv/bin/nox; fi; exec nox'" in hooks()


def test_release_targets_this_http_gitea() -> None:
    remote = config()["remote"]
    assert remote == {
        "type": "gitea",
        "domain": "http://localhost:3000",
        "token": {"env": "GITEA_TOKEN"},
        "insecure": True,
        "ignore_token_for_push": True,
    }


def test_release_produces_no_package_or_mirror_release() -> None:
    semantic = config()
    assert semantic["build_command"] == ""
    assert semantic["upload_to_vcs_release"] is False
    assert "github" not in str(semantic).lower()


def test_generated_release_commit_is_conventional_and_nonreleasing() -> None:
    semantic = config()
    assert semantic["commit_message"] == "chore(release): v{version}"
    excluded = semantic["changelog"]["exclude_commit_patterns"]
    assert any(pattern.startswith("^chore") for pattern in excluded)


def test_workflow_fetches_all_tags_and_runs_the_full_suite() -> None:
    text = workflow()
    assert "fetch-depth: 0" in text
    assert "lake exe cache get\n          lake build\n" in text
    for target in ("repl", "Physlib", "Cslib", "Statlib", "ActionSpeaksAll"):
        assert f"lake build {target}" in text
    assert "lake build repl Physlib" not in text
    assert "floatlib" not in text.lower()
    assert ".venv/bin/nox -s lint format types tests" in text
    assert ".venv/bin/semantic-release version" in text


def test_workflow_caches_the_expensive_lean_build() -> None:
    text = workflow()
    assert "uses: actions/cache/restore@v4" in text
    assert "uses: actions/cache/save@v4" in text
    assert text.index("Save Lean build") < text.index("- name: Test")
    for path in (
        "lean/.lake/build",
        "lean/.lake/packages/Physlib",
        "lean/.lake/packages/cslib",
        "lean/.lake/packages/Statlib",
        "lean/.lake/packages/repl",
        "vendor/loogle",
    ):
        assert path in text
    assert "~/.elan" not in text
    assert "lean/.lake/packages/mathlib" not in text
    for input_file in ("lean/lean-toolchain", "lean/lake-manifest.json"):
        assert input_file in text


def test_type_session_targets_the_python_package() -> None:
    text = (ROOT / "noxfile.py").read_text()
    assert 'session.run("basedpyright", "action_speaks")' in text


def test_skill_security_findings_fail_the_checks() -> None:
    """SkillSpector exits 0 on findings unless told otherwise, which would make it advisory."""
    text = (ROOT / "noxfile.py").read_text()
    assert '"skillspector", "scan", str(directory), "--no-llm", "--fail-on-findings"' in text
    assert '"skills"' in text.split("nox.options.sessions = [")[1].split("]")[0]


def test_nox_sessions_install_tools_only_from_dependency_groups() -> None:
    """Tool requirements live in pyproject.toml, not as literals in noxfile.py."""
    noxfile = (ROOT / "noxfile.py").read_text()
    groups = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]
    used = set(re.findall(r'install_group\(session, "([^"]+)"\)', noxfile))
    assert used and used <= set(groups)
    installs = re.findall(r'session\.install\(("[^)]*)\)', noxfile)
    assert set(installs) <= {'"-e", "."', '"."'}


def test_workflow_uses_the_short_lived_forgejo_token() -> None:
    text = workflow()
    assert "GITEA_API_URL: ${{ forgejo.api_url }}" in text
    assert "GITEA_TOKEN: ${{ forgejo.token }}" in text
    assert "secrets." not in text


def test_workflow_does_not_release_its_own_commit_again() -> None:
    text = workflow()
    assert '"chore(release): "*' in text
    assert "steps.guard.outputs.run == 'true'" in text


def test_conventional_commit_baseline_exists() -> None:
    tags = subprocess.run(
        ["git", "tag", "--list", "v0.1.0"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert tags == ["v0.1.0"]

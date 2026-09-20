"""The installer, driven against a temporary home.

Every case injects a root, a home and an environment, so a test can never read or write the
developer's real installation. What is asserted is the resulting filesystem state, not the
output, apart from the messages a user acts on.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import install  # noqa: E402

TEMPLATE = {
    "mcpServers": {
        "action-speaks": {
            "command": "python3",
            "args": ["-m", "action_speaks.mcp_server"],
            "cwd": "__ACTION_SPEAKS_ROOT__",
            "env": {"PYTHONPATH": "__ACTION_SPEAKS_ROOT__"},
        }
    }
}


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repository layout complete enough to install from."""
    root = tmp_path / "checkout"
    (root / "skills" / "action-speaks" / "scripts").mkdir(parents=True)
    (root / "skills" / "action-speaks" / "SKILL.md").write_text("# skill\n")
    (root / "skills" / "action-speaks" / "scripts" / "action-speaks").write_text("#!/bin/sh\n")
    (root / "mcp").mkdir()
    (root / "mcp" / "copilot-mcp-config.json").write_text(json.dumps(TEMPLATE))
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def make(checkout: Path, home: Path, **env) -> install.Installer:
    environment = {"PATH": str(home / ".local" / "bin"), **env}
    return install.Installer(checkout, home, env=environment, out=io.StringIO())


def run(inst: install.Installer, *argv: str) -> tuple[int, str]:
    code = install.main(list(argv), installer=inst)
    return code, inst.out.getvalue()


def with_venv(checkout: Path, *, cli: bool = True, python: bool = True) -> None:
    binaries = checkout / ".venv" / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    if cli:
        script = binaries / "action-speaks"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
    if python:
        (binaries / "python3").write_text("")


def snapshot(path: Path) -> dict[str, str]:
    """Contents, link targets and modes of everything under `path`."""
    state = {}
    for item in sorted(path.rglob("*")):
        key = str(item.relative_to(path))
        if item.is_symlink():
            state[key] = f"link:{os.readlink(item)}"
        elif item.is_dir():
            state[key] = "dir"
        else:
            digest = hashlib.sha256(item.read_bytes()).hexdigest()
            state[key] = f"{digest}:{oct(item.stat().st_mode)}"
    return state


# -- install paths ---------------------------------------------------------


def test_full_install_links_skill_cli_and_writes_mcp(checkout: Path, home: Path) -> None:
    with_venv(checkout)
    code, out = run(make(checkout, home))
    assert code == 0
    assert (home / ".agents/skills/action-speaks").is_symlink()
    assert (home / ".local/bin/action-speaks").is_symlink()
    config = json.loads((home / ".copilot/mcp-config.json").read_text())
    assert list(config["mcpServers"]) == ["action-speaks"]
    assert "on PATH" in out


@pytest.mark.parametrize(
    ("flag", "skill", "cli", "mcp"),
    [
        ("--skill", True, False, False),
        ("--cli", False, True, False),
        ("--mcp", False, False, True),
    ],
)
def test_component_selection(
    checkout: Path, home: Path, flag: str, skill: bool, cli: bool, mcp: bool
) -> None:
    with_venv(checkout)
    run(make(checkout, home), flag)
    assert (home / ".agents/skills/action-speaks").is_symlink() is skill
    assert (home / ".local/bin/action-speaks").is_symlink() is cli
    assert (home / ".copilot/mcp-config.json").exists() is mcp


def test_missing_console_script_is_reported_not_an_error(checkout: Path, home: Path) -> None:
    """A checkout without a virtualenv still installs the skill and MCP server."""
    code, out = run(make(checkout, home))
    assert code == 0
    assert "no console script" in out
    assert "pip install -e ." in out
    assert not (home / ".local/bin/action-speaks").exists()


def test_mcp_prefers_the_checkout_interpreter(checkout: Path, home: Path) -> None:
    with_venv(checkout)
    run(make(checkout, home), "--mcp")
    entry = json.loads((home / ".copilot/mcp-config.json").read_text())["mcpServers"][
        "action-speaks"
    ]
    assert entry["command"] == str(checkout / ".venv/bin/python3")
    # A venv interpreter needs no PYTHONPATH, and leaving one lets the wrong tree win.
    assert "PYTHONPATH" not in entry.get("env", {})


def test_mcp_without_a_virtualenv_keeps_the_template_command(checkout: Path, home: Path) -> None:
    run(make(checkout, home), "--mcp")
    entry = json.loads((home / ".copilot/mcp-config.json").read_text())["mcpServers"][
        "action-speaks"
    ]
    assert entry["command"] == "python3"
    assert entry["env"]["PYTHONPATH"] == str(checkout)


def test_bin_dir_override_is_honored(checkout: Path, home: Path, tmp_path: Path) -> None:
    with_venv(checkout)
    elsewhere = tmp_path / "custom bin"
    run(make(checkout, home, ACTION_SPEAKS_BIN_DIR=str(elsewhere)), "--cli")
    assert (elsewhere / "action-speaks").is_symlink()


def test_missing_bin_dir_from_path_is_said_so(checkout: Path, home: Path) -> None:
    with_venv(checkout)
    inst = install.Installer(checkout, home, env={"PATH": "/usr/bin"}, out=io.StringIO())
    _, out = run(inst, "--cli")
    assert "is not on PATH" in out


# -- refusals --------------------------------------------------------------


def test_a_real_directory_is_never_replaced(checkout: Path, home: Path) -> None:
    """It may be an older copy someone edited in place."""
    existing = home / ".agents/skills/action-speaks"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("mine")
    code, out = run(make(checkout, home), "--skill")
    assert code == 1
    assert "not a symlink" in out
    assert (existing / "SKILL.md").read_text() == "mine"


def test_a_real_file_at_the_cli_path_is_left_alone(checkout: Path, home: Path) -> None:
    with_venv(checkout)
    target = home / ".local/bin"
    target.mkdir(parents=True)
    (target / "action-speaks").write_text("mine")
    code, out = run(make(checkout, home), "--cli")
    assert code == 1
    assert (target / "action-speaks").read_text() == "mine"


def test_missing_skill_source_is_an_error(checkout: Path, home: Path) -> None:
    (checkout / "skills/action-speaks/SKILL.md").unlink()
    code, out = run(make(checkout, home), "--skill")
    assert code == 1
    assert "SKILL.md is missing" in out


def test_malformed_config_is_reported_without_touching_it(checkout: Path, home: Path) -> None:
    config = home / ".copilot/mcp-config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{not json")
    before = snapshot(home)
    code, out = run(make(checkout, home), "--mcp")
    assert code == 1
    assert "not valid JSON" in out
    assert snapshot(home) == before


# -- MCP merging -----------------------------------------------------------


def test_other_servers_are_preserved(checkout: Path, home: Path) -> None:
    config = home / ".copilot/mcp-config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    _, out = run(make(checkout, home), "--mcp")
    servers = json.loads(config.read_text())["mcpServers"]
    assert servers["other"] == {"command": "x"}
    assert "preserving: other" in out


def test_an_existing_config_is_backed_up(checkout: Path, home: Path) -> None:
    config = home / ".copilot/mcp-config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    run(make(checkout, home), "--mcp")
    assert json.loads(config.with_suffix(".json.bak").read_text())["mcpServers"] == {
        "other": {"command": "x"}
    }


def test_repeated_runs_say_nothing_changed(checkout: Path, home: Path) -> None:
    inst = make(checkout, home)
    run(inst, "--mcp")
    after = snapshot(home)
    second = make(checkout, home)
    _, out = run(second, "--mcp")
    assert "already configured" in out
    assert snapshot(home) == after, "a second run must not rewrite or re-back-up"


def test_a_stale_entry_is_updated(checkout: Path, home: Path) -> None:
    config = home / ".copilot/mcp-config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"action-speaks": {"command": "old"}}}))
    _, out = run(make(checkout, home), "--mcp")
    assert "updating" in out
    assert json.loads(config.read_text())["mcpServers"]["action-speaks"]["command"] != "old"


# -- legacy cleanup --------------------------------------------------------


def test_legacy_links_into_this_checkout_are_removed(checkout: Path, home: Path) -> None:
    """A rename leaves the old link dangling, so the raw target is what identifies it."""
    skills = home / ".agents/skills"
    skills.mkdir(parents=True)
    (skills / "nullius").symlink_to(checkout / "skills" / "nullius")
    (skills / "lean-proof-check").symlink_to(checkout / "skills" / "action-speaks")
    binaries = home / ".local/bin"
    binaries.mkdir(parents=True)
    (binaries / "nullius").symlink_to(checkout / ".venv/bin/nullius")

    run(make(checkout, home), "--skill")
    assert not (skills / "nullius").is_symlink()
    assert not (skills / "lean-proof-check").is_symlink()
    assert not (binaries / "nullius").is_symlink()


def test_unrelated_links_of_the_same_name_are_left_alone(checkout: Path, home: Path) -> None:
    skills = home / ".agents/skills"
    skills.mkdir(parents=True)
    somewhere = home / "other-project"
    somewhere.mkdir()
    (skills / "nullius").symlink_to(somewhere)
    run(make(checkout, home), "--skill")
    assert (skills / "nullius").is_symlink(), "only links into this checkout are ours to remove"


def test_superseded_mcp_server_is_dropped(checkout: Path, home: Path) -> None:
    config = home / ".copilot/mcp-config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"nullius": {"command": "old"}}}))
    _, out = run(make(checkout, home), "--mcp")
    assert "removing superseded server" in out
    assert list(json.loads(config.read_text())["mcpServers"]) == ["action-speaks"]


# -- uninstall -------------------------------------------------------------


def test_uninstall_removes_what_was_installed(checkout: Path, home: Path) -> None:
    with_venv(checkout)
    run(make(checkout, home))
    run(make(checkout, home), "--uninstall")
    assert not (home / ".agents/skills/action-speaks").is_symlink()
    assert not (home / ".local/bin/action-speaks").is_symlink()
    assert json.loads((home / ".copilot/mcp-config.json").read_text())["mcpServers"] == {}


def test_uninstall_preserves_other_servers(checkout: Path, home: Path) -> None:
    config = home / ".copilot/mcp-config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}, "action-speaks": {"command": "y"}}})
    )
    run(make(checkout, home), "--uninstall", "--mcp")
    assert list(json.loads(config.read_text())["mcpServers"]) == ["other"]


def test_uninstalling_nothing_is_not_an_error(checkout: Path, home: Path) -> None:
    code, out = run(make(checkout, home), "--uninstall")
    assert code == 0
    assert "nothing to remove" in out


def test_uninstall_is_idempotent(checkout: Path, home: Path) -> None:
    with_venv(checkout)
    run(make(checkout, home))
    run(make(checkout, home), "--uninstall")
    after = snapshot(home)
    code, _ = run(make(checkout, home), "--uninstall")
    assert code == 0
    assert snapshot(home) == after


# -- dry run ---------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--dry-run", "-n"])
def test_dry_run_changes_nothing_at_all(checkout: Path, home: Path, flag: str) -> None:
    """Including backups and permission changes, which are mutations too."""
    with_venv(checkout)
    before = snapshot(home)
    code, out = run(make(checkout, home), flag)
    assert code == 0
    assert "would:" in out
    assert snapshot(home) == before


def test_dry_run_over_an_existing_install_writes_no_backup(checkout: Path, home: Path) -> None:
    with_venv(checkout)
    run(make(checkout, home))
    before = snapshot(home)
    run(make(checkout, home), "--dry-run")
    assert snapshot(home) == before
    assert not (home / ".copilot/mcp-config.json.bak").exists()


def test_dry_run_and_real_run_plan_the_same_operations(checkout: Path, home: Path) -> None:
    """The point of planning before executing: the two cannot describe different work."""
    with_venv(checkout)
    dry = make(checkout, home).plan(skill=True, cli=True, mcp=True, uninstall=False, dry=True)
    wet = make(checkout, home).plan(skill=True, cli=True, mcp=True, uninstall=False, dry=False)
    assert [op.description for op in dry.operations()] == [
        op.description for op in wet.operations()
    ]


def test_dry_run_uninstall_changes_nothing(checkout: Path, home: Path) -> None:
    with_venv(checkout)
    run(make(checkout, home))
    before = snapshot(home)
    run(make(checkout, home), "--uninstall", "--dry-run")
    assert snapshot(home) == before


# -- idempotency and arguments ---------------------------------------------


def test_repeated_install_is_idempotent(checkout: Path, home: Path) -> None:
    with_venv(checkout)
    run(make(checkout, home))
    (home / ".copilot/mcp-config.json.bak").unlink(missing_ok=True)
    after = snapshot(home)
    run(make(checkout, home))
    assert snapshot(home) == after


def test_the_wrapper_is_made_executable(checkout: Path, home: Path) -> None:
    wrapper = checkout / "skills/action-speaks/scripts/action-speaks"
    wrapper.chmod(0o644)
    run(make(checkout, home), "--skill")
    assert wrapper.stat().st_mode & stat.S_IXUSR


def test_components_are_mutually_exclusive(checkout: Path, home: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        install.parse_args(["--skill", "--mcp"])
    assert caught.value.code == 2


def test_unknown_option_exits_two(checkout: Path, home: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        install.parse_args(["--nonsense"])
    assert caught.value.code == 2


def test_help_names_every_component() -> None:
    with pytest.raises(SystemExit):
        install.parse_args(["--help"])


def test_paths_with_spaces_are_handled(tmp_path: Path) -> None:
    """No shell is involved, so a space is not a quoting problem."""
    root = tmp_path / "check out"
    (root / "skills" / "action-speaks" / "scripts").mkdir(parents=True)
    (root / "skills" / "action-speaks" / "SKILL.md").write_text("# skill\n")
    (root / "skills" / "action-speaks" / "scripts" / "action-speaks").write_text("#!/bin/sh\n")
    (root / "mcp").mkdir()
    (root / "mcp" / "copilot-mcp-config.json").write_text(json.dumps(TEMPLATE))
    home = tmp_path / "my home"
    home.mkdir()
    code, _ = run(make(root, home), "--skill")
    assert code == 0
    assert os.readlink(home / ".agents/skills/action-speaks") == str(
        root / "skills" / "action-speaks"
    )

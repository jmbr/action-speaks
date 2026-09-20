#!/usr/bin/env python3
"""Install the action-speaks skill, CLI and MCP server into the agent harnesses here.

    python3 install.py              install skill + CLI symlink + MCP config
    python3 install.py --skill      skill only
    python3 install.py --mcp        MCP config only
    python3 install.py --cli        `action-speaks` on PATH only
    python3 install.py --uninstall  remove what this script installed
    python3 install.py --dry-run    show what would change

Everything is wired with absolute paths, so no virtualenv has to be activated: the MCP
server names the interpreter the package was installed into, the skill wrapper finds it
through its own symlink, and the CLI symlink points at a wrapper that hard-codes it.

The skill is symlinked rather than copied, so editing it in the repository takes effect at
once and there is only one copy to maintain.

A run is planned before it is executed. `--dry-run` prints the same operations a real run
would apply, so the two cannot drift.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence, TextIO

LEGACY_SKILLS = ("lean-proof-check", "nullius")
LEGACY_CLI = "nullius"
SERVER = "action-speaks"
TEMPLATE_TOKEN = "__ACTION_SPEAKS_ROOT__"


class InstallError(Exception):
    """Something the user has to resolve; reported without mutating anything."""


# -- operations ------------------------------------------------------------
#
# Each carries its own description so a dry run and a real run read from one plan.


@dataclass
class Op:
    description: str

    def apply(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass
class Mkdir(Op):
    path: Path

    def apply(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)


@dataclass
class Symlink(Op):
    source: Path
    target: Path

    def apply(self) -> None:
        # `ln -sfn`: replace an existing link rather than linking inside the directory it
        # points at.
        if self.target.is_symlink() or self.target.exists():
            self.target.unlink()
        self.target.symlink_to(self.source)


@dataclass
class Remove(Op):
    path: Path

    def apply(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass
class MakeExecutable(Op):
    path: Path

    def apply(self) -> None:
        mode = self.path.stat().st_mode
        self.path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@dataclass
class WriteJson(Op):
    path: Path
    payload: dict
    backup: bool

    def apply(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.backup and self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
        # Atomic: a crash mid-write must not leave a truncated config behind.
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(self.payload, stream, indent=2)
                stream.write("\n")
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


@dataclass
class Plan:
    """Messages and mutations in the order they happen."""

    steps: list[tuple[str, Op | None]] = field(default_factory=list)

    def say(self, message: str) -> None:
        self.steps.append((message, None))

    def do(self, op: Op) -> None:
        self.steps.append(("", op))

    def operations(self) -> list[Op]:
        return [op for _, op in self.steps if op is not None]


# -- installer -------------------------------------------------------------


class Installer:
    """Plans and applies the install. Root, home, environment and output are injected so
    tests never reach the real installation."""

    def __init__(
        self,
        root: Path,
        home: Path,
        env: dict[str, str] | None = None,
        out: TextIO | None = None,
    ):
        self.root = root
        self.home = home
        self.env = dict(os.environ if env is None else env)
        self.out = out if out is not None else sys.stdout

    # -- paths --

    @property
    def skill_src(self) -> Path:
        return self.root / "skills" / SERVER

    @property
    def skill_dst(self) -> Path:
        return self.home / ".agents" / "skills" / SERVER

    @property
    def bin_dir(self) -> Path:
        override = self.env.get("ACTION_SPEAKS_BIN_DIR")
        return Path(override) if override else self.home / ".local" / "bin"

    @property
    def cli_src(self) -> Path:
        return self.root / ".venv" / "bin" / SERVER

    @property
    def cli_dst(self) -> Path:
        return self.bin_dir / SERVER

    @property
    def mcp_template(self) -> Path:
        return self.root / "mcp" / "copilot-mcp-config.json"

    @property
    def mcp_dst(self) -> Path:
        return self.home / ".copilot" / "mcp-config.json"

    # -- skill --

    def plan_skill(self, plan: Plan) -> None:
        plan.say(f"skill: {self.skill_dst} -> {self.skill_src}")
        if not (self.skill_src / "SKILL.md").is_file():
            raise InstallError(f"{self.skill_src / 'SKILL.md'} is missing")
        if self.skill_dst.exists() and not self.skill_dst.is_symlink():
            raise InstallError(
                f"{self.skill_dst} exists and is not a symlink.\n"
                "         Move it aside, then re-run. (It may be an older copy of this skill.)"
            )
        plan.do(Mkdir(f"mkdir -p {self.skill_dst.parent}", self.skill_dst.parent))
        plan.do(
            Symlink(f"link {self.skill_dst} -> {self.skill_src}", self.skill_src, self.skill_dst)
        )
        wrapper = self.skill_src / "scripts" / SERVER
        if wrapper.exists():
            plan.do(MakeExecutable(f"chmod +x {wrapper}", wrapper))
        plan.say("  ok (pi and Copilot both read ~/.agents/skills)")
        self.plan_legacy(plan)

    def plan_legacy(self, plan: Plan) -> None:
        """Remove installs under names this project used before.

        Only links pointing into this checkout are touched, so an unrelated skill sharing a
        name is left alone. The raw target is compared rather than the resolved one: a rename
        leaves the old link dangling, and a dangling link resolves to nothing.
        """
        for name in LEGACY_SKILLS:
            legacy = self.home / ".agents" / "skills" / name
            if self._points_into_checkout(legacy):
                plan.say(f"  removing superseded symlink {legacy} (skill renamed to {SERVER})")
                plan.do(Remove(f"rm {legacy}", legacy))
        old_cli = self.bin_dir / LEGACY_CLI
        if self._points_into_checkout(old_cli):
            plan.say(f"  removing superseded command {old_cli}")
            plan.do(Remove(f"rm {old_cli}", old_cli))

    def _points_into_checkout(self, link: Path) -> bool:
        if not link.is_symlink():
            return False
        target = os.readlink(link)
        return target == str(self.root) or target.startswith(f"{self.root}{os.sep}")

    def plan_skill_removal(self, plan: Plan) -> None:
        self.plan_legacy(plan)
        if self.skill_dst.is_symlink():
            plan.say(f"removing skill symlink {self.skill_dst}")
            plan.do(Remove(f"rm {self.skill_dst}", self.skill_dst))
        else:
            plan.say(f"skill: nothing to remove at {self.skill_dst}")

    # -- CLI --

    def plan_cli(self, plan: Plan) -> None:
        """The console script lives in the virtualenv, which would otherwise have to be
        activated before every call. It hard-codes its interpreter, so a symlink from a
        directory already on PATH works without activation."""
        if not os.access(self.cli_src, os.X_OK):
            plan.say(f"cli: no console script at {self.cli_src}")
            plan.say("     (create it with: python3 -m venv .venv && .venv/bin/pip install -e .)")
            return
        plan.say(f"cli: {self.cli_dst} -> {self.cli_src}")
        if self.cli_dst.exists() and not self.cli_dst.is_symlink():
            raise InstallError(f"{self.cli_dst} exists and is not a symlink; leaving it alone.")
        plan.do(Mkdir(f"mkdir -p {self.bin_dir}", self.bin_dir))
        plan.do(Symlink(f"link {self.cli_dst} -> {self.cli_src}", self.cli_src, self.cli_dst))
        entries = self.env.get("PATH", "").split(os.pathsep)
        if str(self.bin_dir) in entries:
            plan.say(f"  ok ('{SERVER}' is on PATH)")
        else:
            plan.say(f"  ok, but {self.bin_dir} is not on PATH; add it to use '{SERVER}' directly")

    def plan_cli_removal(self, plan: Plan) -> None:
        if self.cli_dst.is_symlink():
            plan.say(f"removing cli symlink {self.cli_dst}")
            plan.do(Remove(f"rm {self.cli_dst}", self.cli_dst))
        else:
            plan.say(f"cli: nothing to remove at {self.cli_dst}")

    # -- MCP --

    def _entry(self) -> dict:
        text = self.mcp_template.read_text().replace(TEMPLATE_TOKEN, str(self.root))
        entry = json.loads(text)["mcpServers"][SERVER]
        # Prefer the interpreter the package was installed into: it needs no PYTHONPATH and
        # cannot be shadowed by whatever `python3` means in the agent's environment.
        venv_python = self.root / ".venv" / "bin" / "python3"
        if venv_python.exists():
            entry["command"] = str(venv_python)
            entry.get("env", {}).pop("PYTHONPATH", None)
        return entry

    def _read_config(self) -> dict:
        if not self.mcp_dst.exists():
            return {}
        try:
            return json.loads(self.mcp_dst.read_text())
        except json.JSONDecodeError as exc:
            raise InstallError(f"{self.mcp_dst} is not valid JSON; fix or move it aside") from exc

    def plan_mcp(self, plan: Plan) -> None:
        plan.say(f"mcp: {self.mcp_dst}")
        entry = self._entry()
        config = self._read_config()
        servers = config.setdefault("mcpServers", {})
        superseded = servers.pop(LEGACY_CLI, None) is not None
        if servers.get(SERVER) == entry and not superseded:
            plan.say("  ok (already configured)")
            return
        if superseded:
            plan.say(f"  removing superseded server `{LEGACY_CLI}`")
        action = "updating" if SERVER in servers else "adding"
        servers[SERVER] = entry
        others = [name for name in servers if name != SERVER]
        plan.say(
            f"  {action} `{SERVER}`" + (f", preserving: {', '.join(others)}" if others else "")
        )
        if self.mcp_dst.exists():
            plan.say(f"  backing up existing config to {self.mcp_dst}.bak")
        plan.do(WriteJson(f"write {self.mcp_dst}", self.mcp_dst, config, backup=True))
        plan.say("  ok (restart Copilot to pick it up)")

    def plan_mcp_removal(self, plan: Plan) -> None:
        if not self.mcp_dst.is_file():
            plan.say(f"mcp: nothing to remove ({self.mcp_dst} does not exist)")
            return
        config = self._read_config()
        if config.get("mcpServers", {}).pop(SERVER, None) is None:
            plan.say(f"  mcp: `{SERVER}` not present")
            return
        plan.say(f"  removing `{SERVER}` from {self.mcp_dst}")
        plan.do(WriteJson(f"write {self.mcp_dst}", self.mcp_dst, config, backup=False))

    # -- driving --

    def plan(self, *, skill: bool, cli: bool, mcp: bool, uninstall: bool, dry: bool) -> Plan:
        plan = Plan()
        if dry:
            plan.say("(dry run - no changes will be made)")
        plan.say(f"verifier root: {self.root}")
        if uninstall:
            chosen = [
                (skill, self.plan_skill_removal),
                (cli, self.plan_cli_removal),
                (mcp, self.plan_mcp_removal),
            ]
        else:
            chosen = [(skill, self.plan_skill), (cli, self.plan_cli), (mcp, self.plan_mcp)]
        for wanted, step in chosen:
            if wanted:
                step(plan)
        if not dry and not uninstall:
            plan.say("")
            plan.say(f"Next: ./skills/{SERVER}/scripts/{SERVER} doctor")
            plan.say("      (checks Lean, Mathlib and that the verifier discriminates correctly)")
        return plan

    def run(self, plan: Plan, dry: bool) -> None:
        for message, op in plan.steps:
            if op is None:
                print(message, file=self.out)
            elif dry:
                print(f"  would: {op.description}", file=self.out)
            else:
                op.apply()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    component = parser.add_mutually_exclusive_group()
    component.add_argument("--skill", action="store_true", help="install the skill only")
    component.add_argument("--mcp", action="store_true", help="configure the MCP server only")
    component.add_argument("--cli", action="store_true", help=f"link `{SERVER}` onto PATH only")
    parser.add_argument(
        "--uninstall", action="store_true", help="remove what this script installed"
    )
    parser.add_argument(
        "-n", "--dry-run", dest="dry_run", action="store_true", help="show what would change"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, installer: Installer | None = None) -> int:
    args = parse_args(argv)
    selected = args.skill or args.mcp or args.cli
    installer = installer or Installer(Path(__file__).resolve().parent, Path.home())
    try:
        plan = installer.plan(
            skill=args.skill or not selected,
            cli=args.cli or not selected,
            mcp=args.mcp or not selected,
            uninstall=args.uninstall,
            dry=args.dry_run,
        )
        installer.run(plan, args.dry_run)
    except InstallError as exc:
        print(f"  ERROR: {exc}", file=installer.out)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

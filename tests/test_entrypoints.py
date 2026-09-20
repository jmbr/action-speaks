"""Check that the entry points work without a prepared environment.

Agents launch tools as subprocesses, and what they inherit is not the shell you installed
from: commonly no profile, so no `~/.elan/bin` on PATH, and certainly no activated
virtualenv. Every installed path is therefore absolute, and this checks that the claim holds
rather than assuming it — the failure it guards against is silent, because it only appears
under the agent, never when a human tries the same command by hand.

`log` is the probe: it resolves the configuration, and so exercises finding `lake`, without
paying for a Lean session.

Each case skips if that entry point is not installed, so this is safe in a commit hook.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.lean

ROOT = Path(__file__).resolve().parent.parent

# No PATH to lake, no virtualenv, no profile. HOME is kept because elan lives under it and
# an agent would inherit it too.
STRIPPED = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
LOG_CALL = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "log", "arguments": {"limit": 1}},
}


def run(cmd: list[str], stdin: str | None = None, timeout: int = 300):
    return subprocess.run(
        cmd,
        cwd="/tmp",
        env=STRIPPED,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def installed(path: Path, hint: str) -> Path:
    if not path.exists():
        pytest.skip(f"{hint} is not installed")
    return path


def test_skill_wrapper_runs_without_a_virtualenv() -> None:
    wrapper = installed(ROOT / "skills" / "nullius" / "scripts" / "nullius", "the skill wrapper")
    done = run([str(wrapper), "log", "-n", "1"])
    assert done.returncode == 0, f"rc={done.returncode}\n{done.stdout}\n{done.stderr}"


def test_mcp_server_runs_without_a_virtualenv() -> None:
    python = installed(ROOT / ".venv" / "bin" / "python3", "the checkout virtualenv")
    done = run(
        [str(python), "-m", "nullius.mcp_server"],
        stdin="\n".join(json.dumps(r) for r in (INITIALIZE, LOG_CALL)) + "\n",
    )
    replies = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    call = next((r for r in replies if r.get("id") == 2), None)
    assert call, f"no reply to the tool call: {done.stderr[:400]}"
    assert not call.get("result", {}).get("isError"), call


def test_installed_command_runs_without_a_virtualenv() -> None:
    cli = installed(Path.home() / ".local" / "bin" / "nullius", "~/.local/bin/nullius")
    done = run([str(cli), "log", "-n", "1"])
    assert done.returncode == 0, f"rc={done.returncode}\n{done.stdout}\n{done.stderr}"

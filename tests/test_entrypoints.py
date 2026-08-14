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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# No PATH to lake, no virtualenv, no profile. HOME is kept because elan lives under it and
# an agent would inherit it too.
STRIPPED = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}


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


def main() -> int:
    failures: list[str] = []
    checked = 0

    wrapper = ROOT / "skills" / "nullius" / "scripts" / "nullius"
    if wrapper.exists():
        checked += 1
        p = run([str(wrapper), "log", "-n", "1"])
        ok = p.returncode == 0
        print(f"  {'ok  ' if ok else 'FAIL'}  skill wrapper, stripped environment")
        if not ok:
            failures.append(f"skill wrapper: rc={p.returncode}\n{p.stdout}\n{p.stderr}")

    venv_python = ROOT / ".venv" / "bin" / "python3"
    if venv_python.exists():
        checked += 1
        reqs = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "log", "arguments": {"limit": 1}},
            },
        ]
        p = run(
            [str(venv_python), "-m", "nullius.mcp_server"],
            stdin="\n".join(json.dumps(r) for r in reqs) + "\n",
        )
        replies = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
        call = next((r for r in replies if r.get("id") == 2), None)
        ok = bool(call) and not call.get("result", {}).get("isError")
        print(f"  {'ok  ' if ok else 'FAIL'}  MCP server, stripped environment")
        if not ok:
            failures.append(f"MCP server: {call or p.stderr[:400]}")

    cli = Path.home() / ".local" / "bin" / "nullius"
    if cli.exists():
        checked += 1
        p = run([str(cli), "log", "-n", "1"])
        ok = p.returncode == 0
        print(f"  {'ok  ' if ok else 'FAIL'}  ~/.local/bin/nullius, stripped environment")
        if not ok:
            failures.append(f"cli symlink: rc={p.returncode}\n{p.stdout}\n{p.stderr}")

    if not checked:
        print("nothing installed to check (run ./install.sh) — skipping")
        return 0

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("Entry points work without an activated virtualenv or a prepared PATH.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

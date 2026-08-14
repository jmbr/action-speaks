"""Catch names and version strings that have drifted out of date.

Three renames and a toolchain downgrade in quick succession left stale references scattered
through the documentation every time: MCP tools that no longer exist, a CLI subcommand that
was renamed, a Lean version that had moved. None of it breaks a test, because prose is not
executed — it just quietly misleads whoever reads it next.

This runs in milliseconds and needs no Lean, so it is cheap enough for a commit hook.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Literal strings that must never reappear, with the reason they were retired.
FORBIDDEN = {
    "lean_verify": "MCP tools are named after the CLI subcommands: use `verify`",
    "lean_check_statement": "renamed to `statement`",
    "lean_search_lemma": "renamed to `search`",
    "lean_find_proof": "renamed to `close`",
    "lean_ledger": "renamed to `log`",
    "nullius goal": "the `goal` subcommand is now `close`",
    "nullius.cli goal": "the `goal` subcommand is now `close`",
    "leanai": "the project is called nullius",
    "LeanAI": "the project is called nullius",
    "LEANAI": "the project is called nullius",
    "lean-ai": "the project is called nullius",
    "lean-proof-check": "the skill is named after the project: `nullius`",
    "indexes Mathlib only": "shape search covers Physlib and Cslib too when loogle is built locally",
    "Mathlib and Physlib are already imported": "Cslib is imported as well; naming two of the three misleads",
    "test_cookbook.py": "renamed to tests/test_docs.py, which also checks the skill docs",
}

# Where a documented revision is expected to match what lake actually pins.
REV_CITATION = re.compile(r"\b(mathlib|physlib|cslib)\b[^0-9a-f\n]{0,4}([0-9a-f]{8,40})\b", re.I)
LEAN_VERSION = re.compile(r"\b(?:leanprover/lean4:)?v?(4\.\d+\.\d+)\b")

SKIP_DIRS = ("lean/.lake/", ".agent-shell/", "tests/check_names.py")

# Deliberate uses of a retired name: (path, string). Migration code has to name the thing it
# is migrating away from.
ALLOWED = {
    ("install.sh", "lean-proof-check"),
}


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [
        ROOT / p
        for p in out.split("\0")
        if p and not any(p.startswith(d) or p == d for d in SKIP_DIRS)
    ]


def pinned() -> tuple[str, dict[str, str]]:
    """The Lean version and package revisions lake is actually using."""
    toolchain = (ROOT / "lean" / "lean-toolchain").read_text().strip()
    version = LEAN_VERSION.search(toolchain)
    manifest = json.loads((ROOT / "lean" / "lake-manifest.json").read_text())
    revs = {
        p["name"].lower(): p.get("rev", "")
        for p in manifest.get("packages", [])
        if p.get("rev")
    }
    return (version.group(1) if version else ""), revs


def main() -> int:
    version, revs = pinned()
    problems: list[str] = []

    for path in tracked_files():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        rel = path.relative_to(ROOT)

        for lineno, line in enumerate(text.splitlines(), start=1):
            for bad, why in FORBIDDEN.items():
                if bad in line and (str(rel), bad) not in ALLOWED:
                    problems.append(f"{rel}:{lineno}: stale name {bad!r} — {why}")

            # A cited Lean version must be the one that is pinned.
            for found in LEAN_VERSION.findall(line):
                if version and found != version:
                    problems.append(
                        f"{rel}:{lineno}: cites Lean {found}, but lean-toolchain pins {version}"
                    )

            # A cited Mathlib/Physlib/Cslib revision must be a prefix of the pinned one.
            for pkg, rev in REV_CITATION.findall(line):
                want = revs.get(pkg.lower(), "")
                if want and not want.startswith(rev.lower()):
                    problems.append(
                        f"{rel}:{lineno}: cites {pkg} {rev}, but lake pins {want[:12]}"
                    )

    if problems:
        print(f"stale references ({len(problems)}):")
        for p in problems:
            print("  -", p)
        print("\nUpdate them, or add a deliberate exception in tests/check_names.py.")
        return 1
    print("No stale names or version citations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Literal strings that must never reappear, with the reason they were retired.
FORBIDDEN = {
    "lean_verify": "MCP tools are named after the CLI subcommands: use `verify`",
    "lean_check_statement": "renamed to `statement`",
    "lean_search_lemma": "renamed to `search`",
    "lean_find_proof": "renamed to `close`",
    "lean_ledger": "renamed to `log`",
    "action-speaks goal": "the `goal` subcommand is now `close`",
    "action_speaks.cli goal": "the `goal` subcommand is now `close`",
    "leanai": "the project is called action-speaks",
    "LeanAI": "the project is called action-speaks",
    "LEANAI": "the project is called action-speaks",
    "lean-ai": "the project is called action-speaks",
    "lean-proof-check": "the skill is named after the project: `action-speaks`",
    "indexes Mathlib only": (
        "shape search covers Physlib, Cslib and FloatLib too when loogle is built locally"
    ),
    "Mathlib and Physlib are already imported": (
        "Cslib is imported as well; naming two of the three misleads"
    ),
    "test_cookbook.py": "renamed to tests/test_docs.py, which also checks the skill docs",
    "check_names.py": "renamed to tests/test_names.py, so pytest discovers it",
    "nullius": "the project is called action-speaks-louder-than-words",
    "Nullius": "the Lean package is called ActionSpeaks",
    "NULLIUS_": "environment variables are prefixed ACTION_SPEAKS_",
}

# Where a documented revision is expected to match what lake actually pins.
REV_CITATION = re.compile(r"\b(mathlib|physlib|cslib)\b[^0-9a-f\n]{0,4}([0-9a-f]{8,40})\b", re.I)
# A citation has to name Lean to count. Matching a bare `4.x.y` caught unrelated pins that
# merely look like one -- a `rev: v4.18.1` for a commit-hook tool, say -- and a version
# number with nothing to attach it to is not a Lean citation in the first place.
LEAN_VERSION = re.compile(r"(?:leanprover/lean4:|\bLean\s+)v?(4\.\d+\.\d+)\b")

SKIP_DIRS = ("lean/.lake/", ".agent-shell/", "tests/test_names.py")

# Deliberate uses of a retired name: (path, string). Migration code has to name the thing it
# is migrating away from.
ALLOWED = {
    ("install.py", "lean-proof-check"),
    # The installer removes the superseded install, so it and its tests have to name it.
    ("install.py", "nullius"),
    ("tests/test_install.py", "lean-proof-check"),
    ("tests/test_install.py", "nullius"),
}

# Files whose subject is comparing releases, so naming more than the pinned one is the point
# rather than drift. Only the version check is relaxed; retired names are still caught here.
MULTI_VERSION = {
    "tests/test_update_check.py",
    "scripts/check_updates.py",
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
        p["name"].lower(): p.get("rev", "") for p in manifest.get("packages", []) if p.get("rev")
    }
    return (version.group(1) if version else ""), revs


@pytest.fixture(scope="module")
def sources() -> list[tuple[Path, str]]:
    """Every tracked file that can be read as text, with its path relative to the root."""
    out = []
    for path in tracked_files():
        try:
            out.append((path.relative_to(ROOT), path.read_text()))
        except (UnicodeDecodeError, FileNotFoundError):
            continue
    return out


def report(problems: list[str]) -> None:
    assert not problems, (
        "stale references ({}):\n  - {}\n\nUpdate them, or add a "
        "deliberate exception in tests/test_names.py.".format(
            len(problems), "\n  - ".join(problems)
        )
    )


def test_no_retired_names_are_cited(sources: list[tuple[Path, str]]) -> None:
    problems = [
        f"{rel}:{lineno}: stale name {bad!r} — {why}"
        for rel, text in sources
        for lineno, line in enumerate(text.splitlines(), start=1)
        for bad, why in FORBIDDEN.items()
        if bad in line and (str(rel), bad) not in ALLOWED
    ]
    report(problems)


def test_cited_lean_version_is_the_pinned_one(sources: list[tuple[Path, str]]) -> None:
    version, _ = pinned()
    if not version:
        pytest.skip("lean/lean-toolchain names no version to compare against")
    problems = [
        f"{rel}:{lineno}: cites Lean {found}, but lean-toolchain pins {version}"
        for rel, text in sources
        if str(rel) not in MULTI_VERSION
        for lineno, line in enumerate(text.splitlines(), start=1)
        for found in LEAN_VERSION.findall(line)
        if found != version
    ]
    report(problems)


def test_cited_library_revisions_are_the_pinned_ones(sources: list[tuple[Path, str]]) -> None:
    """A cited Mathlib, Physlib or Cslib revision must be a prefix of the pinned one."""
    _, revs = pinned()
    problems = [
        f"{rel}:{lineno}: cites {pkg} {rev}, but lake pins {revs[pkg.lower()][:12]}"
        for rel, text in sources
        if str(rel) not in MULTI_VERSION
        for lineno, line in enumerate(text.splitlines(), start=1)
        for pkg, rev in REV_CITATION.findall(line)
        if revs.get(pkg.lower()) and not revs[pkg.lower()].startswith(rev.lower())
    ]
    report(problems)

"""The update check: does it report convergence, rather than latest-ness?

No network. Every case drives `scripts/check_updates.py` with a canned view of upstream, so
what is tested is the judgement — which release every required library can agree on — and
not GitHub's availability.

The case that matters is the one that actually happened: at v4.33.0 every shipped library
agreed, and moving there lost Statlib, which had an exact tag for the release being left.
A check that only reported "you are behind" would have recommended that move in silence.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_updates as C  # noqa: E402

TOOLCHAIN = "leanprover/lean4:v{}"
LAKEFILE = '[[require]]\nname = "mathlib"\nrev = "{}"\n'
# `repl` declares no Mathlib of its own, so it must be judged on its toolchain alone.
NO_MATHLIB = '[[require]]\nname = "batteries"\nrev = "abc123"\n'


def upstream(
    spec: dict[str, dict[str, tuple[str, str | None]]],
    tags=("v4.33.0", "v4.32.0"),
    branches: dict[str, str] | None = None,
):
    """A fake GitHub: spec[repo][ref] = (toolchain version, lakefile body or None)."""
    branches = branches or {}

    def get(path: str):
        if path.startswith("/repos/") and path.count("/") == 3:
            return {"default_branch": branches.get(path.split("/repos/")[1], "main")}
        if "/commits/" in path:
            return {"sha": "c" * 40}
        if path.startswith("/repos/leanprover-community/mathlib4/tags"):
            return [{"name": t} for t in tags]
        if "/git/ref/tags/" in path:
            return {"object": {"type": "commit", "sha": "0" * 40}}
        if "/contents/" in path:
            repo, rest = path.split("/repos/")[1].split("/contents/", maxsplit=1)
            filename, ref = rest.split("?ref=", maxsplit=1)
            entry = spec.get(repo, {}).get(ref)
            if entry is None:
                return None
            toolchain, body = entry
            if filename == "lean-toolchain":
                return {"content": base64.b64encode(TOOLCHAIN.format(toolchain).encode()).decode()}
            if filename == "lakefile.toml":
                if body is None:
                    return None
                return {"content": base64.b64encode(body.encode()).decode()}
            return None
        return None

    return get


def shipped(mathlib: str, release: str) -> dict:
    """Every required library tagged for `release` and agreeing on `mathlib`."""
    return {
        "leanprover-community/physlib": {release: (release.lstrip("v"), LAKEFILE.format(mathlib))},
        "leanprover/cslib": {release: (release.lstrip("v"), LAKEFILE.format(mathlib))},
        "leanprover-community/repl": {release: (release.lstrip("v"), NO_MATHLIB)},
    }


@pytest.fixture
def pins(monkeypatch):
    monkeypatch.setattr(
        C.Pins, "read", classmethod(lambda cls: C.Pins("4.32.0", {"mathlib": "old"}))
    )


def survey(monkeypatch, get, candidates=False, limit=2):
    monkeypatch.setattr(C, "get", get)
    return C.survey(candidates, limit)


def row(data, release):
    return next(r for r in data["releases"] if r["release"] == release)


def test_converges_when_every_required_library_agrees(monkeypatch, pins) -> None:
    data = survey(monkeypatch, upstream(shipped("v4.33.0", "v4.33.0")))
    assert row(data, "v4.33.0")["converges"]
    assert row(data, "v4.33.0")["mathlib"] == "v4.33.0"


def test_a_library_without_a_tag_blocks_the_release(monkeypatch, pins) -> None:
    spec = shipped("v4.33.0", "v4.33.0")
    del spec["leanprover-community/physlib"]["v4.33.0"]
    data = survey(monkeypatch, upstream(spec))
    assert not row(data, "v4.33.0")["converges"]
    assert (
        row(data, "v4.33.0")["libraries"]["Physlib"]["why_not"]
        == "nothing built against this release"
    )


def test_disagreeing_mathlib_blocks_the_release(monkeypatch, pins) -> None:
    """Lake admits one Mathlib, so a patch-level difference is still a difference."""
    spec = shipped("v4.33.0", "v4.33.0")
    spec["leanprover/cslib"]["v4.33.0"] = ("4.33.0", LAKEFILE.format("v4.33.1"))
    data = survey(monkeypatch, upstream(spec))
    assert not row(data, "v4.33.0")["converges"]


def test_a_library_declaring_no_mathlib_does_not_block(monkeypatch, pins) -> None:
    """`repl` names none; requiring one of it would veto every release."""
    data = survey(monkeypatch, upstream(shipped("v4.33.0", "v4.33.0")))
    repl = row(data, "v4.33.0")["libraries"]["repl"]
    assert repl["mathlib"] is None
    assert repl["usable"]
    assert row(data, "v4.33.0")["converges"]


def test_a_tag_built_against_another_toolchain_is_not_usable(monkeypatch, pins) -> None:
    spec = shipped("v4.33.0", "v4.33.0")
    spec["leanprover/cslib"]["v4.33.0"] = ("4.32.0", LAKEFILE.format("v4.33.0"))
    data = survey(monkeypatch, upstream(spec))
    assert not row(data, "v4.33.0")["converges"]
    assert "v4.32.0" in row(data, "v4.33.0")["libraries"]["cslib"]["why_not"]


def test_moving_refs_are_reported(monkeypatch, pins) -> None:
    """A branch pin cannot be reproduced, so the bump it suggests is not the bump you get."""
    spec = shipped("v4.33.0", "v4.33.0")
    spec["leanprover/cslib"]["v4.33.0"] = (
        "4.33.0",
        LAKEFILE.format("v4.33.0") + '[[require]]\nname = "subverso"\nrev = "main"\n',
    )
    data = survey(monkeypatch, upstream(spec))
    assert row(data, "v4.33.0")["libraries"]["cslib"]["moving_refs"] == ["subverso"]


def test_a_candidate_is_reported_lost_but_does_not_veto(monkeypatch, pins) -> None:
    """The v4.33.0 case: everything shipped agreed, and the move cost Statlib."""
    spec = shipped("v4.33.0", "v4.33.0")
    spec["stat-lib/statlib"] = {"v4.32.0": ("4.32.0", LAKEFILE.format("old"))}
    data = survey(monkeypatch, upstream(spec), candidates=True)
    moved = row(data, "v4.33.0")
    assert moved["converges"], "a candidate must not block a release"
    assert "Statlib" in moved["candidates_lost"]
    assert "Statlib" not in row(data, "v4.32.0")["candidates_lost"]


def test_no_converging_release_is_stated_plainly(monkeypatch, pins, capsys) -> None:
    spec = shipped("v4.33.0", "v4.33.0")
    del spec["leanprover/cslib"]["v4.33.0"]
    data = survey(monkeypatch, upstream(spec))
    assert C.report(data, candidates=False) == 0
    assert "No release satisfies" in capsys.readouterr().out


def test_report_emits_pins_for_a_newer_release(monkeypatch, pins, capsys) -> None:
    data = survey(monkeypatch, upstream(shipped("v4.33.0", "v4.33.0")))
    monkeypatch.setattr(C, "resolve", lambda repo, ref: "f" * 40)
    assert C.report(data, candidates=False) == 1, "a move being available is worth an exit code"
    out = capsys.readouterr().out
    assert "leanprover/lean4:v4.33.0" in out
    assert "f" * 40 in out
    # Loogle reads .olean files directly, so a stale binary fails every query.
    assert "build-loogle" in out


def test_already_current_is_not_reported_as_a_move(monkeypatch, capsys) -> None:
    monkeypatch.setattr(C.Pins, "read", classmethod(lambda cls: C.Pins("4.33.0", {})))
    data = survey(monkeypatch, upstream(shipped("v4.33.0", "v4.33.0")))
    assert C.report(data, candidates=False) == 0
    assert "Already on the newest" in capsys.readouterr().out


def test_release_candidates_are_never_offered(monkeypatch, pins) -> None:
    get = upstream(shipped("v4.33.0", "v4.33.0"), tags=("v4.34.0-rc1", "v4.33.0", "v4.32.0"))
    data = survey(monkeypatch, get, limit=5)
    assert [r["release"] for r in data["releases"]] == ["v4.33.0", "v4.32.0"]


def test_rate_limiting_is_reported_not_guessed_around(monkeypatch, capsys) -> None:
    def limited(path):
        raise C.Unavailable("GitHub rate limit reached; set GITHUB_TOKEN and retry")

    monkeypatch.setattr(C, "get", limited)
    assert C.main(["--limit", "1"]) == 2
    assert "rate limit" in capsys.readouterr().err


def test_json_output_is_machine_readable(monkeypatch, pins, capsys) -> None:
    monkeypatch.setattr(C, "get", upstream(shipped("v4.33.0", "v4.33.0")))
    assert C.main(["--json", "--limit", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["current"]["toolchain"] == "4.32.0"
    assert any(r["converges"] for r in payload["releases"])


def test_a_default_branch_counts_when_there_is_no_tag(monkeypatch, pins) -> None:
    """Physlib tags per release but lags its own branch, and FloatLib tags by its own
    version. Judging on a release-named tag alone reported both as unavailable while they
    were in fact built against it."""
    spec = shipped("v4.33.0", "v4.33.0")
    del spec["leanprover-community/physlib"]["v4.33.0"]
    spec["leanprover-community/physlib"]["master"] = ("4.33.0", LAKEFILE.format("v4.33.0"))
    data = survey(monkeypatch, upstream(spec, branches={"leanprover-community/physlib": "master"}))
    physlib = row(data, "v4.33.0")["libraries"]["Physlib"]
    assert physlib["usable"]
    assert physlib["from_branch"], "a branch pin is a commit, and the report has to say so"
    assert row(data, "v4.33.0")["converges"]


def test_a_release_candidate_is_not_the_release(monkeypatch, pins) -> None:
    """`v4.33.0-rc2` truncated to `4.33.0` once, which read as ready two prereleases early."""
    spec = shipped("v4.33.0", "v4.33.0")
    del spec["leanprover/cslib"]["v4.33.0"]
    spec["leanprover/cslib"]["main"] = ("4.33.0-rc2", LAKEFILE.format("v4.33.0-rc2"))
    data = survey(monkeypatch, upstream(spec))
    assert not row(data, "v4.33.0")["libraries"]["cslib"]["usable"]
    assert not row(data, "v4.33.0")["converges"]

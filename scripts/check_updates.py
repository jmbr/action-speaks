#!/usr/bin/env python3
"""Report which Lean release this checkout could move to, and what it would cost.

Lake admits one Mathlib in the dependency graph, so a bump is possible only at a release
where *every* required library agrees on the same Mathlib revision. Checking that by hand
means reading four repositories, and the answer is not monotonic: moving forward can lose a
library that had a tag for the release you are on and none for the next. That happened at
v4.33.0, where Statlib had an exact match for the old pin and no final tag for the new one.

So this reports convergence rather than latest-ness. "You are behind" is not actionable on
its own; "v4.33.0 works for everything you ship, and costs you Statlib" is.

    scripts/check_updates.py               # human-readable report
    scripts/check_updates.py --json        # same data for a workflow
    scripts/check_updates.py --candidates  # include libraries not yet required

Needs no built checkout and no Lean: it reads this repository's pins from `lean/`, and
everything else from the GitHub API. Unauthenticated requests are rate-limited to 60 an
hour, which is enough for one run; set GITHUB_TOKEN to raise it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"

# Libraries this checkout requires. `mathlib` is the axis everything is compared against, so
# it is not listed as a participant.
SHIPPED = {
    "Physlib": "leanprover-community/physlib",
    "cslib": "leanprover/cslib",
    "repl": "leanprover-community/repl",
}

# Libraries under consideration but not required. Reported only with --candidates, and never
# allowed to hold back a release: losing one is information, not a veto.
CANDIDATES = {
    "Statlib": "stat-lib/statlib",
    "FloatLib": "lean-dojo/FloatLib",
}

RELEASE = re.compile(r"^v4\.\d+\.\d+$")
TOOLCHAIN = re.compile(r"v(4\.\d+\.\d+(?:-rc\d+)?)")
# `rev = "..."` in a lakefile.toml require block, or `@ "..."` in a lakefile.lean one.
TOML_REV = re.compile(r'name\s*=\s*"mathlib".*?rev\s*=\s*"([^"]+)"', re.S | re.I)
LEAN_REV = re.compile(r'mathlib4"?\s*@\s*"([^"]+)"', re.I)


class Unavailable(RuntimeError):
    """Upstream could not be read. Reported, never guessed around."""


def get(path: str) -> object:
    request = urllib.request.Request(f"{API}{path}", headers={"Accept": "application/vnd.github+json"})
    if token := os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in (403, 429):
            raise Unavailable("GitHub rate limit reached; set GITHUB_TOKEN and retry") from exc
        raise Unavailable(f"GitHub returned {exc.code} for {path}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise Unavailable(f"cannot reach GitHub: {exc}") from exc


def text_at(repo: str, path: str, ref: str) -> str | None:
    """A file's contents at a ref, or None when it is absent."""
    data = get(f"/repos/{repo}/contents/{path}?ref={ref}")
    if not isinstance(data, dict) or "content" not in data:
        return None
    import base64

    return base64.b64decode(data["content"]).decode("utf-8", "replace")


@dataclass
class Pins:
    """What this checkout currently builds against."""

    toolchain: str
    revisions: dict[str, str]

    @classmethod
    def read(cls) -> "Pins":
        toolchain = (ROOT / "lean" / "lean-toolchain").read_text().strip()
        found = TOOLCHAIN.search(toolchain)
        manifest = json.loads((ROOT / "lean" / "lake-manifest.json").read_text())
        return cls(
            toolchain=found.group(1) if found else toolchain,
            revisions={p["name"]: p.get("rev", "") for p in manifest.get("packages", [])},
        )


@dataclass
class Support:
    """What one library says about one release."""

    release: str
    tagged: bool
    ref: str | None = None
    toolchain: str | None = None
    mathlib: str | None = None
    moving: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Built against this release, whether from a tag or the default branch.

        A missing Mathlib requirement is not a failure: `repl` declares none, so it is
        judged on its toolchain alone. Only libraries that name a Mathlib take part in the
        agreement test below.
        """
        return self.ref is not None and self.toolchain == self.release.lstrip("v")

    def why_not(self) -> str | None:
        if self.ref is None:
            return "nothing built against this release"
        if self.toolchain != self.release.lstrip("v"):
            return f"tagged, but built against v{self.toolchain}"
        return None


def mathlib_required(body: str | None) -> str | None:
    if not body:
        return None
    if found := TOML_REV.search(body):
        return found.group(1)
    if found := LEAN_REV.search(body):
        return found.group(1)
    return None


def moving_refs(body: str | None) -> list[str]:
    """Requires pinned to a branch rather than a commit or tag.

    A moving ref cannot be reproduced, so a library that uses one is reported even when its
    Mathlib agrees: the bump it suggests would not be the bump somebody else got.
    """
    if not body:
        return []
    out = []
    for block in re.split(r"\[\[require\]\]", body)[1:]:
        name = re.search(r'name\s*=\s*"([^"]+)"', block)
        rev = re.search(r'rev\s*=\s*"([^"]+)"', block)
        if name and rev and rev.group(1) in ("main", "master", "HEAD"):
            out.append(name.group(1))
    return out


def _at_ref(repo: str, release: str, ref: str, tagged: bool) -> Support | None:
    toolchain = text_at(repo, "lean-toolchain", ref)
    if toolchain is None:
        return None
    body = text_at(repo, "lakefile.toml", ref) or text_at(repo, "lakefile.lean", ref)
    found = TOOLCHAIN.search(toolchain)
    return Support(
        release=release,
        tagged=tagged,
        ref=ref,
        toolchain=found.group(1) if found else toolchain.strip(),
        mathlib=mathlib_required(body),
        moving=moving_refs(body),
    )


def support(repo: str, release: str) -> Support:
    """What `repo` offers for `release`, by tag or, failing that, on its default branch.

    Not every library tags per Lean release. Physlib does, but lags its own branch; FloatLib
    tags by its own version entirely. Judging only by a tag named after the release reports
    both as unavailable while they are in fact built against it — which is how this check
    missed that v4.34.0 had become reachable. The branch is reported as untagged so the
    difference stays visible: a pin taken from it is a commit, not a release.
    """
    if (tagged := _at_ref(repo, release, release, True)) is not None:
        return tagged
    head = _at_ref(repo, release, default_branch(repo), False)
    if head is not None and head.toolchain == release.lstrip("v"):
        return head
    return Support(release=release, tagged=False)


def default_branch(repo: str) -> str:
    data = get(f"/repos/{repo}")
    return data.get("default_branch", "main") if isinstance(data, dict) else "main"


def releases(limit: int) -> list[str]:
    """Mathlib's stable releases, newest first. Release candidates are not offered."""
    tags = get("/repos/leanprover-community/mathlib4/tags?per_page=100")
    names = [t["name"] for t in tags or [] if RELEASE.match(t["name"])]
    return names[:limit]


def resolve(repo: str, ref: str) -> str | None:
    """The commit `ref` names, whether it is a tag or a branch."""
    data = get(f"/repos/{repo}/git/ref/tags/{ref}")
    if isinstance(data, dict):
        obj = data.get("object", {})
        if obj.get("type") == "tag":
            inner = get(f"/repos/{repo}/git/tags/{obj['sha']}")
            return inner.get("object", {}).get("sha") if isinstance(inner, dict) else None
        return obj.get("sha")
    commits = get(f"/repos/{repo}/commits/{ref}")
    return commits.get("sha") if isinstance(commits, dict) else None


def survey(candidates: bool, limit: int) -> dict:
    pins = Pins.read()
    libraries = dict(SHIPPED)
    if candidates:
        libraries |= CANDIDATES

    rows = []
    for release in releases(limit):
        answers = {name: support(repo, release) for name, repo in libraries.items()}
        shipped_ok = [n for n in SHIPPED if answers[n].usable]
        # Only libraries that name a Mathlib take part; `repl` names none.
        agree = {answers[n].mathlib for n in shipped_ok if answers[n].mathlib}
        rows.append(
            {
                "release": release,
                "libraries": {
                    n: {
                        "tagged": a.tagged,
                        "toolchain": a.toolchain,
                        "mathlib": a.mathlib,
                        "moving_refs": a.moving,
                        "usable": a.usable,
                        "why_not": a.why_not(),
                        "ref": a.ref,
                        "from_branch": a.ref is not None and not a.tagged,
                    }
                    for n, a in answers.items()
                },
                # `repl` has no Mathlib requirement of its own, so it is judged on its
                # toolchain alone; everything else must name the same Mathlib.
                "converges": len(shipped_ok) == len(SHIPPED) and len(agree) <= 1,
                "mathlib": next(iter(agree)) if len(agree) == 1 else None,
                "candidates_lost": [
                    n for n in CANDIDATES if n in answers and not answers[n].usable
                ],
            }
        )
    return {"current": {"toolchain": pins.toolchain, "revisions": pins.revisions}, "releases": rows}


def report(data: dict, candidates: bool) -> int:
    current = data["current"]
    print(f"current toolchain: v{current['toolchain']}")
    for name in ("mathlib", *SHIPPED):
        if rev := current["revisions"].get(name):
            print(f"  {name:<10} {rev[:12]}")

    usable = [r for r in data["releases"] if r["converges"]]
    newest = usable[0] if usable else None
    print()
    print(f"{'release':<10} {'converges':<10} notes")
    for row in data["releases"]:
        notes = []
        for name, info in row["libraries"].items():
            if name in SHIPPED and info.get("why_not"):
                notes.append(f"{name}: {info['why_not']}")
            elif info.get("from_branch"):
                notes.append(f"{name}: from {info['ref']}, pin the commit")
            if info["moving_refs"]:
                notes.append(f"{name}: moving ref {', '.join(info['moving_refs'])}")
        if candidates and row["candidates_lost"]:
            notes.append("would lose " + ", ".join(row["candidates_lost"]))
        mark = "yes" if row["converges"] else "no"
        current_mark = "  <- current" if row["release"].lstrip("v") == current["toolchain"] else ""
        print(f"{row['release']:<10} {mark:<10} {'; '.join(notes)}{current_mark}")

    print()
    if newest is None:
        print("No release satisfies every required library. Nothing to do but wait.")
        return 0
    if newest["release"].lstrip("v") == current["toolchain"]:
        print(f"Already on the newest converging release ({newest['release']}).")
        return 0

    print(f"Newest converging release: {newest['release']}")
    if candidates and newest["candidates_lost"]:
        print(f"  Moving there would lose: {', '.join(newest['candidates_lost'])}")
    print("\nPins for lean/lakefile.toml:")
    print(f'  lean-toolchain            leanprover/lean4:{newest["release"]}')
    print(f'  mathlib   rev = "{newest["release"]}"')
    for name, repo in SHIPPED.items():
        ref = newest["libraries"].get(name, {}).get("ref") or newest["release"]
        sha = resolve(repo, ref)
        print(f'  {name:<9} rev = "{sha}"' if sha else f"  {name:<9} (resolve by hand: no tag)")
    print("\nAfter bumping: rebuild, rerun scripts/build-loogle.sh (it reads .olean files")
    print("directly, so a stale binary reports an incompatible header), and run the suite.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="emit the survey as JSON")
    parser.add_argument(
        "--candidates", action="store_true", help="include libraries not yet required"
    )
    parser.add_argument("--limit", type=int, default=6, help="how many releases to examine")
    args = parser.parse_args(argv)
    try:
        data = survey(args.candidates, args.limit)
    except Unavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    return report(data, args.candidates)


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify every Lean block in COOKBOOK.md.

Documentation that claims things about a verifier should not be taken on faith either. Each
fenced ```lean block in the cookbook carries an `-- expect:` line saying what the verifier
must return for it, and this script checks that it still does. A block without such a line is
an error rather than a skip, so nothing can quietly escape checking.

Expectations:
    verified              accepted as-is
    verified-nontrivial   accepted with --require-nontrivial (hypotheses must be used)
    rejected              must NOT be accepted (a deliberately broken example)
    rejected-nontrivial   accepted normally, but rejected with --require-nontrivial
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nullius.repl import Session  # noqa: E402
from nullius.verify import Verifier  # noqa: E402

DOC = Path(__file__).resolve().parent.parent / "COOKBOOK.md"
BLOCK = re.compile(r"```lean\n(.*?)```", re.DOTALL)
EXPECT = re.compile(r"^--\s*expect:\s*(\S+)\s*$", re.MULTILINE)
VALID = {"verified", "verified-nontrivial", "rejected", "rejected-nontrivial"}


def blocks() -> list[tuple[int, str, str]]:
    text = DOC.read_text()
    out = []
    for i, m in enumerate(BLOCK.finditer(text), start=1):
        body = m.group(1)
        line = text[: m.start()].count("\n") + 1
        exp = EXPECT.search(body)
        if not exp:
            raise SystemExit(f"{DOC.name}:{line}: lean block has no `-- expect:` line")
        if exp.group(1) not in VALID:
            raise SystemExit(f"{DOC.name}:{line}: unknown expectation {exp.group(1)!r}")
        out.append((line, exp.group(1), EXPECT.sub("", body).strip() + "\n"))
    return out


def main() -> int:
    cases = blocks()
    print(f"{DOC.name}: {len(cases)} lean block(s)\n")

    session = Session()
    session.start()
    vf = Verifier(session)
    failures: list[str] = []

    for line, expect, src in cases:
        nontrivial = expect.endswith("-nontrivial")
        want_ok = expect.startswith("verified")
        v = vf.verify(src, require_nontrivial=nontrivial)
        ok = v.verified == want_ok
        caught = [c.name for c in v.checks if not c.passed]
        name = v.target or "?"
        print(
            f"  {'ok  ' if ok else 'FAIL'}  {DOC.name}:{line:<4} {expect:20s} "
            f"{name:32s} {v.status}"
            + (f" caught_by={caught}" if caught else "")
        )
        if not ok:
            failures.append(
                f"{DOC.name}:{line} expected {expect}, got {v.status}\n{v.render()}"
            )

    session.close()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("Every example in the cookbook still behaves as documented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

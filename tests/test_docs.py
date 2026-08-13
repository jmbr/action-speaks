"""Verify the Lean examples embedded in the documentation.

Documentation that claims things about a verifier should not be taken on faith either. Two
kinds of example are checked:

* Fenced ```lean blocks in COOKBOOK.md, each carrying an `-- expect:` line saying what the
  verifier must return. A block without one is an error rather than a skip, so nothing can
  quietly escape checking.
* Proofs embedded in `<<'EOF' … EOF` heredocs in the agent-facing docs, which are shown as
  working invocations and must therefore verify. These are checked because they *did* rot:
  the refutation example in GUIDE.md stopped elaborating and nobody noticed.

Expectations for cookbook blocks:
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

from nullius.config import ConfigError  # noqa: E402
from nullius.repl import Session  # noqa: E402
from nullius.verify import Verifier  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COOKBOOK = ROOT / "COOKBOOK.md"
HEREDOC_DOCS = [
    ROOT / "skills" / "nullius" / "SKILL.md",
    ROOT / "skills" / "nullius" / "references" / "GUIDE.md",
]

BLOCK = re.compile(r"```lean\n(.*?)```", re.DOTALL)
EXPECT = re.compile(r"^--\s*expect:\s*(\S+)\s*$", re.MULTILINE)
HEREDOC = re.compile(r"<<'EOF'\n(.*?)\nEOF", re.DOTALL)
VALID = {"verified", "verified-nontrivial", "rejected", "rejected-nontrivial"}

Case = tuple[str, int, str, str]  # (doc name, line, expectation, source)


def cookbook_cases() -> list[Case]:
    text = COOKBOOK.read_text()
    out: list[Case] = []
    for m in BLOCK.finditer(text):
        body = m.group(1)
        line = text[: m.start()].count("\n") + 1
        exp = EXPECT.search(body)
        if not exp:
            raise SystemExit(f"{COOKBOOK.name}:{line}: lean block has no `-- expect:` line")
        if exp.group(1) not in VALID:
            raise SystemExit(f"{COOKBOOK.name}:{line}: unknown expectation {exp.group(1)!r}")
        _check_narrative(text, m.end(), exp.group(1), line)
        out.append((COOKBOOK.name, line, exp.group(1), EXPECT.sub("", body).strip() + "\n"))
    return out


def _check_narrative(text: str, block_end: int, expect: str, line: int) -> None:
    """Reject prose that contradicts the verdict its block is checked against.

    Written for a document that reads as a session: the Lean is checked, but the sentences
    around it — the quoted verdicts, the interpretation — are not, and a reader believes
    those just as readily. This does not verify the narrative, which would mean parsing
    English; it only catches the one inconsistency that matters, where the text announces the
    opposite of what the block is required to do.
    """
    # Only the prose interpreting *this* block: up to the next block or heading.
    rest = text[block_end:]
    stop = min(
        (i for i in (rest.find("\n## "), rest.find("```lean")) if i != -1),
        default=len(rest),
    )
    following = rest[:stop]
    want_ok = expect.startswith("verified")
    if want_ok and "**REJECTED" in following:
        raise SystemExit(
            f"{COOKBOOK.name}:{line}: block is expected to be {expect}, but the text after it "
            "announces REJECTED"
        )
    if not want_ok and "**VERIFIED" in following:
        raise SystemExit(
            f"{COOKBOOK.name}:{line}: block is expected to be {expect}, but the text after it "
            "announces VERIFIED"
        )


def heredoc_cases() -> list[Case]:
    """Proofs shown in shell examples. Being shown as working, they must verify."""
    out: list[Case] = []
    for doc in HEREDOC_DOCS:
        text = doc.read_text()
        for m in HEREDOC.finditer(text):
            line = text[: m.start()].count("\n") + 1
            out.append((doc.name, line, "verified", m.group(1).strip() + "\n"))
    return out


def main() -> int:
    cases = cookbook_cases() + heredoc_cases()
    print(f"{len(cases)} documented example(s)\n")

    session = Session()
    try:
        session.start()
    except ConfigError as exc:
        # Reached from a commit hook on a machine where the verifier is not built. Say so
        # plainly rather than failing with a traceback about a missing binary.
        print(f"cannot check the examples: {exc}")
        return 1
    vf = Verifier(session)
    failures: list[str] = []

    for doc, line, expect, src in cases:
        nontrivial = expect.endswith("-nontrivial")
        want_ok = expect.startswith("verified")
        v = vf.verify(src, require_nontrivial=nontrivial)
        ok = v.verified == want_ok
        caught = [c.name for c in v.checks if not c.passed]
        name = v.target or "?"
        print(
            f"  {'ok  ' if ok else 'FAIL'}  {doc}:{line:<4} {expect:20s} "
            f"{name:32s} {v.status}"
            + (f" caught_by={caught}" if caught else "")
        )
        if not ok:
            failures.append(f"{doc}:{line} expected {expect}, got {v.status}\n{v.render()}")

    session.close()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("Every documented example still behaves as documented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

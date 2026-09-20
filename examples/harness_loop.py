"""A minimal agent loop: propose Lean, verify, repair, report.

`propose` here is a canned list standing in for a model call. Swap it for your generation
function and the rest of the loop is unchanged.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from action_speaks import Harness  # noqa: E402

CLAIM = "for every natural n, n - 1 is less than n"

# Round 0 is what a model typically writes: true over the integers, but FALSE for `ℕ`,
# where subtraction truncates and 0 - 1 = 0. Round 1 is the repair.
DRAFTS = [
    "theorem nat_pred_lt (n : ℕ) : n - 1 < n := by omega",
    "theorem nat_pred_lt (n : ℕ) (hn : 0 < n) : n - 1 < n := by omega",
]


def propose(feedback: str | None, round_no: int) -> str:
    if feedback is not None:
        print(f"  round {round_no}: repairing after ->")
        for line in feedback.splitlines():
            print(f"      {line}")
    return DRAFTS[min(round_no, len(DRAFTS) - 1)]


def main() -> int:
    with Harness(pool_size=1, tag="example").warm() as h:
        verdict, history = h.prove(propose, claim=CLAIM, max_rounds=3, require_nontrivial=True)

        print(f"\n{len(history)} round(s), final: {verdict.status}")
        if verdict.verified:
            print("\nClaim, as stated informally:")
            print(f"  {CLAIM}")
            print("Claim, as Lean actually checked it:")
            print(f"  {verdict.statement}")
            print(f"Axioms: {', '.join(verdict.axioms)}")
            print(
                f"Checked against {verdict.provenance['toolchain']}, "
                f"mathlib {verdict.provenance['mathlib_rev'][:12]}"
            )
            print(
                "\nNote the difference between the two statements above: the informal "
                "claim was FALSE\nfor n = 0, and the verifier is what forced the "
                "hypothesis `0 < n` to appear."
            )
        else:
            print(verdict.feedback())
        return 0 if verdict.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())

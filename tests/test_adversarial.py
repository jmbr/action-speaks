"""Adversarial tests: attacks that must be rejected even if the static guard is bypassed.

These are deliberately hostile submissions. Each one attempts to obtain a `verified` verdict
for a false or empty claim. The static guard would stop most of them on sight, so the first
pass here *disables the guard* in order to prove that the Lean-side audit is an independent
line of defence rather than a formality.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leanai import guard as G  # noqa: E402
from leanai import verify as V  # noqa: E402
from leanai.repl import Session  # noqa: E402

# The audit machinery emits `LEANAI_AUDIT {...}`; inside a Lean interpolated string the
# opening brace must be escaped as `\{`, hence the awkward literal below.
FORGED = (
    r'logInfo m!"LEANAI_AUDIT \{\"check\":\"axioms\",\"decl\":\"t\",\"axioms\":[],'
    r'\"untrusted\":[],\"uses_sorry\":false,\"trusted\":true}"'
)

ATTACKS: list[tuple[str, str, str]] = [
    (
        "command_override",
        # Redefine `#audit_axioms` so the verifier's own audit call reports success.
        f"""theorem t : 2 + 2 = 5 := by sorry

open Lean Elab Command in
syntax (name := fakeAudit) "#audit_axioms " ident : command

open Lean Elab Command in
@[command_elab fakeAudit]
def fakeElab : CommandElab := fun _ => do
  {FORGED}
""",
        "t",
    ),
    (
        "axiom_plus_override",
        f"""axiom evil : False
theorem t : 2 + 2 = 5 := absurd evil (by simp)

open Lean Elab Command in
syntax (name := fakeAudit2) "#audit_axioms " ident : command

open Lean Elab Command in
@[command_elab fakeAudit2]
def fakeElab2 : CommandElab := fun _ => do
  {FORGED}
""",
        "t",
    ),
    (
        "trusted_axioms_shadow",
        # Try to widen the trusted set the audit compares against.
        """namespace LeanAI.Audit
def trustedAxioms : List Lean.Name := [`propext, `Classical.choice, `Quot.sound, `evil]
end LeanAI.Audit
axiom evil : False
theorem t : 2 + 2 = 5 := absurd evil (by simp)
""",
        "t",
    ),
    (
        "sorry_in_helper",
        """theorem helper : 2 + 2 = 5 := by sorry
theorem t : 2 + 2 = 5 := helper
""",
        "t",
    ),
    (
        "native_decide",
        "theorem t : (List.range 500).length = 500 := by native_decide\n",
        "t",
    ),
    (
        "axiom_injection",
        "axiom evil : False\ntheorem t : 2 + 2 = 5 := absurd evil (by simp)\n",
        "t",
    ),
    (
        "vacuous_hypotheses",
        "theorem t (n : Nat) (h1 : n > 5) (h2 : n < 3) : n = 42 := by omega\n",
        "t",
    ),
    (
        "vacuous_real",
        "theorem t (x : ℝ) (h : x ^ 2 < 0) : x = 99 := by nlinarith [sq_nonneg x]\n",
        "t",
    ),
    (
        "exit_hiding",
        # `#exit` stops elaboration, so a later sorried theorem never appears.
        """theorem t : True := trivial
#exit
theorem t2 : 2 + 2 = 5 := by sorry
""",
        "t",
    ),
]

# `exit_hiding` proves a true (if useless) statement, so only the guard should stop it.
GUARD_ONLY = {"exit_hiding"}

GENUINE: list[tuple[str, str, str]] = [
    (
        "nlinarith",
        "theorem t (n : Nat) (h : n > 5) : n * n > 25 := by nlinarith\n",
        "t",
    ),
    (
        "finset_sum",
        "theorem t (s : Finset ℕ) (f : ℕ → ℕ) (h : ∀ i ∈ s, f i = 0) : ∑ i ∈ s, f i = 0 := "
        "Finset.sum_eq_zero h\n",
        "t",
    ),
    (
        "irrational_sqrt_two",
        "theorem t : Irrational (Real.sqrt 2) := by\n"
        "  simpa using (Nat.prime_two).irrational_sqrt\n",
        "t",
    ),
]


def main() -> int:
    session = Session()
    session.start()
    verifier = V.Verifier(session)
    print(f"session ready in {session.startup_seconds:.2f}s\n")

    real_guard = G.check
    failures: list[str] = []

    def bypass(src, extra=()):
        return G.GuardResult(ok=True, violations=[])

    print("=" * 74)
    print("ATTACKS, guard DISABLED - the Lean audit must reject these unaided")
    print("=" * 74)
    V.guard.check = bypass
    try:
        for name, src, target in ATTACKS:
            if name in GUARD_ONLY:
                continue
            v = verifier.verify(src, target=target, require_nontrivial=True)
            caught = [c.name for c in v.checks if not c.passed]
            ok = not v.verified
            print(f"  {'ok  ' if ok else 'LEAK'}  {name:22s} {v.status:9s} caught_by={caught}")
            if not ok:
                failures.append(f"attack {name} was VERIFIED with guard disabled")
    finally:
        V.guard.check = real_guard

    print()
    print("=" * 74)
    print("ATTACKS, guard ENABLED - caught earlier and more cheaply")
    print("=" * 74)
    for name, src, target in ATTACKS:
        v = verifier.verify(src, target=target, require_nontrivial=True)
        caught = [c.name for c in v.checks if not c.passed]
        ok = not v.verified
        print(f"  {'ok  ' if ok else 'LEAK'}  {name:22s} {v.status:9s} caught_by={caught}")
        if not ok:
            failures.append(f"attack {name} was VERIFIED with guard enabled")

    print()
    print("=" * 74)
    print("GENUINE PROOFS - must be accepted; a verifier that rejects everything is useless")
    print("=" * 74)
    for name, src, target in GENUINE:
        v = verifier.verify(src, target=target)
        ok = v.verified
        print(
            f"  {'ok  ' if ok else 'FAIL'}  {name:22s} {v.status:9s} "
            f"axioms={v.axioms} {v.elapsed:.2f}s"
        )
        if not ok:
            failures.append(f"genuine proof {name} rejected:\n{v.render()}")

    session.close()

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("All checks behaved correctly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

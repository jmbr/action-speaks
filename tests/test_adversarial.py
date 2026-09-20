"""Adversarial tests: attacks that must be rejected even if the static guard is bypassed.

These are deliberately hostile submissions. Each one attempts to obtain a `verified` verdict
for a false or empty claim. The static guard would stop most of them on sight, so the first
pass here *disables the guard* in order to prove that the Lean-side audit is an independent
line of defense rather than a formality.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.lean

from action_speaks import guard as G  # noqa: E402
from action_speaks import verify as V  # noqa: E402
from action_speaks.config import ConfigError  # noqa: E402
from action_speaks.repl import Session  # noqa: E402

# The audit machinery emits `ACTION_SPEAKS_AUDIT {...}`; inside a Lean interpolated string the
# opening brace must be escaped as `\{`, hence the awkward literal below.
FORGED = (
    r'logInfo m!"ACTION_SPEAKS_AUDIT \{\"check\":\"axioms\",\"decl\":\"t\",\"axioms\":[],'
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
        """namespace ActionSpeaks.Audit
def trustedAxioms : List Lean.Name := [`propext, `Classical.choice, `Quot.sound, `evil]
end ActionSpeaks.Audit
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
    (
        "forged_environment",
        # The attack that defeats the axiom audit outright. `addDeclCore` with `doCheck :=
        # false` installs `forged : 4 = 5` without the kernel ever seeing it, and
        # `#print axioms forged` then reports "does not depend on any axioms". Only replaying
        # the declaration through the kernel exposes it.
        """theorem decoy : True := trivial
open Lean Elab Command in
run_cmd do
  let env <- getEnv
  let bogusType := mkApp3 (mkConst ``Eq [Level.one]) (mkConst ``Nat) (mkNatLit 4) (mkNatLit 5)
  let decl := Declaration.thmDecl
    { name := `forged, levelParams := [], type := bogusType, value := mkConst ``trivial }
  match env.addDeclCore 0 0 decl none false with
  | .ok env' => modifyEnv fun _ => env'
  | .error _ => logInfo "rejected"
""",
        "forged",
    ),
    (
        # Physlib ships deliberately incomplete results, marked `@[sorryful]`. Nothing in
        # the *submission* looks suspicious - there is no `sorry` token for the static guard
        # to find, and the proof really is `rfl` - so this is caught only by walking the
        # axiom footprint through the cited constant.
        "physlib_sorryful_citation",
        "theorem t :\n"
        "    ClassicalMechanics.CoplanarDoublePendulum.ConfigurationSpace =\n"
        "      ClassicalMechanics.CoplanarDoublePendulum.ConfigurationSpace := rfl\n",
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
    (
        # Regression: the audit aliases the target with `def alias := @target`. Without the
        # `@`, Lean inserts metavariables for these leading implicit and instance-implicit
        # binders, instance resolution gets stuck, and the alias is never defined - so this
        # entirely ordinary polymorphic theorem came back as `target_declared` FAILED.
        "implicit_binders",
        "theorem t {E : Type*} [NormedAddCommGroup E] [NormedSpace ℝ E] (x y : E) :\n"
        "    ‖x + y‖ ≤ ‖x‖ + ‖y‖ := norm_add_le x y\n",
        "t",
    ),
    (
        # Physlib's complete results must remain usable, or importing it buys nothing.
        "physlib_theorem",
        "open ClassicalMechanics ClassicalMechanics.FreeParticle in\n"
        "theorem t (s : FreeParticle) (q : Trajectory)\n"
        "    (h : ∀ u, s.NewtonsSecondLaw q u) (hcont : ContDiff ℝ 2 q) :\n"
        "    ∃ p, ∀ u, s.linearMomentum q u = p :=\n"
        "  ClassicalMechanics.FreeParticle.linearMomentum_conserved s q h hcont\n",
        "t",
    ),
]


@pytest.fixture(scope="module")
def verifier() -> Iterator[V.Verifier]:
    session = Session()
    try:
        session.start()
    except ConfigError as exc:
        # Reached from a commit hook on a machine where the verifier is not built.
        pytest.fail(f"cannot run the suite: {exc}")
    yield V.Verifier(session)
    session.close()


@pytest.fixture
def guard_disabled() -> Iterator[None]:
    """Prove the Lean audit rejects an attack unaided, with the source guard out of the way."""
    real = V.guard.check
    V.guard.check = lambda src, extra=(): G.GuardResult(ok=True, violations=[])
    try:
        yield
    finally:
        V.guard.check = real


UNAIDED = [case for case in ATTACKS if case[0] not in GUARD_ONLY]


@pytest.mark.parametrize(("name", "source", "target"), UNAIDED, ids=[c[0] for c in UNAIDED])
def test_attack_is_rejected_with_the_guard_disabled(
    verifier: V.Verifier, guard_disabled: None, name: str, source: str, target: str
) -> None:
    verdict = verifier.verify(source, target=target, require_nontrivial=True)
    caught = [check.name for check in verdict.checks if not check.passed]
    assert not verdict.verified, f"attack {name} was VERIFIED with the guard disabled {caught}"


@pytest.mark.parametrize(("name", "source", "target"), ATTACKS, ids=[c[0] for c in ATTACKS])
def test_attack_is_rejected_with_the_guard_enabled(
    verifier: V.Verifier, name: str, source: str, target: str
) -> None:
    verdict = verifier.verify(source, target=target, require_nontrivial=True)
    caught = [check.name for check in verdict.checks if not check.passed]
    assert not verdict.verified, f"attack {name} was VERIFIED with the guard enabled {caught}"


@pytest.mark.parametrize(("name", "source", "target"), GENUINE, ids=[c[0] for c in GENUINE])
def test_genuine_proof_is_accepted(
    verifier: V.Verifier, name: str, source: str, target: str
) -> None:
    """A verifier that rejects everything is useless, so the honest cases matter as much."""
    verdict = verifier.verify(source, target=target)
    assert verdict.verified, f"genuine proof {name} rejected:\n{verdict.render()}"

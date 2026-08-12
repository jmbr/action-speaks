# Reference: verifying claims with Lean

Loaded on demand. `SKILL.md` has the workflow; this has the details.

## Failure catalogue

What each verdict line means and what to do about it.

| Check | Meaning | What to do |
|---|---|---|
| `static_guard` | banned construct in the source | remove it; these are never negotiable |
| `elaboration` | the proof does not compile | read the Lean error; use `goal` to find the lemma |
| `no_sorry` | proof incomplete, anywhere in the file | finish it, or admit you cannot prove it |
| `target_declared` | the named theorem does not exist | name the theorem you want audited, put it last |
| `audit_integrity` | the submission interfered with the verifier | submit a plain proof |
| `trusted_axioms` | rests on something beyond `propext`, `Classical.choice`, `Quot.sound` | remove the axiom / `native_decide` |
| `statement_sorry_free` | `sorry` inside the statement itself | write the statement out fully |
| `not_vacuous` | hypotheses contradict each other | **fix the statement, not the proof** |
| `hypotheses_used` | conclusion holds without the hypotheses | strengthen the conclusion, or drop the hypotheses and claim the stronger result |

## The two repairs that are not obvious

### Vacuous: fix the statement, not the proof

```
[FAIL] not_vacuous - hypotheses are contradictory, so the theorem is vacuously true
```

The instinct is to change the proof. That is wrong — the proof is fine and Lean was right to
accept it. The *statement* is the problem: the hypotheses cannot all hold, so the theorem
says nothing.

```lean
-- rejected: no ℕ is both > 5 and < 3
theorem bad (n : ℕ) (h₁ : n > 5) (h₂ : n < 3) : n = 42 := by omega
```

Work out what you actually meant, and restate it.

### `ℕ` truncation: the most common false "obvious" claim

```lean
-- rejected: FALSE at n = 0, because 0 - 1 = 0 in ℕ
theorem bad (n : ℕ) : n - 1 < n := by omega

-- correct
theorem good (n : ℕ) (hn : 0 < n) : n - 1 < n := by omega
```

`ℕ` subtraction truncates at zero and `ℕ` division floors. If a claim involves subtraction,
division, or "the difference between", either add the hypothesis that makes it well-behaved
or state it over `ℤ`/`ℝ`.

## Finding lemmas

Three routes, in increasing order of reliability and cost:

```bash
scripts/nullius search 'every continuous function on a compact set attains its maximum'
scripts/nullius search '|- Continuous (fun _ => _)'          # Loogle pattern
scripts/nullius search 'Real.sqrt, |- _ ≤ _'                 # Loogle conjunction
scripts/nullius close 'Irrational (Real.sqrt 2)'              # ask Lean; authoritative
scripts/nullius close '0 ≤ x^2' -b '(x : ℝ)'
```

Loogle patterns: `?a` is a named wildcard, `_` an anonymous one, `|-` restricts the match to
the conclusion, commas conjoin constraints, `"foo"` matches names containing `foo`.

`goal` runs `exact?`/`apply?` inside Lean. Slower (seconds), but whatever it returns actually
closes the goal — it cannot invent a name. When search and `goal` disagree, trust `goal`.

## Worked example: claim to verdict

Claim: *the arithmetic mean of two nonnegative reals is at least their geometric mean.*

```bash
scripts/nullius statement '(a b : ℝ) (ha : 0 ≤ a) (hb : 0 ≤ b) : Real.sqrt (a * b) ≤ (a + b) / 2'
# → ∀ (a b : ℝ), 0 ≤ a → 0 ≤ b → √(a * b) ≤ (a + b) / 2 ; hypotheses satisfiable

scripts/nullius check "AM-GM for two nonnegative reals" <<'EOF'
theorem am_gm_two (a b : ℝ) (ha : 0 ≤ a) (hb : 0 ≤ b) :
    Real.sqrt (a * b) ≤ (a + b) / 2 := by
  rw [show a * b = ((a+b)/2)^2 - ((a-b)/2)^2 by ring]
  calc Real.sqrt (((a+b)/2)^2 - ((a-b)/2)^2) ≤ Real.sqrt (((a+b)/2)^2) := by
        apply Real.sqrt_le_sqrt; nlinarith [sq_nonneg ((a-b)/2)]
    _ = |(a+b)/2| := Real.sqrt_sq_eq_abs _
    _ = (a+b)/2 := abs_of_nonneg (by linarith)
EOF
```

Then report: *"Machine-checked in Lean 4 / Mathlib: `∀ (a b : ℝ), 0 ≤ a → 0 ≤ b →
√(a * b) ≤ (a + b) / 2`."* Quote the elaborated statement, not a paraphrase.

## Refuting a claim

To refute something, prove its negation — equally checkable:

```bash
scripts/nullius check "not every continuous function is differentiable" <<'EOF'
theorem not_all_cont_diff : ¬ (∀ f : ℝ → ℝ, Continuous f → Differentiable ℝ f) := by
  intro h
  have : Differentiable ℝ (fun x : ℝ => |x|) := h _ continuous_abs
  simpa using (this 0).differentiableAt
EOF
```

If you can prove neither the claim nor its negation, say exactly that. "I could not settle
this" is a legitimate and useful answer; a fabricated proof is not.

## Repeated or batch use

Each CLI invocation starts its own Lean session (~2.5 s to import Mathlib). For more than a
handful of checks, drive the Python API, which keeps sessions warm:

```python
import sys; sys.path.insert(0, "<verifier repo>")   # the directory containing nullius/
from nullius import Harness

with Harness(pool_size=4).warm() as h:
    verdicts = h.verify_many([{"source": s, "claim": c} for s, c in items])
```

A warm pool verifies in 10–200 ms per claim, and `h.prove(propose)` runs a repair loop that
feeds each failure back to the model. See `INTEGRATION.md` in the verifier repo.

## Setup and diagnosis

```bash
scripts/nullius doctor    # provenance, startup time, three sanity checks
scripts/nullius log       # what has been verified so far
```

`doctor` should end with "verifier is discriminating correctly", meaning it accepted a
genuine proof and rejected both a `sorry` and a vacuous theorem. If it fails, the verifier
needs rebuilding — see `README.md` in the verifier repo. The wrapper finds the repository by
resolving its own symlink; set `NULLIUS_ROOT` to override.

## Provenance

Every verdict records the toolchain and the Mathlib and Physlib revisions, and each check is
appended to a SQLite ledger. When a verification matters, cite it:

```
Lean 4.32.0, Mathlib 81a5d257c8e4, Physlib cf1d86d1fbba,
axioms: propext, Classical.choice, Quot.sound
```

A proof that verifies against one Mathlib revision may not even elaborate against another,
so the revision is part of the claim.

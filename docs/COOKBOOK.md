# Cookbook: checking applied mathematics

Start with a specific claim, not an entire paper. Good candidates include a key inequality,
an error estimate, a suspicious set of assumptions, or an expression involving integer
indices. The examples below also cover library citations and computer-generated proofs.

`tests/test_docs.py` checks every Lean block below against the installed libraries.
Each `-- expect:` line specifies the expected result. Some examples intentionally fail.

## Two interfaces, same verifier

Examples use the installed CLI. Agents with MCP support can use tools with the same names;
other agents can use the shell wrapper supplied by the skill.

| Operation                               | CLI                 | MCP tool    |
|-----------------------------------------|---------------------|-------------|
| Check a proof                           | `action-speaks verify`    | `verify`    |
| Check types and look for contradictory assumptions | `action-speaks statement` | `statement` |
| Find a lemma by meaning or shape        | `action-speaks search`    | `search`    |
| Find what closes a goal                 | `action-speaks close`     | `close`     |
| Look up past verdicts                   | `action-speaks log`       | `log`       |

For `verify`, `--require-nontrivial` maps to `require_nontrivial`, `--tag` to `tag`,
`-t` to `target`, and `-c` to `claim`. For `close`, `-b` maps to `binders`. For example:

```bash
action-speaks verify bound.lean -c "energy estimate, eq. (3.7)" --tag paper-draft --require-nontrivial
```

The corresponding MCP `verify` arguments are:

```json
{"source": "theorem …", "claim": "energy estimate, eq. (3.7)",
 "tag": "paper-draft", "require_nontrivial": true}
```

Both interfaces write to the configured ledger. The skill wrapper has different argument
syntax; see [agent setup](SETUP-AGENTS.md#shell-wrapper-syntax).

## The shape of a useful check

You can check a consequence without formalizing all its prerequisites. State those
prerequisites as explicit assumptions, justify them separately, and prove what follows.

For example, this theorem bounds a sequence that satisfies an error recurrence:

```lean
-- expect: verified-nontrivial
theorem error_recursion_accumulates (q d : ℝ) (e : ℕ → ℝ) (hq : 0 ≤ q)
    (hrec : ∀ n, e (n + 1) ≤ q * e n + d) (n : ℕ) :
    e n ≤ q ^ n * e 0 + d * ∑ i ∈ Finset.range n, q ^ i := by
  induction n with
  | zero => simp
  | succ n ih =>
    have hstep : q * e n ≤ q * (q ^ n * e 0 + d * ∑ i ∈ Finset.range n, q ^ i) :=
      mul_le_mul_of_nonneg_left ih hq
    have := hrec n
    rw [geom_sum_succ, pow_succ]
    ring_nf
    ring_nf at hstep this
    linarith
```

This checks how the recurrence accumulates. It does not show that a numerical method
satisfies the recurrence: `e` is an arbitrary sequence, with no differential equation in
the statement.

For an application, justify `hrec` separately. Do not assume the conclusion in another
form. Also review whether the assumptions can hold together.

## Check assumptions before building on them

Run `action-speaks statement` before attempting a proof. It checks types and tries to find
contradictory assumptions. Applied arguments often combine parameter ranges and step-size
conditions that need to be checked together.

For example, the following proof derives a contradiction from `0 < q < 1` and
`1/(1-q) ≤ q`:

```lean
-- expect: verified
theorem contraction_conditions_empty (q : ℝ) (hq : 0 < q) (hq1 : q < 1)
    (hstep : 1 / (1 - q) ≤ q) : False := by
  rw [div_le_iff₀ (by linarith)] at hstep
  nlinarith [sq_nonneg (q - 1 / 2)]
```

The automatic contradiction probe misses this example. The explicit proof rewrites the
division and gives `nlinarith` the additional fact `sq_nonneg (q - 1/2)`.
A clean probe result therefore does not establish consistency. If assumptions look
suspicious, try a separate proof of `False` or construct an example satisfying them.

## Check key inequalities

Formalize the inequalities on which the argument depends. For an energy estimate, this
checks the factors and the assumption on `ε`:

```lean
-- expect: verified-nontrivial
theorem young_with_epsilon (a b ε : ℝ) (hε : 0 < ε) :
    a * b ≤ ε * a ^ 2 / 2 + b ^ 2 / (2 * ε) := by
  rw [div_add_div _ _ (by norm_num) (by positivity), le_div_iff₀ (by positivity)]
  nlinarith [sq_nonneg (ε * a - b), sq_nonneg (ε * a + b), hε.le]
```

To check both directions of a threshold, use `↔`. This example checks the multiplier bound
used for explicit Euler on `y' = -Ly`:

```lean
-- expect: verified-nontrivial
theorem euler_stability_iff (L h : ℝ) (hL : 0 < L) (hh : 0 < h) :
    |1 - h * L| ≤ 1 ↔ h ≤ 2 / L := by
  rw [abs_le, le_div_iff₀ hL]
  constructor
  · rintro ⟨h1, h2⟩; nlinarith
  · intro h1; constructor <;> nlinarith
```

This next example bounds a repeated growth factor by an exponential:

```lean
-- expect: verified-nontrivial
theorem euler_growth_le_exp (h L : ℝ) (n : ℕ) (hh : 0 ≤ h) (hL : 0 ≤ L) :
    (1 + h * L) ^ n ≤ Real.exp (n * h * L) := by
  have h1 : 1 + h * L ≤ Real.exp (h * L) := by
    have := Real.add_one_le_exp (h * L); linarith
  calc (1 + h * L) ^ n ≤ (Real.exp (h * L)) ^ n :=
        pow_le_pow_left₀ (by positivity) h1 n
    _ = Real.exp (n * h * L) := by
        rw [← Real.exp_nat_mul]; ring_nf
```

These proofs check the displayed inequalities, not the surrounding numerical analysis.

## Choose the right arithmetic for indices

On `ℕ`, subtraction stops at zero and division discards the remainder. For instance,
dividing a grid size into two equal integer parts need not preserve the total:

```lean
-- expect: verified
theorem nat_halving_loses_a_point : ∃ n : ℕ, n / 2 + n / 2 ≠ n := ⟨3, by decide⟩
```

Use `ℕ` for counts when these operations are intended. Use `ℤ` for signed differences or
`ℝ` for real-valued ratios.

## Check physics citations for unfinished proofs

Physlib is preloaded. Search for its existing results and the assumptions they require:

```
action-speaks search 'ClassicalMechanics.HarmonicOscillator.ω, |- _ = _' --backend loogle
  → ω_sq : S.ω ^ 2 = S.k / S.m
action-speaks search 'ClassicalMechanics.HarmonicOscillator.m, |- 0 < _' --backend loogle
  → m_pos : 0 < self.m
```

The second query asks for a result mentioning the mass whose conclusion is a positivity
bound. The proof uses that bound to justify division:

```lean
-- expect: verified-nontrivial
open ClassicalMechanics in
/-- The stiffness of a harmonic oscillator is its mass times its angular frequency squared. -/
theorem k_eq_m_mul_omega_sq (S : HarmonicOscillator) : S.k = S.m * S.ω ^ 2 := by
  have hm : S.m ≠ 0 := ne_of_gt S.m_pos
  rw [S.ω_sq]
  field_simp
```

Physlib also includes unfinished results marked `@[sorryful]` or `@[pseudo]`.
A citation can look complete while depending on one of them:

```lean
-- expect: rejected
theorem uses_placeholder :
    ClassicalMechanics.CoplanarDoublePendulum.ConfigurationSpace =
      ClassicalMechanics.CoplanarDoublePendulum.ConfigurationSpace := rfl
```

The source contains no `sorry`, but the axiom check follows its dependencies and reports:

```
[FAIL] trusted_axioms - untrusted: sorryAx
  axioms: sorryAx
```

`sorryAx` marks an unfinished proof dependency. Use a completed result, prove the missing
step, or report the limitation.

## Include the assumptions a computation theorem needs

Cslib covers computation topics such as automata, type systems, and rewriting.
This attempt to use Newman's lemma omits its termination assumption:

```lean
-- expect: rejected
theorem locally_confluent_suffices {α : Type} (r : α → α → Prop)
    (hlc : Relation.LocallyConfluent r) : Relation.Confluent r :=
  hlc.Terminating_toConfluent
```

Lean reports that the supplied term still needs a termination argument:

```
has type
  Relation.Terminating r → Relation.Confluent r
but is expected to have type
  ... Relation.Join (Relation.ReflTransGen r) b c
```

Supply the missing assumption:

```lean
-- expect: verified-nontrivial
theorem newman {α : Type} (r : α → α → Prop)
    (hlc : Relation.LocallyConfluent r) (ht : Relation.Terminating r) :
    Relation.Confluent r :=
  hlc.Terminating_toConfluent ht
```

This proves confluence under the displayed local-confluence and termination assumptions.
The `require_nontrivial` probe also passes, but that check does not establish the necessity
of each assumption separately.

## Use a computer algebra system to find a proof certificate

A *certificate* is data from which Lean can reconstruct a proof. The optional
[`polyrith'` contribution](../contrib/polyrith-local/README.md) asks Singular for polynomial
coefficients, then suggests a `linear_combination` proof. It is separate from the verifier.

Here is a certificate for a polynomial model of a two-link arm. The variables `c1`, `s1`,
`c2`, and `s2` satisfy the displayed identities; the theorem does not define them as
trigonometric functions:

```lean
-- expect: verified-nontrivial
/-- Two-link planar arm: the squared reach depends only on the elbow angle. -/
theorem arm_reach (x y L1 L2 c1 s1 c2 s2 : ℚ)
    (hx : x = L1*c1 + L2*(c1*c2 - s1*s2))
    (hy : y = L1*s1 + L2*(s1*c2 + c1*s2))
    (p1 : c1^2 + s1^2 = 1)
    (p2 : c2^2 + s2^2 = 1) :
    x^2 + y^2 = L1^2 + L2^2 + 2*L1*L2*c2 := by
  linear_combination
    (L2 * c2 * c1 + x * c1 ^ 2 + x * s1 ^ 2 - 1 * L2 * s1 * s2 + L1 * c1) * hx +
          (L2 * c2 * s1 + L2 * c1 * s2 + L1 * s1 + y) * hy +
        (x * L2 * c2 * c1 - 1 * x * L2 * s1 * s2 + 2 * L1 * L2 * c2 + x * L1 * c1 - 1 * x ^ 2
          + L1 ^ 2 + L2 ^ 2) * p1 +
      (L2 ^ 2 * c1 ^ 2 + L2 ^ 2 * s1 ^ 2) * p2
```

This proof passes with `require_nontrivial`. It establishes the displayed identity over
`ℚ`, not a floating-point error bound or a theorem about a physical controller.

Singular supplies the coefficients; Lean checks the resulting arithmetic. The submitted
certificate can be checked without running Singular again.

## Look up lemma names

An otherwise plausible proof can fail because a lemma name is wrong:

```lean
-- expect: rejected
theorem euler_growth_guessed (h L : ℝ) (n : ℕ) (hh : 0 ≤ h) (hL : 0 ≤ L) :
    (1 + h * L) ^ n ≤ Real.exp (n * h * L) := by
  have h1 : 1 + h * L ≤ Real.exp (h * L) := by
    have := Real.add_one_le_exp (h * L); linarith
  calc (1 + h * L) ^ n ≤ (Real.exp (h * L)) ^ n := by
        apply pow_le_pow_left (by positivity) h1
    _ = Real.exp (n * h * L) := by
        rw [← Real.exp_nat_mul]; ring_nf
```

Ask Lean for a suitable lemma:

```
action-speaks close 'a ^ n ≤ b ^ n' -b '(a b : ℝ) (n : ℕ) (ha : 0 ≤ a) (hab : a ≤ b)'
```

The suggestion is `exact pow_le_pow_left₀ ha hab n`, with a `₀` suffix.
`close` asks the installed Lean environment. `search` can also find lemmas by shape through
local Loogle or by meaning through remote LeanSearch. Verify any resulting proof.

## Check for unnecessary assumptions

`--require-nontrivial` rejects a proof if a probe establishes the conclusion after removing
propositional assumptions. This can reveal that the statement is weaker than intended:

```lean
-- expect: rejected-nontrivial
theorem positivity_unused (x : ℝ) (hx : 0 < x) : 0 ≤ x ^ 2 := sq_nonneg x
```

Here the cited lemma does not need `hx`. Either remove the assumption or revise the
conclusion to express the intended claim. The probe can miss unnecessary assumptions;
passing it is not a proof that each assumption is needed.

## Scope of the result

Lean checks the formal statement, not its translation from English or its suitability as
a model. Compare the returned statement with the claim, and account separately for physical
assumptions, discretization, and floating-point arithmetic.

Results also depend on library versions. Keep the recorded toolchain and revisions with
the proof so it can be reproduced.

## Group checks for a paper

Use a tag to collect related attempts:

```bash
action-speaks verify bound.lean -c "energy estimate, eq. (3.7)" --tag paper-draft --require-nontrivial
action-speaks log --tag paper-draft
```

From an agent, the same two steps are `verify` with `tag: "paper-draft"` and
`log` with `tag: "paper-draft"`.

The ledger keeps both successes and rejections. Review failed checks before retrying, and
re-verify stored proofs before citing them in a new environment.

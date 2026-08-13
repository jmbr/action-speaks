# Cookbook — nullius on applied mathematics

Full formalisation of an applied paper is rarely worth the effort. The wins are narrower and
much cheaper than that, and they cluster in a few places: the load-bearing inequality you
derived by hand, the hypothesis set you never checked was non-empty, and the `ℕ` index
expression that does not mean what it reads — and, if you work with Physlib, the physics
result you built on that turns out to be a placeholder.

Every Lean block below is verified on each run of `tests/test_docs.py`, against the
toolchain and revisions this repository pins. The `-- expect:` line says what the verifier
must return; blocks marked `rejected` are there because failing usefully is half the point.

## Two interfaces, same verifier

This document uses shell commands (`nullius verify`, `nullius close`, …). Inside an agent —
Copilot, pi, Claude Code — the same operations arrive as MCP tools, where the shell is not
available. **The names are identical**, so every recipe here transfers directly:

| Operation | CLI | MCP tool |
|---|---|---|
| Check a proof | `nullius verify` | `verify` |
| Elaborate a statement, test for vacuity | `nullius statement` | `statement` |
| Find a lemma by meaning or shape | `nullius search` | `search` |
| Find what closes a goal | `nullius close` | `close` |
| Look up past verdicts | `nullius log` | `log` |

Flags map to arguments of the same name: `--require-nontrivial` is `require_nontrivial`,
`--tag` is `tag`, `-t` is `target`, `-c` is `claim`, `-b` is `binders`. So

```bash
nullius verify "energy estimate, eq. (3.7)" --tag paper-draft --require-nontrivial < bound.lean
```

is, from an agent:

```json
{"source": "theorem …", "claim": "energy estimate, eq. (3.7)",
 "tag": "paper-draft", "require_nontrivial": true}
```

(`nullius check` is accepted as an alias for `verify`, for muscle memory.) Both interfaces
write to the same ledger, so a result checked from the shell is visible to the agent and the
other way round.

## The shape of a useful check

The pattern that makes this affordable is **assume the analysis**: the hard analytic facts
become *hypotheses*, and what gets verified is only what follows from them.

Here is the global error bound for a one-step method:

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

The hypothesis `e (n+1) ≤ q * e n + d` *is* the analysis. Establishing it for an actual
scheme needs a Lipschitz condition, a Taylor remainder, and existence and smoothness of the
exact solution — none of which appear here. `e` is an arbitrary sequence; the theorem knows
nothing about differential equations. What is checked is the other half: that this recursion
accumulates to that closed form, with the powers and the geometric sum in the right places
and nothing dropped.

That is the half where mistakes actually happen. The Lipschitz estimate is usually the part
you can check by hand, or cite.

**The trust boundary moves; it does not vanish.** You have reduced "is my error bound right?"
to "does my scheme really satisfy that recursion?" — a smaller and sharper question. Be
honest with yourself about which hypotheses you are importing, because assuming the analysis
is also precisely how one fakes a result. Two rules keep it defensible:

1. Every hypothesis must be independently justifiable — a textbook theorem, or a bound
   verified separately. Never the conclusion in disguise.
2. Check the hypotheses can be satisfied at all. See the next section.

## Pattern: check a hypothesis set before building on it

The cheapest useful call is `statement` (`nullius statement`) on a lemma you are
*about* to assume. It elaborates without proving and reports whether the hypotheses are
contradictory. Applied work stacks conditions — a decay rate, a step-size restriction, a
parameter range — and it is easy to write down a set that is quietly empty. A theorem with
empty hypotheses is vacuously true and supports nothing, while looking entirely respectable.

Suppose a convergence argument assumes a contraction factor `0 < q < 1` together with a step
condition `1/(1-q) ≤ q`. That is empty: the condition says `q(1-q) ≥ 1`, but `q(1-q)` peaks at
`1/4`. Any theorem carrying those hypotheses is worthless.

```lean
-- expect: verified
theorem contraction_conditions_empty (q : ℝ) (hq : 0 < q) (hq1 : q < 1)
    (hstep : 1 / (1 - q) ≤ q) : False := by
  rw [div_le_iff₀ (by linarith)] at hstep
  nlinarith [sq_nonneg (q - 1 / 2)]
```

**Important caveat, learned the hard way.** On this example the automatic vacuity probe
reported *"hypotheses are satisfiable as far as the prover can tell"* — it missed it. The
probe runs a fixed battery of finishing tactics, so it is sound but incomplete: a reported
vacuity is always real, a clean report is evidence and not proof. When a condition looks
suspiciously tight, spend the extra minute trying to prove `False` from it yourself. Here
`nlinarith` needed to be handed the maximiser, `sq_nonneg (q - 1/2)`.

## Pattern: verify the load-bearing inequality, not the paper

Applied arguments rest on a handful of inequalities, often obtained after a page of algebra.
Formalise those alone. This one — Cauchy with `ε` — is the workhorse of energy estimates, and
exactly where a stray factor of two silently ruins a constant:

```lean
-- expect: verified-nontrivial
theorem young_with_epsilon (a b ε : ℝ) (hε : 0 < ε) :
    a * b ≤ ε * a ^ 2 / 2 + b ^ 2 / (2 * ε) := by
  rw [div_add_div _ _ (by norm_num) (by positivity), le_div_iff₀ (by positivity)]
  nlinarith [sq_nonneg (ε * a - b), sq_nonneg (ε * a + b), hε.le]
```

When a threshold is supposed to be sharp, state it as an equivalence — the `↔` is where the
constant lives. Explicit Euler on `y' = -Ly`:

```lean
-- expect: verified-nontrivial
theorem euler_stability_iff (L h : ℝ) (hL : 0 < L) (hh : 0 < h) :
    |1 - h * L| ≤ 1 ↔ h ≤ 2 / L := by
  rw [abs_le, le_div_iff₀ hL]
  constructor
  · rintro ⟨h1, h2⟩; nlinarith
  · intro h1; constructor <;> nlinarith
```

And the step that turns per-step growth into `e^{TL}` in every Euler error analysis:

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

Each is three to eight lines, because none of them formalises the surrounding theory.

## Pattern: distrust `ℕ` for anything with `-` or `/`

Natural subtraction truncates (`0 - 1 = 0`) and natural division floors. Applied work is full
of indices, grid sizes and step counts, so this bites often, and a "verified" `ℕ` statement
can quietly be a different claim from the one you read. Split a grid of odd size into two
halves and a point vanishes:

```lean
-- expect: verified
theorem nat_halving_loses_a_point : ∃ n : ℕ, n / 2 + n / 2 ≠ n := ⟨3, by decide⟩
```

Worth internalising that this is a *theorem*, not a quirk. Use `ℝ` or `ℤ` unless you
genuinely mean the truncating operation.

## Pattern: physics, and the placeholder trap

Physlib is imported alongside Mathlib, so a claim about a physical system is checkable the
same way an inequality is. The workflow is the same too — find the library's own lemmas
rather than restate them:

```
nullius search 'ClassicalMechanics.HarmonicOscillator.ω, |- _ = _'
  → ω_sq : S.ω ^ 2 = S.k / S.m
nullius search 'ClassicalMechanics.HarmonicOscillator.m, |- 0 < _'
  → m_pos : 0 < self.m
```

That second query is the shape worth learning: *a declaration mentioning this quantity whose
conclusion is a positivity*. It found the side condition the algebra needs, which is the part
one forgets. With both in hand the proof is three lines:

```lean
-- expect: verified-nontrivial
open ClassicalMechanics in
/-- The stiffness of a harmonic oscillator is its mass times its angular frequency squared. -/
theorem k_eq_m_mul_omega_sq (S : HarmonicOscillator) : S.k = S.m * S.ω ^ 2 := by
  have hm : S.m ≠ 0 := ne_of_gt S.m_pos
  rw [S.ω_sq]
  field_simp
```

**Now the trap, which is specific to physics and has no analogue in Mathlib.** Physlib ships
results that are deliberately unfinished — a statement with no proof behind it, marking work
nobody has done yet, attributed `@[sorryful]` or `@[pseudo]`. Building on one looks *exactly*
like building on a theorem:

```lean
-- expect: rejected
theorem uses_placeholder :
    ClassicalMechanics.CoplanarDoublePendulum.ConfigurationSpace =
      ClassicalMechanics.CoplanarDoublePendulum.ConfigurationSpace := rfl
```

There is no `sorry` in that submission, the proof really is `rfl`, and nothing in the source
looks wrong. It is caught only by asking what the theorem ultimately rests on:

```
[FAIL] trusted_axioms - untrusted: sorryAx
  axioms: sorryAx
```

`sorryAx` is Lean's marker for "this rests on something nobody has proved". A placeholder
cannot hide from that question, which is why the axiom footprint is checked rather than the
text. Against Mathlib that check is near-ceremonial — Mathlib has no such placeholders — and
against Physlib it does real work.

When it fires, the fix is not to your proof. The physics result you leaned on is not
available yet; say so.

## Pattern: do not guess lemma names

Inventing a plausible-sounding name is the most common way an attempt dies. This is a real
rejection, not a constructed one — the name is right in spirit and does not exist:

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

The fix is to ask instead of guess — `close`, at the shell or from an agent:

```
nullius close 'a ^ n ≤ b ^ n' -b '(a b : ℝ) (n : ℕ) (ha : 0 ≤ a) (hab : a ≤ b)'
```

which answers `exact pow_le_pow_left₀ ha hab n` — the `₀` suffix being exactly the kind of
detail nobody recalls correctly. `close` runs in the real environment, so it cannot
hallucinate: it reports only lemmas that genuinely close the goal. `search` is the
complement — by meaning or by shape, against remote indexes — and is worth using even if you
never formalise anything, to answer "does Mathlib already have my bound, and what is it
called?" Search may return a name that does not exist; `close` cannot.

## Pattern: make unused hypotheses fatal

`--require-nontrivial` rejects a proof whose hypotheses turn out to be unnecessary. That
sounds pedantic and is not: it catches the case where you *believe* a condition is doing
work, and it is not — which usually means the statement is weaker than the claim you intend
to make with it, or that you have stated the wrong thing.

```lean
-- expect: rejected-nontrivial
theorem positivity_unused (x : ℝ) (hx : 0 < x) : 0 ≤ x ^ 2 := sq_nonneg x
```

The conclusion holds for every real, so `hx` is decoration. Either drop it and claim the
stronger unconditional result, or strengthen the conclusion to something that needs it.

## What this does not do for you

- **It cannot check that your Lean says what your English said.** Lean checks the proof; the
  translation is yours. This is why every verdict prints the elaborated statement — read it,
  and confirm the quantifiers, the hypotheses and the types are the ones you meant.
- **It says nothing about modelling.** Whether your equations describe the physical system,
  whether your discretisation is stable in practice, whether floating point resembles `ℝ` —
  all outside.
- **Numerics formalise badly.** Interval arithmetic in Lean is painful and usually not worth
  your time.
- **A verdict is tied to a revision.** A proof that verifies against one Mathlib may not
  elaborate against the next. The ledger records the toolchain and revisions per row for
  exactly this reason; cite them alongside the result.

## A realistic workflow

While writing a paper, formalise the two or three inequalities you are least sure of, tag
them so they are easy to find again, and move on:

```bash
nullius verify "energy estimate, eq. (3.7)" --tag paper-draft --require-nontrivial < bound.lean
nullius log --tag paper-draft
```

From an agent, the same two steps are `verify` with `tag: "paper-draft"` and
`log` with `tag: "paper-draft"`.

The ledger keeps rejections as well as successes, so the history of a claim — including a
step that stopped verifying after a dependency bump — stays visible.

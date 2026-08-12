---
name: nullius
description: Verify a mathematical claim by proving it in Lean 4 with Mathlib and Physlib, so the claim is machine-checked rather than asserted. Use whenever stating a non-obvious mathematical fact - an inequality, identity, bound, closed form, convergence or termination argument, correctness property, or counterexample - especially in analysis, algebra, number theory, combinatorics, probability, algorithm correctness, or physics. Also use to check whether a conjecture is even consistent before trying to prove it, and to find the right Mathlib lemma name instead of guessing.
compatibility: Requires the nullius verifier (Lean 4.32.0 + Mathlib + Physlib, ~9GB built) installed via its install.sh. Linux/macOS with python3.
metadata:
  repository: nullius
---

# Backing mathematical claims with Lean

Use this when you are about to assert something mathematical that a careful reader would want
checked. Instead of asserting it, prove it in Lean and let the kernel decide.

## Why this exists

"It compiled" is not evidence. All of these compile with no errors:

```lean
theorem a : 2 + 2 = 5 := by sorry          -- incomplete
axiom evil : False                          -- smuggled assumption
theorem b : 2 + 2 = 5 := absurd evil (by simp)
theorem c (n : ℕ) (h₁ : n > 5) (h₂ : n < 3) : n = 42 := by omega   -- vacuous
```

The last is the dangerous one: it is a *genuine theorem*, but no such `n` exists, so it
supports no claim at all. Careless formalisation produces these constantly and they look like
success. The verifier rejects all four.

## The workflow

### 1. Write the claim in English first

You will need it to check that the Lean statement actually says the same thing.

### 2. Check the statement before proving it

```bash
~/.agents/skills/nullius/scripts/nullius statement '(n : ℕ) (h : 5 < n) : 25 < n * n'
```

(That path is a symlink into the verifier repository, so it works from any directory. The
examples below shorten it to `scripts/nullius`.)

This elaborates without proving. It reports whether the statement type-checks, shows what
Lean understood, and warns if the hypotheses are **contradictory** — in which case stop and
restate, because any proof would be vacuous.

### 3. Find lemmas; do not guess names

Invented lemma names are the most common cause of failed proofs.

```bash
scripts/nullius search 'sum of two even numbers is even'        # by meaning
scripts/nullius search '|- Irrational (Real.sqrt _)'            # by shape (Loogle)
scripts/nullius close '25 < n * n' -b '(n : ℕ) (h : 5 < n)'      # ask Lean directly
```

`close` cannot hallucinate: it only reports lemmas that genuinely close the goal. `search`
consults remote indexes and can hand back a name that does not exist; `close` cannot.

### 4. Verify the proof

```bash
scripts/nullius verify "if n > 5 then n squared exceeds 25" <<'EOF'
theorem main (n : ℕ) (h : 5 < n) : 25 < n * n := by nlinarith
EOF
```

Exit code 0 means verified, 1 means not. Add `--require-nontrivial` to also reject proofs
whose hypotheses turn out to be unnecessary.

### 5. Report honestly

- **Verified:** say it is machine-checked, and quote the **elaborated statement** the tool
  prints — not a looser paraphrase of it.
- **Not verified:** do not present the claim as proved. Say what failed, or say plainly that
  you could not prove it. Never dress up a failure.

## Writing the Lean

- **No `import` lines.** Mathlib and Physlib are already imported; an `import` is rejected.
- **Physlib ships incomplete results** marked `@[sorryful]` / `@[pseudo]`. Citing one gets
  rejected on its axiom footprint, which means that physics result is not proved yet.
- Helper lemmas first, the claim you care about **last** (that one is audited by default).
- Workhorse tactics: `nlinarith`, `linarith`, `omega`, `positivity`, `norm_num`, `field_simp`,
  `aesop`, `simp`, `decide`, `grind`.
- Prefer `ℝ`/`ℤ` when the claim is about ordinary arithmetic. **`ℕ` subtraction truncates**
  (`0 - 1 = 0`) and `ℕ` division floors — a large share of "obvious" claims are false in `ℕ`
  for exactly this reason.

## What gets rejected

`sorry` · new `axiom`s · `native_decide` · `debug.skipKernelTC` · `unsafe` · `partial def` ·
`@[implemented_by]` · `@[extern]` · `#exit` · `maxHeartbeats 0` · tampering with the audit
commands · contradictory hypotheses · (optionally) unused hypotheses.

Attempting any of these is worse than admitting you could not prove the claim. The verifier
checks the proof's kernel-level axiom footprint, so tactic and macro trickery does not help.

See [references/GUIDE.md](references/GUIDE.md) for the failure catalogue, worked repair
examples, batch/harness use, and setup.

## The part the tool cannot do

Lean checks the proof. It cannot check that your Lean statement means what your English
sentence meant. Always read the elaborated statement and ask: are the quantifiers right? did
I add a hypothesis that makes it easy? is the conclusion the full claim? are the types right?

If the elaborated statement does not match the claim, a green verdict is worthless. That
comparison is yours to make, and it is the reason the tool always prints the statement.

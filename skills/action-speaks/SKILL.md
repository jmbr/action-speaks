---
name: action-speaks
description: Check mathematical or computational claims by proving them in Lean 4 with Mathlib, Physlib and Cslib. Use for non-obvious inequalities, identities, bounds, closed forms, convergence, termination, correctness arguments, and counterexamples in mathematics, physics, or computer science. Also use to check statements for contradictory assumptions and to find library lemmas instead of guessing names.
compatibility: Requires a built action-speaks checkout (Lean 4.33.0 + Mathlib + Physlib + Cslib) and its install.sh setup. Linux/macOS with python3.
metadata:
  repository: action-speaks
---

# Check mathematical claims with Lean

Use action-speaks for mathematical facts that need more than inspection to justify. It checks Lean
proofs, rejects unfinished proofs and untrusted axioms, and looks for problems in assumptions.
It cannot check that a formal statement matches an English claim; you must compare them.

Checks are answered one at a time, so a slow verification delays whatever you ask for next.
Prefer one substantial check to a burst of speculative ones.

## Workflow

### 1. Write the claim in English

State what you intend to prove before writing Lean.

### 2. Check the statement

```bash
~/.agents/skills/action-speaks/scripts/action-speaks statement '(n : ℕ) (h : 5 < n) : 25 < n * n'
```

This returns the *elaborated statement*: what Lean interpreted after resolving notation
and types. It also tries to detect contradictory assumptions. If it finds a contradiction,
review the statement. A clean report does not prove that the assumptions are consistent.

The response may include earlier work from the ledger. Compare the recorded statement with
your claim, then re-verify any source you reuse. Similar wording is not evidence of an
equivalent theorem. A previous tactic failure is a reason to try another approach, not to
give up. A detected contradiction or unnecessary assumption calls for reviewing the
statement.

The commands below use `scripts/action-speaks` relative to this skill directory. Use the full path
above when working elsewhere. For wording-based ledger recall, run
`scripts/action-speaks log --recall TEXT`.

### 3. Find lemmas

```bash
scripts/action-speaks search 'sum of two even numbers is even'
scripts/action-speaks search '|- Irrational (Real.sqrt _)' --backend loogle
scripts/action-speaks close '25 < n * n' -b '(n : ℕ) (h : 5 < n)'
```

Local Loogle searches the installed Mathlib, Physlib, and Cslib. `close` asks the installed
Lean environment for tactic suggestions. Use these instead of guessing lemma names, and
verify the resulting proof.

LeanSearch is remote. Hosted Loogle is available only through `--backend loogle-remote`;
it uses a different Mathlib revision and does not cover Physlib or Cslib.

#### Find reusable earlier proofs

When you need an earlier proof, use local ledger shape search, not just library search:

```bash
scripts/action-speaks search 'Real.sqrt, |- _ ≤ _' --backend ledger
scripts/action-speaks search 'Real.sqrt, |- _ ≤ _' --backend loogle --include-ledger
scripts/action-speaks log --id 123 --json
```

`ledger` searches only earlier targets; `--include-ledger` adds a separate group alongside
library results. Defaults are unchanged. Ordinary ledger searches never compile or verify.
If the cache is missing or outdated, explicitly add `--refresh-ledger` to recheck proofs
and build it. MCP uses `backend: "ledger"`, `include_ledger: true`, and
`refresh_ledger: true` for the same options; retrieve source with `log { "id": 123 }`.

Use the hit's row ID to retrieve its source, include the needed declarations, and verify
a fresh submission. Do not cite generated aliases or assume earlier proofs are preloaded.
Read [export limitations](references/GUIDE.md#ledger-search-limits) before interpreting
missing results.

For a registered Lean project, check a module target rather than copying its structures into
a snippet: MCP `verify` accepts `project`, `module`, and `target`, with `build: true` for
an explicit build. Project `search` and `log` calls use the same selector. Registration and
trust approval must already exist. Keep historical verdicts separate from current-checkout
results; retrieve stable ledger references with `log`'s `ref` argument.

### 4. Verify the proof

```bash
scripts/action-speaks verify "if n > 5 then n squared exceeds 25" --require-nontrivial <<'EOF'
theorem main (n : ℕ) (h : 5 < n) : 25 < n * n := by nlinarith
EOF
```

For `verify`, exit code 0 means verified, 1 means not verified, and 2 indicates a command or
configuration error. Keep `--require-nontrivial` enabled: it rejects the proof if a probe
can establish the conclusion after removing propositional assumptions. It does not prove
that each assumption is necessary.

The wrapper takes a claim and reads source from stdin or `-f FILE`. The installed CLI uses
`action-speaks verify FILE -c "claim"` instead.

### 5. Report the result

For `VERIFIED`, quote the elaborated statement and say it is machine-checked. Check its
types, quantifiers, assumptions, and conclusion against the English claim. If they differ,
revise the statement and verify again.

For any other verdict, explain what failed. Do not present the claim as proved.

## Lean source rules

- No `import` lines: Mathlib, Physlib, Cslib, and the audit module are preloaded.
- Put helpers first and the target theorem last, or select the theorem explicitly.
- Physlib includes unfinished results marked `@[sorryful]` or `@[pseudo]`. Citing one can
  fail the axiom check even if your source contains no `sorry`.
- Cslib covers computation topics such as automata, type systems, and verified algorithms.
  Search it before writing your own definitions.
- Try `nlinarith`, `linarith`, `omega`, `positivity`, `norm_num`, `field_simp`, `aesop`,
  `simp`, `decide`, or `grind` for routine goals.
- Choose types carefully: subtraction on `ℕ` stops at zero, and division discards the
  remainder. Use `ℤ` or `ℝ` when the claim needs different arithmetic.

The verifier rejects `sorry`, new axioms, `native_decide`, kernel bypasses, `unsafe`,
`partial def`, `@[implemented_by]`, `@[extern]`, `#exit`, `maxHeartbeats 0`, and changes to
the audit commands. Submit a complete proof rather than trying to bypass these checks.

See [the reference guide](references/GUIDE.md) for error explanations, worked examples,
and batch use.

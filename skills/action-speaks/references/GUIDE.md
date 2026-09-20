# action-speaks reference

See [SKILL.md](../SKILL.md) for the workflow. Commands below use the `action-speaks` command
to the skill directory.

## Failed checks

| Check | Meaning | Action |
|---|---|---|
| `static_guard` | Banned source construct | Remove it and submit a plain proof |
| `elaboration` | Lean could not type-check the source | Read the error; use `search` or `close` for lemmas |
| `no_sorry` | An unfinished proof occurs in the submission | Complete it or report that it remains unproved |
| `target_declared` | The selected theorem could not be resolved | Check its name or put the intended theorem last |
| `kernel_replay` | The kernel rejected a rechecked declaration | Remove code that bypasses normal checking |
| `audit_integrity` | The audit failed its integrity check | Remove changes to audit behavior |
| `trusted_axioms` | The proof depends on an untrusted axiom | Remove that dependency; check for unfinished Physlib results |
| `statement_sorry_free` | The statement contains `sorry` | Complete the statement |
| `is_theorem` | The target is a definition, not a theorem | Select a theorem |
| `not_vacuous` | The probe found contradictory assumptions | Review the statement, not just the proof |
| `hypotheses_used` | The probe proved the conclusion without the removed assumptions | Review the conclusion and assumptions |

`hypotheses_used` is a warning unless `--require-nontrivial` is enabled. The allowed axioms
are `propext`, `Classical.choice`, and `Quot.sound`.

## Common repairs

### Contradictory assumptions

```lean
-- rejected: no ℕ is both > 5 and < 3
theorem bad (n : ℕ) (h₁ : n > 5) (h₂ : n < 3) : n = 42 := by omega
```

Lean accepts this proof, but its assumptions cannot hold together. Review the intended claim
and correct its assumptions. Changing the proof alone does not address the problem.

### Natural-number arithmetic

```lean
-- rejected: FALSE at n = 0, because 0 - 1 = 0 in ℕ
theorem bad (n : ℕ) : n - 1 < n := by omega

-- correct
theorem good (n : ℕ) (hn : 0 < n) : n - 1 < n := by omega
```

Natural-number subtraction stops at zero; division discards the remainder. Add the needed
assumption or choose a different type, depending on the intended claim.

### Unfinished Physlib results

Physlib includes results marked `@[sorryful]` or `@[pseudo]` that depend on untrusted axioms.
A citation can therefore fail even when your submission contains no unfinished proof:

```text
[FAIL] trusted_axioms - untrusted: sorryAx
  axioms: sorryAx
```

Use a completed result, prove the missing step, or report that the dependency is unfinished.

## Finding lemmas

```bash
action-speaks search 'every continuous function on a compact set attains its maximum'
action-speaks search '|- Continuous (fun _ => _)' --backend loogle
action-speaks search 'Real.sqrt, |- _ ≤ _' --backend loogle
action-speaks close 'Irrational (Real.sqrt 2)'
action-speaks close '0 ≤ x^2' -b '(x : ℝ)'
```

Loogle pattern syntax:

| Syntax | Meaning |
|---|---|
| `?a` | Named wildcard |
| `_` | Anonymous wildcard |
| `|-` | Match only the conclusion |
| `,` | Require all listed constraints |
| `"foo"` | Match declaration names containing `foo` |

Local Loogle searches the installed library versions. If it is unavailable, build it with
`scripts/build-loogle.sh` in the verifier repository. Hosted Loogle must be requested with
`--backend loogle-remote`; it searches a different Mathlib revision without Physlib or Cslib.
Natural-language search uses the remote LeanSearch service.

`close` runs search tactics in Lean against your goal. Treat the suggestions as proof
candidates and run the finished proof through `verify`.

### Ledger-search limits

`--backend ledger` searches cached earlier targets; `--backend loogle --include-ledger`
adds a separate group alongside library results. Only explicit `--refresh-ledger` rechecks
proofs and builds the index. Ordinary searches do neither; a missing or outdated cache asks
for refresh. The default cache is `<root>/build/ledger-search`; override it with
`ACTION_SPEAKS_LEDGER_INDEX_DIR`. `ACTION_SPEAKS_LEDGER_REFRESH_TIMEOUT` defaults to 900 seconds.

Only formerly verified targets that pass current rechecking and export are indexed.
Identical source/target pairs share a hit; helpers are not separate hits. Export copies
kernel-level theorem, definition, and opaque dependency terms with remapped names, not
source text. Targets depending on local inductives, structures, or recursors are excluded
in this initial version. Index metadata lists exclusions; an export failure does not show
that a claim is unprovable. These limits do not change the accepted proof language.

Ledger source stays local. `--backend both --include-ledger` sends only the original query
to remote natural-language search; remote-only backends cannot include ledger results.
Retrieve a hit with `action-speaks log --id 123 --json`, include its needed declarations,
and verify a fresh submission. Generated aliases are not available in normal submissions
or `close`. Reading the index or a source row does not create, migrate, or write the ledger.

## Worked example: claim to verdict

For results that belong to a registered Lean project, use project-backed module verification.
It keeps the original module's structures and dependencies rather than exporting a snippet.
The project ledger stores references and fingerprints, not source snapshots. Project labels
and paths may change; UUIDs and historical entries do not. See `docs/PROJECTS.md` in the
verifier repository for registration, history association/import, and source promotion.

Claim: *the arithmetic mean of two nonnegative reals is at least their geometric mean.*

```bash
action-speaks statement '(a b : ℝ) (ha : 0 ≤ a) (hb : 0 ≤ b) : Real.sqrt (a * b) ≤ (a + b) / 2'

action-speaks verify - -c "AM-GM for two nonnegative reals" --require-nontrivial <<'EOF'
theorem am_gm_two (a b : ℝ) (ha : 0 ≤ a) (hb : 0 ≤ b) :
    Real.sqrt (a * b) ≤ (a + b) / 2 := by
  rw [show a * b = ((a+b)/2)^2 - ((a-b)/2)^2 by ring]
  calc Real.sqrt (((a+b)/2)^2 - ((a-b)/2)^2) ≤ Real.sqrt (((a+b)/2)^2) := by
        apply Real.sqrt_le_sqrt; nlinarith [sq_nonneg ((a-b)/2)]
    _ = |(a+b)/2| := Real.sqrt_sq_eq_abs _
    _ = (a+b)/2 := abs_of_nonneg (by linarith)
EOF
```

After verification, report the elaborated statement:
`∀ (a b : ℝ), 0 ≤ a → 0 ≤ b → √(a * b) ≤ (a + b) / 2`.
Compare it with the English claim before calling the claim machine-checked.

## Refuting a claim

Prove the negation to refute a claim:

```bash
action-speaks verify - -c "not every continuous function is differentiable" <<'EOF'
theorem not_all_cont_diff : ¬ (∀ f : ℝ → ℝ, Continuous f → Differentiable ℝ f) := by
  intro h
  exact not_differentiableAt_abs_zero (h _ continuous_abs 0)
EOF
```

If neither direction is proved, report that the question remains unresolved.

## Repeated or batch use

The CLI starts Lean for each invocation. For repeated checks, reuse sessions through Python:

```python
from action_speaks import Harness

with Harness(pool_size=2).warm() as h:
    verdicts = h.verify_many(
        [{"source": s, "claim": c} for s, c in items],
        require_nontrivial=True,
    )
```

`h.prove(propose)` supports a repair loop that sends failure feedback to your proof generator.
See `docs/INTEGRATION.md` in the verifier repository.

## Setup, history, and reproducibility

```bash
action-speaks doctor
action-speaks log
action-speaks log --recall 'gradient descent'
```

`doctor` checks configuration and whether the verifier accepts and rejects its sample proofs
as expected. Use any error message to identify the missing configuration or build step.
`ACTION_SPEAKS_ROOT` points the command at a different built checkout.

The ledger stores attempts with their source and library versions. Use `--tag NAME` when
verifying related claims and `log --tag NAME` to retrieve them. A failed attempt does not
establish that a claim is unprovable. When reusing an earlier proof, compare its statement
and verify its source again.

Record the toolchain and library revisions with results you publish. A proof can stop
compiling when dependencies change. For this checkout:

```text
Lean 4.33.0, Mathlib db584cd6d46c, Physlib 98fbbee20d0a, Cslib 3951377e5a3f
Allowed axioms: propext, Classical.choice, Quot.sound
```

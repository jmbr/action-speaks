# Instructions for agents using action-speaks

Use action-speaks to check mathematical claims with Lean 4, Mathlib, Physlib, and Cslib.
Verify substantial inequalities, identities, bounds, convergence or correctness arguments,
and counterexamples. Skip trivial arithmetic and non-mathematical claims.

## Workflow

### 1. State the claim in English

Write the intended claim before writing Lean. You will compare it with the statement Lean
actually checks.

### 2. Check the statement

```text
statement { "statement": "(n : Nat) (h : 5 < n) : 25 < n * n" }
```

`statement` checks the types without requiring a proof. It returns the **elaborated
statement**: the statement Lean interpreted after resolving notation and implicit arguments.
It also tries to find contradictory assumptions. If it finds a contradiction, review the
statement before proving it. If it finds none, consistency is still not established.

The response may include earlier ledger entries. Matches use statement text or similar
wording, not a proof that the claims are equivalent. Compare each result with your claim,
then re-verify any stored source you reuse. Do not cite an old ledger entry as a fresh proof.

A rejected attempt is not evidence that the claim is unprovable. Tactic and name-resolution
errors call for another attempt. Failures of `not_vacuous`, `hypotheses_used`,
`statement_sorry_free`, or `is_theorem` call for reviewing the statement or selected target.
The result may also change after a library update.

### 3. Find lemmas instead of guessing names

```text
search { "query": "sum of two even numbers is even" }
search { "query": "|- Irrational (Real.sqrt _)", "backend": "loogle" }
close   { "goal": "25 < n * n", "binders": "(n : Nat) (h : 5 < n)" }
```

Local Loogle searches the installed Mathlib, Physlib, and Cslib. `close` asks the installed
Lean environment for tactic suggestions for a goal. Verify the resulting proof before
citing it.

If local Loogle is unavailable, report that. Use `backend: "loogle-remote"` only explicitly:
the public index uses a different Mathlib revision and does not include Physlib or Cslib.
A missing result there does not establish that a lemma is absent locally.

When looking for reusable earlier proofs, explicitly search the local ledger:

```text
search { "query": "Real.sqrt, |- _ ≤ _", "backend": "ledger" }
search { "query": "Real.sqrt, |- _ ≤ _", "backend": "loogle", "include_ledger": true }
log    { "id": 123 }
```

`ledger` searches only earlier targets; `include_ledger` returns them as a separate group
alongside library results. Ordinary ledger searches read the cache without compiling or
verifying. If it is missing or outdated, opt in to rechecking and rebuilding with
`refresh_ledger: true` on the search call. Defaults remain unchanged.

Retrieve a hit's source by its row ID, include the needed declarations in your submission,
and verify it afresh. Do not cite generated aliases or assume earlier proofs are preloaded.
Only targets that pass current rechecking and export enter the index. Read its exclusions:
an export failure is not evidence of unprovability. See
[export limitations](docs/LEDGER.md#optional-local-ledger-search).

The tools are `verify`, `statement`, `search`, `close`, and `log`. Clients may prefix their
names with the server name, for example `action-speaks-search`.

Over MCP these are answered one at a time: the server reads a request, answers it, and only
then reads the next. Issuing several checks does not overlap them, so a slow verification
delays whatever follows it. Prefer one substantial check to a burst of speculative ones.

For a registered project, use `verify` with `project`, `module`, and `target` instead of
copying the project's definitions into a snippet. Set `build: true` only when you intend
to build the module. Project search and log calls also accept `project`. A hit may link to
older history; compare its current statement and reverify before reuse. Project names can
change, but project and ledger UUIDs remain stable. See [project-backed proofs](docs/PROJECTS.md).

### 4. Verify the proof

```text
verify {
  "source": "theorem main (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
  "claim": "If n is greater than 5 then n squared exceeds 25",
  "require_nontrivial": true
}
```

Keep `require_nontrivial` enabled. It rejects a proof if a probe can establish the conclusion
after removing propositional assumptions. Passing this check does not prove that each
assumption is necessary.

Use `tag` to group related attempts, and `log { "tag": "..." }` to retrieve them.
The ledger keeps both successes and rejections.

### 5. Report the result accurately

Only a `VERIFIED` verdict supports calling the result machine-checked. Quote the returned
elaborated statement, and compare it with the English claim:

- Are the types correct? In particular, natural-number subtraction and division truncate.
- Are the quantifiers (`∀`, `∃`) and their order correct?
- Are the assumptions justified, rather than added to make the proof easy?
- Is the conclusion the full claim?

If the statements differ, correct the Lean and verify again. For any other verdict, explain
what failed without claiming a proof.

## Writing Lean source

- Do not include `import` lines. Mathlib, Physlib, Cslib, FloatLib, and the audit module are preloaded.
- Put helper lemmas first and the theorem to check last, or select it with `target`.
- Use tactics such as `nlinarith`, `linarith`, `omega`, `positivity`, `norm_num`, `field_simp`,
  `aesop`, `simp`, `decide`, and `grind` for routine goals.
- Use `exact?` and `apply?` to find proofs, then submit the concrete term they suggest.
- Physlib includes unfinished results marked `@[sorryful]` or `@[pseudo]`. A proof that
  depends on their untrusted axioms is rejected, even if the submission contains no `sorry`.
  Find a proved result or state that the dependency is unfinished.
- Search Cslib for computation topics such as automata, type systems, and verified algorithms
  before defining your own model.

## Writing style

Use American spelling and vocabulary in code, comments, claims, and documentation.

Keep commit messages and comments short: what changed and why, not what the diff shows.
Comment only what the code cannot say for itself.

## Rejection rules

| Construct or condition | Reason |
|---|---|
| `sorry`, `sorryAx` | Incomplete proof |
| New `axiom` declarations | Untrusted assumptions |
| `native_decide` | Uses compiler-based evidence rather than only kernel checking |
| `debug.skipKernelTC` | Disables kernel type checking |
| `unsafe`, `partial def`, `@[implemented_by]`, `@[extern]` | Unsupported ways to bypass normal checking |
| `#exit` | Stops processing before later declarations |
| Changes to audit commands | Interference with verification |
| Detected contradictory assumptions | The assumptions cannot hold together |
| Conclusion proved without removed assumptions | Warning by default; rejection with `require_nontrivial` |

Lean checks a formal proof, not the accuracy of the translation from English. That final
comparison remains your responsibility.

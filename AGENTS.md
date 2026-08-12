# Instructions for agents using the Lean verifier

You have access to a Lean 4 + Mathlib proof checker. Use it to turn assertions into evidence.

## When to verify

Verify whenever you state a mathematical fact that matters and that you cannot expect the
reader to check by inspection: an inequality, a closed form, a bound, a convergence claim, a
termination or correctness argument, a counterexample.

Do not verify trivial arithmetic, definitional restatements, or claims that are not
mathematical.

## The workflow

**1. State the claim in English first.** Write the claim you intend to support before you
write any Lean. You will need it to check that the formal statement matches.

**2. Check the statement before proving it.**

```
statement { "statement": "(n : Nat) (h : 5 < n) : 25 < n * n" }
```

This elaborates the statement without a proof. It tells you whether it type-checks, shows you
the statement Lean actually understood, and — critically — whether the hypotheses are
**contradictory**. If they are, stop: any proof you write will be rejected, because a theorem
with contradictory hypotheses is vacuously true and supports nothing.

**3. Find the lemmas you need. Do not guess names.**

```
search { "query": "sum of two even numbers is even" }       # by meaning
search { "query": "|- Irrational (Real.sqrt _)", "backend": "loogle" }  # by shape
close   { "goal": "25 < n * n", "binders": "(n : Nat) (h : 5 < n)" }     # ask Lean
```

Inventing a plausible-sounding lemma name is the single most common reason proofs fail.
`close` cannot hallucinate: it reports only lemmas that genuinely close the goal.

(The five tools are named `verify`, `statement`, `search`, `close`, `log` — the same names as
the CLI subcommands. Your client namespaces them by server, typically as `nullius-search` and
so on, which is what distinguishes this `search` from any other tool of that name.)

**4. Verify the proof.**

```
verify {
  "source": "theorem main (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
  "claim":  "If n is greater than 5 then n squared exceeds 25"
}
```

**5. Report honestly.** If the verdict is `VERIFIED`, say the claim is machine-checked and
state the **elaborated statement** from the result, not a looser paraphrase. If the verdict is
anything else, do not present the claim as proved.

## Writing the Lean source

- **No `import` lines.** Mathlib and Physlib are already imported. An `import` in your
  submission is rejected.
- **Physlib results are not all complete.** It ships placeholders marked `@[sorryful]`
  (`sorryAx`) and `@[pseudo]` (`Lean.ofReduceBool`). Citing one is not an error you will see
  in the proof — the audit catches it as an untrusted axiom, and the verdict is a rejection.
  If that happens, the physics result you leaned on is not actually proved yet.
- `search` and `close` both see Mathlib and Physlib.
- Put helper lemmas first and the claim you care about **last**; that last theorem is what
  gets audited by default.
- Prefer `nlinarith`, `linarith`, `omega`, `positivity`, `norm_num`, `field_simp`, `aesop`,
  `simp`, `decide`, `grind` for routine goals.
- `exact?` and `apply?` are for *finding* proofs interactively. Once you know the answer, put
  the concrete term in your submission.

## What will get you rejected

The verifier is adversarial by design. These are all rejected, and attempting them is worse
than admitting you cannot prove the claim:

| Attempt | Why it fails |
|---|---|
| `sorry` anywhere | the proof is incomplete by definition |
| `axiom foo : ...` | you may only use Mathlib's existing foundations |
| `native_decide` | trusts the compiler instead of the kernel |
| `set_option debug.skipKernelTC` | disables the type-checker |
| `unsafe`, `partial def`, `@[implemented_by]`, `@[extern]` | bypass the kernel |
| `#exit` | hides everything after it |
| redefining the audit commands | detected by a tripwire; treated as tampering |
| contradictory hypotheses | vacuously true, supports no claim |
| unused hypotheses | the statement is weaker than it looks (warning, or fatal on request) |

## The trap that matters most

A proof can be perfectly sound and still be worthless, because it proves something other than
what you claimed. Lean checks the proof; it cannot check that the statement means what your
English sentence meant. **That comparison is your job.**

Concretely, always read the elaborated statement in the result and ask:

- Are the quantifiers the ones I meant (`∀` vs `∃`, and in what order)?
- Are the hypotheses the ones I claimed, or did I quietly add one that makes it easy?
- Is the conclusion the full claim, or a weakened version?
- Are types right — `ℕ` subtraction and division truncate, so `n - m` on `ℕ` is not what a
  reader assumes.

If the elaborated statement does not match your English claim, the verification is worthless
no matter how green the verdict is. Fix the statement and verify again.

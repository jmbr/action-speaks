# `polyrith` with a local backend

A working restoration of Mathlib's `polyrith` tactic, which computes a Nullstellensatz
certificate for a polynomial goal and suggests a `linear_combination` call that proves it.
Not part of nullius: kept here because it is finished and tested, and belongs upstream.

```lean
example (x y : ℚ) (h1 : x + y = 3) (h2 : x - y = 1) : x = 2 := by polyrith'
-- Try this: linear_combination h1 / 2 + h2 / 2
```

## Why this exists

`polyrith` asked a public SageMath server for the coefficients. That server was shut down,
and [Mathlib removed the tactic](https://github.com/leanprover-community/mathlib4/commit/ee0f6ec07f)
in September 2025, leaving a note that it "can immediately be restored from history if
someone wants to implement one of these solutions" — one of which is *running Sage locally*.
This is that, done: the certificate is computed on your machine by Singular, through
[passagemath](https://passagemath.org).

## Why it is not part of nullius

`grobner`, already in Mathlib, closes every goal tested here with no external dependency at
all, and at these sizes the two are indistinguishable in elaboration time (~2.35 s each,
almost entirely `import Mathlib`). What `polyrith'` adds is the *certificate*: a
`linear_combination` term a reader can check by eye and a machine can recheck forever without
Singular. That is worth having — but in Mathlib, where everyone benefits and the maintenance
is shared, rather than vendored into a verifier that does not otherwise prove anything.

The maintenance argument is concrete rather than hypothetical: in under a year of removal the
file already needed fixing for `Mathlib.Tactic.Ring` being refactored into `Ring.Common`.

## Contents

| File | |
|---|---|
| `Polyrith.lean` | upstream's tactic, with the network call replaced by a local one |
| `polyrith_helper.py` | Mathlib's `scripts/polyrith_sage_helper.py`, **byte for byte** |
| `polyrith_local.py` | the local backend: stdin query → Singular → the service's JSON reply |

`polyrith_helper.py` is unmodified, which is the point of the design: the mathematics is
upstream's, including the trick from §2.2 of [arxiv.org/pdf/1007.3615](https://arxiv.org/pdf/1007.3615.pdf)
that reduces radical membership to ordinary ideal membership so `lift` can certify it.

Everything except `Polyrith.lean` is Apache 2.0 from Mathlib; nullius is Apache 2.0 too, so
the whole directory is under a single licence.

## Using it

```bash
python3 -m venv .venv-cas
.venv-cas/bin/pip install passagemath-singular passagemath-repl
```

Copy `Polyrith.lean` into a Mathlib-based Lake project as its own library, keeping the two
`.py` files beside it, and point the tactic at the interpreter you just made:

```bash
export NULLIUS_PYTHON=/path/to/.venv-cas/bin/python3
export NULLIUS_POLYRITH_SCRIPT=/path/to/polyrith_local.py
lake env lean YourFile.lean
```

Both default sensibly (`python3`, `scripts/polyrith_local.py`) but the absolute paths are
worth setting: an agent or editor will not have your shell's environment.

The tactic is spelled `polyrith'` because Mathlib still declares `polyrith` as a stub that
throws, and two elaborators for one token would be ambiguous. Rename it if you upstream this.

## What changed from upstream

64 lines, all of which are one of four things. Regenerate the diff with:

```bash
git -C <mathlib> show ee0f6ec07f^:Mathlib/Tactic/Polyrith.lean > /tmp/upstream.lean
diff /tmp/upstream.lean Polyrith.lean
```

1. **`runSage` runs a local process** instead of `curl`ing sagecell, and the dead
   `polyrith.sageUserAgent` option is gone with it. This is the substantive change.
2. **`Ring.Cache` → `Ring.Common.Cache`**, and the corresponding `open`: Mathlib refactored
   `Mathlib.Tactic.Ring` after the removal.
3. **`import Mathlib`** rather than `Mathlib.Tactic.LinearCombination`, since a downstream
   project has no reason to track upstream's minimal import graph.
4. **The `include_str` path**, and the tactic token.

Note what is *not* in that list: `Poly.format` still emits `^`, and the helper is untouched,
because `polyrith_local.py` runs the query through Sage's preparser exactly as the REPL would.
Patching the `^` out by hand also works, but then you are maintaining a fork of Mathlib's
helper for no reason.

## Status

Tested against Lean 4.32.0, Mathlib `81a5d257c8e4`, passagemath 10.8.9. These four goals
produce certificates, and all four certificates pass a full `nullius verify` audit — trusted
axioms only, hypotheses used, no `sorry`:

```lean
example (x y : ℚ) (h1 : x + y = 3) (h2 : x - y = 1) : x = 2 := by polyrith'
example (x y : ℚ) (h : x = y + 1) : x^2 - 2*x*y + y^2 = 1 := by polyrith'
example (x : ℚ) (h : x - 1/3 = 0) : 3 * x = 1 := by polyrith'
example (a b c : ℚ) (h1 : a + b = 3) (h2 : b + c = 5) (h3 : a + c = 4) : a = 1 := by polyrith'
```

Known limitation, inherited from upstream and not introduced here: division by a numeral is
parsed as an opaque atom, so `(h : x / 3 = 2) : x = 6` fails with *"polynomial is not in the
ideal"* — correctly, since `x/3` and `x` are unrelated as far as the ideal is concerned.
Rephrasing as `2 * x = 6` works.

Nothing here is trusted, and that is what makes an unpinned Singular acceptable when an
unpinned lemma index would not be. The certificate is a *suggestion*; Lean elaborates the
`linear_combination` and checks it, so a wrong answer from Singular yields a failed tactic,
never a theorem.

## If you upstream this

The open question is the dependency. Mathlib cannot require `passagemath-singular` of
everyone, so the tactic has to degrade cleanly when the backend is absent —
`polyrith_local.py` already reports `MissingBackend` with the install command, and
`runSage` surfaces it, but a Mathlib PR would want that path tested rather than merely
present.

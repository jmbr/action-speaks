# `polyrith` with a local backend

This contribution restores Mathlib's `polyrith` tactic using a local computer algebra
backend. It asks Singular, through [passagemath](https://passagemath.org), for polynomial
coefficients and returns a `linear_combination` proof for Lean to check.

It is separate from nullius and is not included in the verifier build.

```lean
example (x y : ℚ) (h1 : x + y = 3) (h2 : x - y = 1) : x = 2 := by polyrith'
-- Try this: linear_combination h1 / 2 + h2 / 2
```

## Background

The original tactic used a public SageMath server. After that service shut down,
[Mathlib removed the tactic](https://github.com/leanprover-community/mathlib4/commit/ee0f6ec07f).
This version replaces the network request with a local process.

Mathlib's `grobner` tactic is an alternative that needs no external backend. The reason to
use `polyrith'` is its explicit certificate: the suggested proof can be checked later
without Singular.

## Files

| File | Purpose |
|---|---|
| `Polyrith.lean` | Adapted upstream tactic with a local process call |
| `polyrith_helper.py` | Unmodified upstream `scripts/polyrith_sage_helper.py` |
| `polyrith_local.py` | Reads a query from stdin, calls Singular, and returns JSON |

The helper retains upstream's algorithm for producing certificates, including the
radical-membership method in [section 2.2 of this paper](https://arxiv.org/pdf/1007.3615.pdf).
The code is under Apache 2.0; see the source headers and repository license.

## Setup

```bash
python3 -m venv .venv-cas
.venv-cas/bin/pip install passagemath-singular passagemath-repl
```

Add `Polyrith.lean` to a Mathlib-based Lake project as a library, with the two Python files
beside it. Set the interpreter and backend paths:

```bash
export NULLIUS_PYTHON=/path/to/.venv-cas/bin/python3
export NULLIUS_POLYRITH_SCRIPT=/path/to/polyrith_local.py
lake env lean YourFile.lean
```

The defaults are `python3` and `scripts/polyrith_local.py`. Absolute paths avoid dependence
on the working directory and shell environment.

The tactic is named `polyrith'` because Mathlib still defines `polyrith` as a removed-tactic
stub. Use the generated `linear_combination` proof, not the external tactic call, when
submitting a proof to nullius.

## Changes from upstream

- `runSage` calls a local process instead of sagecell; the unused user-agent option is removed.
- `Ring.Cache` and its namespace are updated to `Ring.Common.Cache`.
- The file imports `Mathlib` rather than a narrower set of tactic modules.
- The embedded helper path and tactic name are adjusted.

To compare with the original:

```bash
git -C <mathlib> show ee0f6ec07f^:Mathlib/Tactic/Polyrith.lean > /tmp/upstream.lean
diff /tmp/upstream.lean Polyrith.lean
```

The helper still emits `^` for powers. The backend uses Sage's preparser, so the helper does
not need a separate syntax conversion.

## Examples and limitations

The recorded compatibility baseline is Lean 4.32.0, Mathlib `81a5d257c8e4`, and passagemath
10.8.9. These examples produced certificates that passed nullius's audit:

```lean
example (x y : ℚ) (h1 : x + y = 3) (h2 : x - y = 1) : x = 2 := by polyrith'
example (x y : ℚ) (h : x = y + 1) : x^2 - 2*x*y + y^2 = 1 := by polyrith'
example (x : ℚ) (h : x - 1/3 = 0) : 3 * x = 1 := by polyrith'
example (a b c : ℚ) (h1 : a + b = 3) (h2 : b + c = 5) (h3 : a + c = 4) : a = 1 := by polyrith'
```

The parser treats expressions such as `x / 3` as opaque atoms. A goal that requires relating
that expression to `x` may fail with "polynomial is not in the ideal." Clear denominators
before calling the tactic.

The certificate still needs to pass Lean's checks. A result from Singular alone is not a
verified proof.

## Upstreaming

The backend is an optional dependency. When it is missing, `polyrith_local.py` reports
`MissingBackend` with an install command, and the tactic displays that error. An upstream
contribution should include a test for this path.

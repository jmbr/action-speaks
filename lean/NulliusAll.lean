/-
The root module for local lemma search.

Loogle indexes whatever a single root module transitively imports, so this exists to make
that set *exactly* the environment a submission is checked in — the same four imports as
`PRELUDE` in `nullius/repl.py`. Indexing `Mathlib` alone would hide every Physlib and Cslib
lemma, and indexing either of those alone would hide most of Mathlib, since neither imports
more of it than it needs. Either way the search would disagree with the verifier about what
exists, which is the one thing a lemma search must never do.

Keep this list and `PRELUDE` in step. Nothing enforces it mechanically, and the failure is
silent: search would keep answering, just about a different library set than the one a proof
is checked against.

This module is not part of the verifier's trusted path; it is imported by nothing else.
-/
import Mathlib
import Physlib
import Cslib
import Nullius.Audit

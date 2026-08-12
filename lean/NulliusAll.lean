/-
The root module for local lemma search.

Loogle indexes whatever a single root module transitively imports, so this exists to make
that set *exactly* the environment a submission is checked in — the same three imports as
`PRELUDE` in `nullius/repl.py`. Indexing `Mathlib` alone would hide every Physlib lemma, and
indexing `Physlib` alone would hide most of Mathlib, since Physlib imports only the parts it
needs. Either way the search would disagree with the verifier about what exists, which is the
one thing a lemma search must never do.

This module is not part of the verifier's trusted path; it is imported by nothing else.
-/
import Mathlib
import Physlib
import Nullius.Audit

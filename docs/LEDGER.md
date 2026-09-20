# Ledger, recall, and reproducibility

The SQLite *ledger* stores verification attempts with their source, result, and library
versions. It keeps rejected attempts as well as successful ones. This document covers
finding earlier work, building the optional local search index, and reproducing a verdict.

## Recall earlier work

`statement` automatically looks for similar earlier work. CLI and MCP `verify` also look
for identical source. You can search directly:

```bash
action-speaks log --recall 'gradient descent step size'
```

Recall uses three kinds of match:

| Match | Meaning |
|---|---|
| Source hash | The source text is identical |
| Normalized statement text | The printed statements match after limited text normalization |
| Full-text search | The wording is similar; only previously verified entries are returned |

Statement matching is a heuristic, not a test of mathematical equivalence. Renaming a
variable can prevent a match. Even a match must be compared with the current claim.
**Reuse the stored source, but verify it again before citing it.**

A rejected attempt does not establish that the claim is unprovable. Read the failed check:
a tactic error calls for another proof attempt; a detected contradiction calls for reviewing
the statement. Library updates can change either outcome.

## Optional local ledger search

Default searches are unchanged. Use Loogle patterns to search earlier verified targets:

```bash
action-speaks search 'Real.sqrt, |- _ ≤ _' --backend ledger --refresh-ledger
action-speaks search 'Real.sqrt, |- _ ≤ _' --backend ledger
action-speaks search 'Real.sqrt, |- _ ≤ _' --backend loogle --include-ledger
```

`backend="ledger"` searches only the ledger. `include_ledger` adds a separate ledger group
to `loogle` or `both`; it is invalid with remote-only backends. Ledger source stays local.
With `both`, only the original query goes to the existing remote natural-language search.

HTTP `POST /search` and MCP `search` accept the same keys. For example, this explicitly
refreshes before searching:

```json
{"query": "Real.sqrt, |- _ ≤ _", "backend": "ledger", "refresh_ledger": true}
```

For library and ledger results together, use `"backend": "loogle", "include_ledger": true`.
Python `Harness.search` uses the same keyword flags, with Python booleans.

**Refresh is explicit.** Ordinary ledger searches never invoke the compiler, Lake, the
verifier, or an index build. A missing or outdated cache returns an error asking for
`--refresh-ledger` (API: `refresh_ledger=true`). Refresh requires `backend="ledger"` or
`include_ledger`; `Harness(log=False)` rejects explicit ledger access.

Refresh rechecks formerly verified targets against the current environment and indexes only
those that pass rechecking and export. Identical source/target pairs are deduplicated;
helpers are not separate hits. The cache holds immutable audited `.olean` entries and a
small, target-only Loogle index, not a rebuilt Mathlib index.

Export copies kernel-level theorem, definition, and opaque dependency terms with names
remapped, not source text. This initial version excludes targets whose dependency closure
contains locally declared inductives, structures, or recursors. Index result metadata lists
exclusions; failures do not show that a claim is unprovable. These are export limitations,
not changes to the proof language accepted by the verifier.

**Reuse source, not generated aliases.** Use a hit's row ID to retrieve its current source:
CLI `action-speaks log --id 123 --json`, MCP `log { "id": 123 }`, or HTTP `GET /ledger/123`.
Include the needed declarations and verify a fresh submission. Earlier proofs are not
preloaded, and `close` is unchanged. Index and single-row source reads do not create, migrate,
or write the ledger.

For project-backed proofs, the ledger stores a module reference and environment fingerprint
rather than a source snapshot. See [project-backed proofs](PROJECTS.md) before interpreting
or moving those entries.

## Reproduce a verdict

The ledger records the source, toolchain, and Mathlib, Physlib, and Cslib revisions.
The checkout pins its dependencies in `lean/lake-manifest.json`. Current library pins are:

```text
toolchain   leanprover/lean4:v4.34.0
mathlib     5ed2965256430c3649e86755f9576b54eca72435
physlib     d410e856abdd7c4c747332da2533c1596327a473
cslib       990e65a685bed413f43b139db900a36ad5322a10
floatlib    0d91825727839f597fd06b22fdd038ea21480f0c
```

Rebuild the recorded versions and rerun the stored source to reproduce a result. Set
`ACTION_SPEAKS_NO_DAEMON=1` when the check must run in the selected checkout rather than
through an already-running daemon.

To find out whether those pins can move, run `scripts/check_updates.py`. Lake admits one
Mathlib, so a bump is possible only at a release where every required library agrees on the
same revision; the script reports that agreement rather than each library's latest version,
and says what a move would cost. It needs no built checkout.

See [operation and configuration](INTEGRATION.md#operation-and-configuration) for ledger,
daemon, timeout, and search-index environment variables.

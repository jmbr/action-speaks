# nullius — check an agent's mathematical claims

nullius checks Lean 4 proofs written by AI agents. It uses Mathlib for mathematics,
Physlib for physics, and Cslib for computer science. You can call it from Python, the
command line, an HTTP service, or an MCP tool.

It checks more than whether a proof compiles: it rejects incomplete proofs and untrusted
axioms, and looks for contradictory or unnecessary assumptions. **You still need to check
that the Lean statement matches the claim you intended.**

The name comes from *nullius in verba*: "on the word of no one."

## Start here

| Task | Guide |
|---|---|
| Install and build nullius | [Setup from scratch](#setup-from-scratch) |
| Enable it in an agent | [Agent setup](docs/SETUP-AGENTS.md) |
| Use Python, HTTP, MCP, or the CLI | [Integration guide](docs/INTEGRATION.md) |
| Work through mathematical examples | [Cookbook](docs/COOKBOOK.md) |
| Follow the agent workflow | [AGENTS.md](AGENTS.md) |
| Keep proofs in a Lean project | [Project-backed proofs](docs/PROJECTS.md) |

Once installed:

```bash
nullius doctor
nullius statement '(n : Nat) (h : 5 < n) : 25 < n * n'
nullius search 'sum of two even numbers is even'
nullius close '25 < n * n' -b '(n : Nat) (h : 5 < n)'
nullius verify proof.lean -c "My claim" --require-nontrivial
nullius log --stats
```

Starting Lean takes seconds. Run the daemon once and the commands above answer in
milliseconds:

```bash
nullius serve --install     # a systemd user service, or a launchd agent on macOS
nullius serve               # or just run it in the foreground
```

For repeated checks from Python, keep a session open:

```python
from nullius import Harness

with Harness(pool_size=1).warm() as h:
    v = h.verify(
        "theorem t (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
        claim="if n > 5 then n squared exceeds 25",
        require_nontrivial=True,
    )
    print(v.statement if v.verified else v.feedback())
```

## What it checks

Lean accepts declarations that use `sorry` (an unfinished proof) or new axioms. Those are
useful while developing a proof, but they are not evidence for a claim. nullius checks the
proof's axiom dependencies and allows only `propext`, `Classical.choice`, and `Quot.sound`.

A complete proof can also prove the wrong thing. nullius runs two additional checks:

- **Contradictory assumptions (vacuity).** It tries to derive `False` from the assumptions.
  If it succeeds, the theorem is rejected.
- **Unnecessary assumptions (triviality).** It tries to prove the conclusion after removing
  propositional assumptions. If it succeeds, it warns by default or rejects the proof when
  `require_nontrivial` is enabled.

These two checks use a fixed set of tactics. They can miss problems. Passing them does
**not** establish that the assumptions are consistent, that each is necessary, or that the
statement matches your informal claim.

### Verification pipeline

| Check | What fails |
|---|---|
| `static_guard` | Banned source constructs, including new axioms, kernel bypasses, custom elaboration, IO, and changes to the audit code |
| `elaboration` | Lean reports errors while translating the source into a typed proof |
| `no_sorry` | The submission contains unfinished proofs |
| `target_declared` | The theorem selected for checking cannot be resolved |
| `kernel_replay` | The kernel rejects declarations rechecked after elaboration |
| `audit_integrity` | The audit fails to identify an injected, known-bad proof |
| `trusted_axioms` | The target depends on an axiom outside the allowed set |
| `statement_sorry_free` | The theorem's statement contains `sorry` |
| `is_theorem` | The target is a definition rather than a theorem |
| `not_vacuous` | The probe finds contradictory assumptions |
| `hypotheses_used` | The probe proves the conclusion without the removed assumptions; a warning unless `require_nontrivial` is enabled |

### Why replay and an integrity check are needed

An axiom check alone is not enough. Lean metaprograms can insert declarations without a
kernel check. nullius therefore rechecks eligible local declarations with the kernel before
examining their axiom dependencies.

The audit also runs in the submission's environment, where modified audit commands could
report false results. To detect this, nullius adds a known-bad proof, called a *canary*,
alongside a randomly named alias of the target. The audit must reject the canary.

These checks supplement the source guard; they are not a sandbox for running arbitrary
untrusted code. The [adversarial tests](tests/test_adversarial.py) exercise the checks with
and without the guard.

## Limitations

- **Check the translation.** Compare the returned *elaborated statement*—the statement Lean
  actually interpreted—with the informal claim. Pay attention to types, assumptions, and
  quantifiers.
- **The probes are incomplete.** A failed attempt to find a contradiction is not a proof
  that the assumptions can be satisfied. The unnecessary-assumption check has the same
  limitation.
- **Some Physlib results are unfinished.** A citation can compile but still depend on
  `sorryAx` or `Lean.ofReduceBool`. The axiom check rejects those dependencies.
- **Library versions matter.** Lean, Mathlib, Physlib, Cslib, and the REPL must use compatible
  versions. Update the pins in `lean/lakefile.toml` together.
- **Search results depend on the backend.** Local Loogle searches the installed Mathlib,
  Physlib, and Cslib. Hosted Loogle searches a different Mathlib version and does not cover
  the other two libraries. LeanSearch is a remote natural-language service. `close` asks
  the installed Lean environment for tactic suggestions.

## Recalling earlier work

The SQLite *ledger* stores verification attempts, including their source, result, and
library versions. It keeps rejected attempts as well as successful ones.

`statement` automatically looks for similar earlier work. CLI and MCP `verify` also look
for identical source. You can search directly:

```bash
nullius log --recall 'gradient descent step size'
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

### Optional local shape search

Use Loogle patterns to find reusable earlier proofs:

```bash
nullius search 'Real.sqrt, |- _ ≤ _' --backend ledger --refresh-ledger
nullius search 'Real.sqrt, |- _ ≤ _' --backend ledger
nullius search 'Real.sqrt, |- _ ≤ _' --backend loogle --include-ledger
nullius log --id 123 --json
```

`ledger` searches only cached ledger targets; `--include-ledger` adds a separate group
alongside library results. Defaults are unchanged. Only explicit `--refresh-ledger`
rechecks proofs and builds the cache; ordinary ledger searches never compile or verify.
A missing or outdated cache asks you to refresh.

Hits point to source, not preloaded lemmas. Retrieve the row by its ID, include the needed
declarations, and verify a fresh submission. See the
[ledger-search guide](docs/INTEGRATION.md#optional-local-ledger-search) for configuration
and export limitations.

## Setup from scratch

### Prerequisites

- Python 3.10 or later and Git.
- [elan](https://github.com/leanprover/elan), which provides Lean and `lake`.
- Roughly 12 GB of free disk space for the toolchain, compiled libraries, and search index.

elan reads `lean/lean-toolchain` to select the required Lean version.

### Install the Python package

Run from this checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

nullius has no required Python dependencies. Use an **editable install** (`-e`): the driver
expects the built `lean/` project beside its source. It is not a standalone PyPI package.

Without installing, you can run `python3 -m nullius.cli` from the repository root, or use
`skills/nullius/scripts/nullius` from another directory.

### Build Lean and the libraries

```bash
cd lean
lake exe cache get && lake build
lake build repl
lake build Physlib
lake build Cslib
cd ..
./scripts/build-loogle.sh
```

Mathlib's compiled files are downloaded from its cache. Physlib and Cslib build locally,
which can take several minutes.

The last command builds local Loogle in `vendor/`. Its first query builds a search index;
use `./scripts/build-loogle.sh --index` to build the index in advance. The index is cached
and rebuilt when the compiled libraries change.

Without local Loogle, shape search reports that it is unavailable. It never silently
switches to the public service. To use that service explicitly, choose
`--backend loogle-remote`.

### Enable the command and agent tools

```bash
./install.sh --dry-run
./install.sh
nullius doctor
```

The installer adds the command to `~/.local/bin`, links the skill, and configures Copilot's
MCP server. It uses the checkout's interpreter, so virtualenv activation is not required.
See [agent setup](docs/SETUP-AGENTS.md) for individual install options.

`doctor` checks that a complete proof is accepted and that an unfinished proof and a theorem
with contradictory assumptions are rejected.

## Performance

Starting Lean loads the libraries and takes several seconds. Reusing a session avoids that
cost; simple proofs can then take milliseconds, while harder proofs and probes take longer.
Use `Harness`, a persistent server, or `nullius serve` for repeated work — with the daemon
running, a `nullius verify` that took three seconds takes about a third of one.

Each concurrent verification needs a session, and a session is expensive: roughly 7.5 GB
resident, of which about 4 GB is private, the rest being shared `.olean` pages. A second
session therefore costs much less than the first, but not nothing. Measure on your own
workload before increasing the pool size. Submissions start from the same initial environment, so definitions
from one submission are not available to the next.

## Reproducing a verdict

The ledger records the source, toolchain, and Mathlib, Physlib, and Cslib revisions.
The checkout pins its dependencies in `lean/lake-manifest.json`. Current library pins are:

```text
toolchain   leanprover/lean4:v4.33.0
mathlib     db584cd6d46c92f209a44c0f1c829460d327499d
physlib     98fbbee20d0a376ea9160408517a48e274df1663
cslib       3951377e5a3f5772737f11cd62bc5bb6a72f95d1
```

Rebuild the recorded versions and rerun the stored source to reproduce a result.

To find out whether those pins can move, run `scripts/check_updates.py`. Lake admits one
Mathlib, so a bump is possible only at a release where every required library agrees on the
same revision; the script reports that agreement rather than each library's latest version,
and says what a move would cost. It needs no built checkout.

## Project layout

| Path | Purpose |
|---|---|
| `lean/Nullius/Audit.lean` | Lean audit commands |
| `lean/lakefile.toml`, `lean/lake-manifest.json` | Dependency requirements and locked revisions |
| `lean/NulliusAll.lean` | Combined imports for the local search index |
| `nullius/config.py`, `nullius/repl.py` | Configuration, Lean processes, and session pool |
| `nullius/guard.py`, `nullius/verify.py` | Source restrictions, verification pipeline, and verdicts |
| `nullius/ledger.py`, `nullius/search.py` | Stored results, recall, and lemma search |
| `nullius/harness.py` | Python API |
| `nullius/cli.py`, `nullius/http_server.py`, `nullius/mcp_server.py` | CLI and server entry points |
| `skills/nullius/`, `mcp/`, `install.sh` | Agent instructions and installation |
| `tests/`, `noxfile.py`, `.pre-commit-config.yaml` | Tests and development checks |
| `scripts/check_updates.py` | Which Lean release the pins could move to, and what it costs |
| `contrib/` | Separate contributions, not part of the verifier build |

## Contributing

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/prek install
.venv/bin/nox -s lint format
.venv/bin/nox -s types
.venv/bin/nox -s tests
```

Use `nox -s fix` to apply formatting. The tests are pytest tests: `nox -s quick` runs the
Lean-free ones in about a second, and `nox -s tests` runs everything, including the
documentation suite that verifies the cookbook's Lean blocks and the skill guides' proof
examples. Tests needing a built `lean/` checkout carry the `lean` marker, so
`pytest -m "not lean"` selects the rest. Arguments reach pytest after `--`, for example
`nox -s tests -- -k adversarial`.

Commit hooks are deliberately few: file hygiene, a Conventional Commits check, plus
`nox -s quick`. The Lean-backed tests need a built checkout and take minutes, so run
`nox -s tests` before pushing rather than on every commit. Use
`.venv/bin/prek run --all-files` to run all hooks.

### Commit messages

Subjects follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), so
that release tooling can read the history rather than have somebody summarise it. `prek
install` wires up a `commit-msg` hook that rejects anything else.

```text
feat: add a per-user session daemon
fix(daemon): stop a failed bind unlinking the winner's socket
docs: say that the MCP server answers one request at a time
feat!: rename the environment variables
```

`feat` is a minor bump and `fix` a patch one. `docs`, `test`, `build`, `ci`, `refactor`,
`perf`, `style` and `chore` release nothing. A `!` before the colon, or a `BREAKING CHANGE:`
footer, marks a breaking change; below 1.0 that is still a minor bump, since 2.0 would claim
a stability this project has not offered yet. Scopes are optional and free-form.

Only the subject is constrained. The body is where the reasoning goes, and it is the part
worth writing: say why the change is right, not what the diff already shows.

## Related tools

- [SafeVerify](https://github.com/GasStationManager/SafeVerify) checks proofs against a supplied
  specification. Its kernel-replay approach informed this project.
- [lean4checker](https://github.com/leanprover/lean4checker) rechecks declarations with Lean's kernel.
- [LeanTool](https://github.com/GasStationManager/LeanTool) provides a Lean feedback loop and
  contradiction checks.
- [lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp) exposes Lean's language server and search tools.

nullius focuses on claims whose statements are written by the agent, rather than supplied
as a fixed specification.

## License and authorship

Apache 2.0; see [LICENSE](LICENSE). Lean, Mathlib, Physlib, Cslib, and Loogle also use Apache 2.0.
Coding agents contributed under human direction and review; commit trailers identify
co-authored commits.

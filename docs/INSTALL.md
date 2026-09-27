# Install and build action-speaks

Most of the work here is one-time: building Lean, Mathlib, Physlib, Cslib, FloatLib and the
search index for this checkout. Once that is done, installing is two commands.

## If the checkout is already built

Re-installing after a `git pull`, or wiring up another agent harness on the same machine:

```bash
.venv/bin/pip install -e .
python3 install.py
.venv/bin/action-speaks doctor
```

`install.py` links the command and skill and adds the MCP entry; it builds nothing, so it
succeeds even when Lean is missing. `doctor` is what tells you whether the checkout actually
works. If it reports a missing REPL or library, build it first. See
[agent setup](SETUP-AGENTS.md) for what `install.py` touches.

## Building it the first time

### Prerequisites

- Python 3.10 or later and Git.
- [elan](https://github.com/leanprover/elan), which provides Lean and `lake`.
- Roughly 12 GB of free disk space for the toolchain, compiled libraries, and search index.

Elan reads `lean/lean-toolchain` to select the required Lean version.

### Python package

Run from the checkout root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

action-speaks has no required Python dependencies. Use an **editable install** (`-e`): the
driver expects the built `lean/` project beside its source. It is not a standalone PyPI
package.

Without installing, you can run `python3 -m action_speaks.cli` from the repository root. The
skill needs the installed `action-speaks` command, so create the virtualenv before using it.

### Lean and the libraries

```bash
cd lean
lake exe cache get && lake build
lake build repl
lake build Physlib
lake build Cslib
lake build FloatLib
cd ..
./scripts/build-loogle.sh
```

Mathlib's compiled files are downloaded from its cache. Physlib, Cslib and FloatLib build
locally, which can take several minutes. All four libraries are needed: a submission is
elaborated with every one of them imported, so `lake build` on its own — which reaches only
Mathlib — is not enough. Keep them as separate commands rather than collapsing them into one
`lake build` invocation; the release workflow builds them the same way, and
`tests/test_release.py` checks that it still does.

The last command builds local Loogle in `vendor/`. Its first query builds a search index
over `ActionSpeaksAll`, the module importing the same libraries a submission sees; use
`./scripts/build-loogle.sh --index` to build that module and its index in advance. The index
is cached and rebuilt when the compiled libraries change.

Without local Loogle, shape search reports that it is unavailable. It never silently
switches to the public service. To use that service explicitly, choose
`--backend loogle-remote`.

### Validate the checkout

```bash
.venv/bin/action-speaks doctor
```

`doctor` checks paths and library versions, then confirms that a complete proof is accepted
while an unfinished proof and a theorem with contradictory assumptions are rejected.

## Next steps

- Continue with [agent setup](SETUP-AGENTS.md) to install the command, skill, and MCP entry.
- For repeated checks, optionally [run the session daemon as a service](INTEGRATION.md#running-it-as-a-service)
  so clients can share warm Lean sessions.

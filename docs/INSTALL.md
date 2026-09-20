# Install and build action-speaks

This guide prepares one checkout: its Python environment, Lean toolchain, libraries, and
search index. After the checkout works, follow [agent setup](SETUP-AGENTS.md) to connect it
to agent clients on the current machine.

## Prerequisites

- Python 3.10 or later and Git.
- [elan](https://github.com/leanprover/elan), which provides Lean and `lake`.
- Roughly 12 GB of free disk space for the toolchain, compiled libraries, and search index.

Elan reads `lean/lean-toolchain` to select the required Lean version.

## Install the Python package

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

## Build Lean and the libraries

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
locally, which can take several minutes.

The last command builds local Loogle in `vendor/`. Its first query builds a search index;
use `./scripts/build-loogle.sh --index` to build the index in advance. The index is cached
and rebuilt when the compiled libraries change.

Without local Loogle, shape search reports that it is unavailable. It never silently
switches to the public service. To use that service explicitly, choose
`--backend loogle-remote`.

## Validate the checkout

```bash
.venv/bin/action-speaks doctor
```

`doctor` checks paths and library versions, then confirms that a complete proof is accepted
while an unfinished proof and a theorem with contradictory assumptions are rejected.

## Next steps

- Continue with [agent setup](SETUP-AGENTS.md) to install the command, skill, and MCP entry.
- For repeated checks, optionally [run the session daemon as a service](INTEGRATION.md#running-it-as-a-service)
  so clients can share warm Lean sessions.

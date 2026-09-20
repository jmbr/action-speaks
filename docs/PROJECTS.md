# Project-backed proofs

A registered Lean project keeps its own ledger at `.nullius/ledger.sqlite3`.
The project remains the source of its definitions and proofs. nullius records verification
attempts and builds disposable search caches; it does not copy or snapshot the project.

Standalone `nullius verify FILE` and the default ledger continue to work as before.

## Register a project

```bash
nullius project register /path/to/mechanics --name mechanics
nullius project list
```

Registration creates a project UUID and binds its ledger to that UUID. The display name
and directory are not the identity. Registration does not build the project or install its
dependencies.

Before running project checks or loading its search index, explicitly approve execution:

```bash
nullius project register /path/to/mechanics --trust
```

Only approve projects you trust: Lake configurations and project code can execute programs.
This is not a sandbox. Prepare the project's locked dependencies yourself. nullius uses
offline Lake commands, not automatic dependency installation.

Project configuration lives in `.nullius/project.json`; registration and local trust
settings live in the user registry. `NULLIUS_PROJECT_REGISTRY` overrides the registry path.
Keep generated ledgers and caches out of version control. Keep the project identity file
if you want a copy of the checkout to retain its identity.

## Check a named theorem

```bash
nullius verify --project mechanics --module Mechanics.Bounds \
  --target Mechanics.Bounds.energy_bound --build
```

`--build` explicitly allows Lake to build the selected module. Without it, verification
requires up-to-date build artifacts:

```bash
nullius verify --project mechanics --module Mechanics.Bounds \
  --target Mechanics.Bounds.energy_bound --require-nontrivial
```

The project must use the verifier's Lean toolchain and compatible shared dependency sources.
Unlike snippet export, module verification retains the original structures, constructors,
recursors, and instances. It replays project declaration groups through the kernel and
checks the selected theorem's axioms, statement, and assumptions.

A project record identifies the module and target rather than storing another copy of the
source files. It includes an environment fingerprint and available Git provenance. A
record from a dirty or unversioned checkout does not promise historical reconstruction.
If the project changes later, the original verdict and recorded statement stay unchanged.

You can also use `--project` with an ordinary proof file. That records a **snippet** in the
project ledger; it does not add project modules to the snippet's imports.

## Search and retrieve

```bash
nullius search 'Real.sqrt' --project mechanics --backend ledger --refresh-ledger
nullius search 'Real.sqrt' --project mechanics --backend ledger
nullius log --project mechanics --id 91 --json
```

Project-backed results describe declarations checked in the **current checkout**. Their
linked history can refer to older statements or environments; the index does not update
those historical verdicts. Reverify the module target before relying on a result.

Only explicit refresh builds indexes or rechecks targets. Ordinary searches read caches and
report when a refresh is needed. Project references and standalone snippets are searched
separately, so their Lean environments are not merged.
If a project-specific name is unknown in the snippet environment, that part of the query
is reported as inapplicable rather than hiding valid project results.

## Rename or move a project

```bash
nullius project rename mechanics continuum
```

The UUID and ledger stay unchanged. The previous name remains an alias. Historical names,
targets, and verdicts are not rewritten.

If you move the directory, move `.nullius/` with it and register the new path:

```bash
nullius project register /new/path/to/continuum
```

The UUID identifies the moved project. If its old registered directory still exists, use
`--relocate` to explicitly select the new location. A ledger belonging to a different
project is an error, not something registration silently repairs.

Renaming Lean modules or namespaces is a source change, not just a label change. Rebuild and
verify the new targets; retain old records as history.

## Associate or copy existing history

First preview a selection from an existing ledger:

```bash
nullius project link mechanics --from-ledger /path/to/ledger.sqlite3 \
  --tag mechanics --dry-run
```

Remove `--dry-run` to add references without copying the verification rows. Select individual
entries with repeated `--id` flags instead of `--tag`.

Use `import` when the project needs its own copy of the history:

```bash
nullius project import mechanics --from-ledger /path/to/ledger.sqlite3 \
  --id 91 --id 92 --dry-run
```

Imports preserve original source, statements, timestamps, statuses, and provenance,
including rejected attempts. They never delete the source rows. Origin references make
repeated imports idempotent. Importing an already linked entry replaces that link with
the local copy, so the copied history no longer requires the source ledger.

Every ledger has its own UUID. A durable entry reference is `LEDGER_UUID:ROW_ID`; numeric
IDs alone are local to one database:

```bash
nullius log --project mechanics --ref LEDGER_UUID:ROW_ID --json
```

Links require the source ledger to remain available. If it moves, relink it at the new
path; its UUID is checked before its rows are used.

Neither association nor import converts an old snippet into an importable project theorem.
To promote it, put the needed definitions and proof in a project module, then record a new
verification linked to the old entry:

```bash
nullius verify --project mechanics --module Mechanics.Bounds \
  --target Mechanics.Bounds.energy_bound --build --derived-from LEDGER_UUID:ROW_ID
```

## Python and agents

```python
from nullius import Harness

with Harness(project="mechanics") as h:
    verdict = h.verify_module(
        "Mechanics.Bounds", "Mechanics.Bounds.energy_bound", build=True
    )
    results = h.search("Real.sqrt", backend="ledger", refresh_ledger=True)
```

MCP `verify` accepts `project`, `module`, `target`, and optional `build` arguments.
MCP `search` and `log` accept a project selector; `log` also accepts `ref`.
Project registration and trust approval remain explicit host-side operations, not proof
submission options.

For HTTP, start the service with `--project mechanics` to select its project context.
The existing `--no-log` policy still applies.

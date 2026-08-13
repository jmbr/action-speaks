# nullius — making an agent back up its claims

A verification harness that lets an LLM agent prove its mathematical assertions in Lean 4 +
Mathlib, and that refuses to accept the proof unless it is actually worth something.

*Nullius in verba* — "on the word of no one" — is the Royal Society's motto, adopted as a
commitment to settle questions by evidence rather than by authority. An agent's word is
exactly the kind of authority it refers to.

## The problem this solves

"The proof compiled" is a much weaker statement than it sounds. On this machine, before any
of this was built:

```lean
axiom evil : False
theorem cheat : 2 + 2 = 5 := absurd evil (by simp)
```

compiles with **zero errors**. So does any proof containing `sorry`. And even an honest,
fully-checked proof can be worthless:

```lean
theorem useless (n : Nat) (h₁ : n > 5) (h₂ : n < 3) : n = 42 := by omega
```

This is a genuine theorem — Lean is right to accept it — but no such `n` exists, so it
supports no claim whatsoever. An agent that autoformalises carelessly produces these
constantly, and they look like success.

So there are two independent failure modes:

1. **Unsound proof** — `sorry`, injected axioms, `native_decide`, kernel bypasses.
2. **Sound proof of the wrong statement** — vacuous hypotheses, weakened conclusions, type
   confusion. This is the one that will actually bite you.

## How it addresses them

**Soundness** is settled by asking Lean what the proof ultimately rests on, rather than
whether it compiled. `#print axioms` reports kernel-level dependencies and sees through every
tactic, macro and `set_option`:

```
cheat  → depends on axioms: [evil, propext]                    rejected
sorry  → depends on axioms: [sorryAx]                          rejected
native → depends on axioms: [..._native.native_decide.ax_1]    rejected
honest → depends on axioms: [propext, Classical.choice, Quot.sound]   accepted
```

Anything outside `{propext, Classical.choice, Quot.sound}` is disqualifying.

**Meaningfulness** is settled by two probes written as Lean metaprograms:

- *Vacuity*: try to derive `False` from the theorem's hypotheses alone. If that succeeds, the
  hypotheses are contradictory and the theorem is vacuously true — rejected.
- *Triviality*: try to prove the conclusion with the hypotheses deleted. If that succeeds,
  the hypotheses do no work and the statement is weaker than advertised.

## Layout

```
lean/Nullius/Audit.lean   the audit commands (#audit_axioms, #audit_replay, #audit_vacuity,
                         #audit_triviality, #audit_shape), pinned to Lean v4.32.0
lean/lakefile.toml       pins Mathlib, Physlib and the Lean REPL; lake-manifest.json locks all
nullius/config.py         locates the project, records toolchain + Mathlib + Physlib + REPL revs
nullius/repl.py           persistent REPL session and pool
nullius/guard.py          static ban-list, applied before Lean sees the source
nullius/verify.py         the pipeline and the Verdict type
nullius/ledger.py         append-only SQLite record of every verdict
nullius/search.py         Loogle (local or hosted), LeanSearch, and local exact?/apply?
scripts/build-loogle.sh  builds Loogle against our toolchain, for offline shape search
lean/NulliusAll.lean     import-only root whose Loogle index covers Mathlib + Physlib
nullius/cli.py            command-line interface
nullius/harness.py        pooled, thread-safe entry point for programmatic use
nullius/http_server.py    HTTP service for non-Python harnesses
nullius/mcp_server.py     MCP server (stdio, standard library only)
skills/nullius/ the agent skill (pi, Copilot, Claude Code, Codex)
mcp/                     MCP server entry, templated on the repo path
pyproject.toml           packaging; `pip install -e .` gives the `nullius` command
LICENSE                  Apache 2.0, matching Lean, Mathlib, Physlib and Loogle
install.sh               symlinks the skill and merges the MCP entry into place
tests/test_adversarial.py  attacks that must be rejected, proofs that must pass
tests/test_docs.py       re-runs every Lean example in the documentation
tests/test_search.py     local shape search reaches Mathlib and Physlib
tests/test_entrypoints.py  entry points work from an agent's stripped environment
tests/check_names.py     catches renamed tools and superseded revisions in prose
.pre-commit-config.yaml  runs all five before a commit lands (via prek)
AGENTS.md                the contract handed to the agent
COOKBOOK.md              worked patterns for applied mathematics
INTEGRATION.md           how to drive this from a harness
SETUP-AGENTS.md          enabling it in pi / Copilot (skill + MCP)
```

The Lean REPL is a pinned `require` in `lean/lakefile.toml`, so `lake build` fetches it, locks
its revision in `lake-manifest.json`, and guarantees it is built against the same toolchain as
the project — a mismatch there breaks elaboration in ways that are tedious to diagnose.

## Prior art

This is not the first tool in this space, and the soundness layer is not novel:

* [SafeVerify](https://github.com/GasStationManager/SafeVerify) checks Lean submissions
  against a target spec, requires the same three axioms via `CollectAxioms.collect`, bans
  `partial`/`unsafe`, and uses `Environment.replay` to defend against environment
  manipulation. It is the proof-checking backend for PutnamBench's leaderboard and has been
  used to check DeepSeek-Prover-V2, Kimina, Seed-Prover and Trinity output. The kernel replay
  here follows its approach.
* [lean4checker](https://github.com/leanprover/lean4checker) is the kernel re-checker
  SafeVerify is built on.
* [LeanTool](https://github.com/GasStationManager/LeanTool) provides an LLM↔Lean feedback
  loop with MCP/HTTP/CLI interfaces, and its `check_false` tactic — which tries to prove the
  negation of the goal at a `sorry` hole — is the nearest predecessor to the vacuity check.
* [lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp) exposes Lean's LSP, Loogle,
  LeanSearch and friends to agents.

What is unusual here is treating **vacuity and triviality as blocking gates**: rejecting a
proof because its hypotheses are contradictory, or because they do no work. Those checks
matter precisely when the agent writes its *own* statement, which is the case the
submission-against-a-spec tools do not have to handle — when a trusted party pins the
statement in advance, a vacuous statement is the spec author's problem, not the checker's.

If you need to verify submissions against a known specification, use SafeVerify. This tool is
for the case where the claim itself is the thing being invented.

## The pipeline

A submission is accepted only if it survives, in order:

| Check | Rejects |
|---|---|
| `static_guard` | `axiom`, `sorryAx`, `native_decide`, `debug.skipKernelTC`, `unsafe`, `partial def`, `@[implemented_by]`, `@[extern]`, `#exit`, `maxHeartbeats 0`, `#eval`/`run_cmd`, direct environment writes, IO, custom syntax, audit tampering |
| `elaboration` | anything Lean reports an error for |
| `no_sorry` | `sorry` in the target or in any helper it depends on |
| `target_declared` | the claimed theorem does not exist |
| `kernel_replay` | declarations the elaborator accepted but the kernel does not |
| `audit_integrity` | the submission subverted the audit machinery (tripwire) |
| `trusted_axioms` | dependence on anything outside the trusted three |
| `statement_sorry_free` | `sorry` inside the statement itself |
| `not_vacuous` | contradictory hypotheses |
| `hypotheses_used` | hypotheses that do no work (warning by default) |

The layers are deliberately redundant: the test suite disables the guard and confirms the
Lean-side checks still reject every attack unaided.

### Why the kernel replay matters

The axiom audit is only meaningful if the declarations it walks were themselves accepted by
the kernel, and they need not have been. A declaration inserted with `addDeclCore ... false`,
or admitted under `set_option debug.skipKernelTC true`, is in the environment without ever
having been checked. Demonstrated on this machine:

```
theorem forged : 4 = 5     -- installed with doCheck := false, value is `trivial`
#print axioms forged       -- 'forged' does not depend on any axioms
```

A false theorem with a spotless axiom footprint. `kernel_replay` re-adds every declaration
the submission introduced through `Kernel.Environment.addDecl` under a fresh name, and the
kernel rejects it:

```
(kernel) declaration type mismatch, has type True but is expected to have type 4 = 5
```

This is the approach used by [lean4checker](https://github.com/leanprover/lean4checker) and
[SafeVerify](https://github.com/GasStationManager/SafeVerify), which re-check submissions
against the kernel rather than trusting the elaborator's environment. Replay runs *before*
the axiom audit, since the axiom audit's result is worthless otherwise.

### The tripwire

Replay defeats a forged environment, but the audit commands themselves are elaborated in the
environment the submission created, so a submission could redefine `#audit_axioms` to report
success. This was a real hole — the adversarial suite caught it. Each audit run injects a
**canary** built from a `sorry`, alongside the target aliased under an unpredictable random
name, with the order shuffled. Honest audit machinery must flag the canary as untrusted;
machinery rigged to say "trusted" clears the canary too and is caught.

SafeVerify avoids this problem more thoroughly by never entering the submission's environment
at all: it works on `.olean` files in a separate process. That is the stronger design where
latency does not matter. This harness keeps the audit in-process to preserve millisecond
checks, and pays for it with the guard and the tripwire.

## Usage

See **[SETUP-AGENTS.md](SETUP-AGENTS.md)** to enable this in pi or Copilot,
**[INTEGRATION.md](INTEGRATION.md)** for driving it from a harness (Python API, HTTP service,
MCP, CLI), and **[AGENTS.md](AGENTS.md)** for the contract handed to the agent.
**[COOKBOOK.md](COOKBOOK.md)** works through what to check in applied mathematics, and what
not to bother with; every example in it is re-run by `tests/test_docs.py`.

```python
from nullius import Harness

with Harness(pool_size=4).warm() as h:
    v = h.verify("theorem t (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
                 claim="if n > 5 then n squared exceeds 25")
    print(v.statement if v.verified else v.feedback())
```

```bash
nullius doctor                      # check the installation
nullius verify proof.lean -c "..."  # verify a file
nullius statement '(n : Nat) (h : 5 < n) : 25 < n * n'
nullius search 'sum of two even numbers is even'
nullius close '25 < n * n' -b '(n : Nat) (h : 5 < n)'
nullius log --stats

python3 -m nullius.http_server --port 823 --pool 4  # HTTP service
```

### As an MCP server

```json
{
  "mcpServers": {
    "nullius": {
      "command": "python3",
      "args": ["-m", "nullius.mcp_server"],
      "cwd": "/home/jmbr/sources/nullius"
    }
  }
}
```

Tools: `verify`, `statement`, `search`, `close`, `log` — the same names as the CLI
subcommands. Point the agent at `AGENTS.md` for the workflow it should follow.

## Performance

Measured on this machine (24 cores, 62 GB):

| Operation | Time |
|---|---|
| Session start (`import Mathlib` + `import Physlib`) | ~2.6 s, ~0.8 GB resident / ~5.2 GB mapped |
| Verify a simple theorem | 10–20 ms |
| Verify with vacuity + triviality probes | 60–200 ms |
| Rejection by static guard | < 1 ms (no Lean involved) |

Adding Physlib costs almost nothing at startup: oleans are memory-mapped, so pages are only
faulted in as they are used, and a session that never touches physics never pays for it.
Budget by mapped size rather than resident when setting `pool_size`.

The import cost is paid once per process. Every check then branches off that same pristine
environment — which is also a soundness property, not just a speed one: it stops one
submission from leaving definitions behind for the next to exploit.

## Limitations

- **Vacuity detection is sound but incomplete.** It runs a fixed battery of finishing tactics
  (`omega`, `simp_all`, `nlinarith`, `aesop`, …). A subtle contradiction it cannot find will
  pass. A *reported* vacuity is always real; a clean report is evidence, not proof.
- **Statement faithfulness is not automated.** Nothing here can confirm that the Lean
  statement means what the English claim meant. The verdict shows the elaborated statement
  precisely so a human (or a second agent) can compare. This is the residual trust.
- **The trusted computing base** is Lean's kernel, Mathlib, and `Nullius/Audit.lean`.
  Physlib is *not* in it: its incomplete results rest on `sorryAx` or `Lean.ofReduceBool`,
  and the axiom audit rejects anything that reaches them, so importing it cannot weaken a
  verdict — it can only make more things provable.
- **The toolchain is pinned by Physlib, not by us.** Physlib tracks Mathlib about one release
  behind, so the whole graph sits at whatever it supports (currently v4.32.0). Bump both pins
  in `lean/lakefile.toml` together, or not at all; a mismatch fails to resolve.
- **Search backends vary in reach.** Shape search (Loogle) runs against a local index
  covering Mathlib *and* Physlib at the pinned revisions. It is never silently replaced by
  the public service, whose index is a different Mathlib revision without Physlib — that has
  to be requested by name (`--backend loogle-remote`), because a miss against the wrong
  library is indistinguishable from a lemma that does not exist. Natural-language search
  (LeanSearch) is remote either way. `close` works offline and is authoritative.

## Reproducing a verdict

Every ledger row stores the toolchain, Mathlib and Physlib revisions, so a third party can
rebuild the exact environment:

```
toolchain   leanprover/lean4:v4.32.0
mathlib     81a5d257c8e410db227a6665ed08f64fea08e997
physlib     cf1d86d1fbba4fe42ce52577bab8b9df40d83a28
```

The Physlib revision matters more than the other two. Physlib ships results that are
deliberately incomplete, and which ones are complete changes between commits, so "this was
accepted" is only meaningful against a known revision.

## Setup from scratch

### Prerequisites

| | Why | Check |
|---|---|---|
| **elan** | provides `lake` and the pinned Lean toolchain | `lake --version` |
| **Python 3.10+** | the driver | `python3 --version` |
| **git** | fetches Mathlib, Physlib, the REPL and Loogle | `git --version` |
| **~12 GB free** | ~10 GB of built oleans and indexes here, ~2 GB for the toolchain under `~/.elan` | `df -h .` |

Install elan if `lake` is missing (<https://github.com/leanprover/elan>); it downloads the
right Lean version by itself, driven by `lean/lean-toolchain`.

### Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e .   # the driver; no dependencies
```

You never have to activate that virtualenv. `./install.sh` below symlinks the console script
into `~/.local/bin`, and wires the skill and the MCP server to the interpreter inside it by
absolute path — so `nullius` works from any shell, and an agent that launches the server with
a stripped environment gets a working verifier rather than an import error.

An editable install from a checkout is the supported arrangement, and the only one that
works: this package is a driver for a Lean project of several gigabytes that has to be built
here, and it finds that project relative to its own source file. A copied install would look
for `lean/` inside `site-packages`, and fails with a message saying so. For the same reason
there is nothing on PyPI to install: the Python is a few thousand lines of stdlib, and
everything that gives a verdict its weight is the Lean tree beside it, pinned to exact
revisions.

If you would rather not install at all, every entry point still works from the repository
root (`python3 -m nullius.cli ...`), and `skills/nullius/scripts/nullius` works from
anywhere regardless — it prefers `.venv` and falls back to the source tree.

### Build the Lean side

```bash
cd lean
lake exe cache get && lake build     # Mathlib (~3.6 GB, cached) + the audit module
lake build repl                      # the REPL, pinned by lake-manifest.json
lake build Physlib                   # physics; builds from source, ~15 min
cd .. && ./scripts/build-loogle.sh   # shape search, offline (~15 s)
```

### Check it, and enable it

```bash
./install.sh                # `nullius` on PATH, plus the skill and MCP server
nullius doctor              # before install.sh: .venv/bin/nullius doctor
```

`doctor` verifies a genuine proof, a `sorry` proof and a vacuous one, so it fails loudly if
the verifier has stopped discriminating.

### Contributing

```bash
.venv/bin/pip install -e ".[dev]"    # adds prek
prek install                         # run the checks before each commit
python3 tests/test_adversarial.py    # or run them directly
python3 tests/test_docs.py
```

`lake exe cache get` only serves Mathlib, so Physlib compiles locally the first time.

`scripts/build-loogle.sh` is what makes shape search work. Loogle has no dependencies and does
not need to be one of ours: the binary reads the `.olean` files of any Lake project built
with the same toolchain, so the script clones it into `vendor/`, copies our `lean-toolchain`
over its own, and builds — about 15 seconds, with nothing of Mathlib rebuilt. Shape search
then runs offline, covers Physlib as well as Mathlib, and answers from the exact revisions
pinned here rather than whatever the hosted service last deployed. The first query builds a
374 MB index (~2 min, cached beside the oleans and rebuilt automatically when they change);
pass `--index` to pay that cost up front instead. Skip the script and shape search reports
that it has no index, rather than quietly answering from the public service — that one is
still there, but you have to ask for it: `nullius search --backend loogle-remote`.

`prek install` wires the checks into `git commit`: prose is scanned for renamed tools and
superseded revisions on every commit (72 ms, no Lean), and the two Lean suites run only when
something they cover actually changes — about 12 s for a commit that touches everything. Use
`prek run --all-files` to check the whole tree, and `SKIP=nullius-docs git commit` to bypass
a hook deliberately. `pre-commit` reads the same config if you prefer it to `prek`.

## Licence and provenance

Apache 2.0 — see [LICENSE](LICENSE). That matches every component this sits on: Lean 4,
Mathlib, Physlib and Loogle are all Apache 2.0, so there is no licence seam anywhere in the
dependency graph, and a result proved here can go upstream to Mathlib without relicensing.

Much of this repository was written by coding agents working under human direction and
review; the `Co-authored-by` trailers in the git history record which commits, and the
session that produced them. Saying so is not a disclaimer, it is the same principle the tool
applies to mathematics: a claim about provenance should be checkable rather than taken on
trust. Judge the code by the audit it survives — `tests/test_adversarial.py` is the argument,
not the authorship.

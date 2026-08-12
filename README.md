# lean-ai — making an agent back up its claims

A verification harness that lets an LLM agent prove its mathematical assertions in Lean 4 +
Mathlib, and that refuses to accept the proof unless it is actually worth something.

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
lean/LeanAI/Audit.lean   the audit commands (#audit_axioms, #audit_vacuity,
                         #audit_triviality, #audit_shape), pinned to Lean v4.33.0
repl/                    leanprover-community/repl, checked out at the commit whose
                         toolchain matches (bbeedf3)
leanai/config.py         locates the project, records toolchain + Mathlib revision
leanai/repl.py           persistent REPL session and pool
leanai/guard.py          static ban-list, applied before Lean sees the source
leanai/verify.py         the pipeline and the Verdict type
leanai/ledger.py         append-only SQLite record of every verdict
leanai/search.py         Loogle, LeanSearch, and local exact?/apply?
leanai/cli.py            command-line interface
leanai/harness.py        pooled, thread-safe entry point for programmatic use
leanai/http_server.py    HTTP service for non-Python harnesses
leanai/mcp_server.py     MCP server (stdio, standard library only)
skills/lean-proof-check/ the agent skill (pi, Copilot, Claude Code, Codex)
mcp/                     MCP server entry, templated on the repo path
install.sh               symlinks the skill and merges the MCP entry into place
tests/test_adversarial.py  attacks that must be rejected, proofs that must pass
AGENTS.md                the contract handed to the agent
INTEGRATION.md           how to drive this from a harness
SETUP-AGENTS.md          enabling it in pi / Copilot (skill + MCP)
```

## The pipeline

A submission is accepted only if it survives, in order:

| Check | Rejects |
|---|---|
| `static_guard` | `axiom`, `sorryAx`, `native_decide`, `debug.skipKernelTC`, `unsafe`, `partial def`, `@[implemented_by]`, `@[extern]`, `#exit`, `maxHeartbeats 0`, `#eval`, IO, custom syntax, audit tampering |
| `elaboration` | anything Lean reports an error for |
| `no_sorry` | `sorry` in the target or in any helper it depends on |
| `target_declared` | the claimed theorem does not exist |
| `audit_integrity` | the submission subverted the audit machinery (tripwire) |
| `trusted_axioms` | dependence on anything outside the trusted three |
| `statement_sorry_free` | `sorry` inside the statement itself |
| `not_vacuous` | contradictory hypotheses |
| `hypotheses_used` | hypotheses that do no work (warning by default) |

The static guard and the axiom audit are deliberately redundant: the test suite disables the
guard and confirms the Lean-side audit still rejects every attack unaided.

### The tripwire

The audit commands necessarily run inside the environment the submission created, so a
submission could in principle redefine `#audit_axioms` to report success. This was a real
hole — the adversarial suite caught it. The fix: each audit run injects a **canary** built
from a `sorry`, alongside the target aliased under an unpredictable random name. Honest audit
machinery must flag the canary as untrusted; machinery rigged to say "trusted" clears the
canary too and is caught. Names are random and the order is shuffled, so the forgery cannot
special-case its way out.

## Usage

See **[SETUP-AGENTS.md](SETUP-AGENTS.md)** to enable this in pi or Copilot,
**[INTEGRATION.md](INTEGRATION.md)** for driving it from a harness (Python API, HTTP service,
MCP, CLI), and **[AGENTS.md](AGENTS.md)** for the contract handed to the agent.

```python
from leanai import Harness

with Harness(pool_size=4).warm() as h:
    v = h.verify("theorem t (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
                 claim="if n > 5 then n squared exceeds 25")
    print(v.statement if v.verified else v.feedback())
```

```bash
python3 -m leanai.cli doctor                      # check the installation
python3 -m leanai.cli verify proof.lean -c "..."  # verify a file
python3 -m leanai.cli statement '(n : Nat) (h : 5 < n) : 25 < n * n'
python3 -m leanai.cli search 'sum of two even numbers is even'
python3 -m leanai.cli goal '25 < n * n' -b '(n : Nat) (h : 5 < n)'
python3 -m leanai.cli log --stats

python3 -m leanai.http_server --port 823 --pool 4  # HTTP service
```

### As an MCP server

```json
{
  "mcpServers": {
    "leanai": {
      "command": "python3",
      "args": ["-m", "leanai.mcp_server"],
      "cwd": "/home/jmbr/sources/lean-ai"
    }
  }
}
```

Tools: `lean_verify`, `lean_check_statement`, `lean_search_lemma`, `lean_find_proof`,
`lean_ledger`. Point the agent at `AGENTS.md` for the workflow it should follow.

## Performance

Measured on this machine (24 cores, 62 GB):

| Operation | Time |
|---|---|
| Session start (`import Mathlib`) | ~2.5 s, ~7 GB resident |
| Verify a simple theorem | 10–20 ms |
| Verify with vacuity + triviality probes | 60–200 ms |
| Rejection by static guard | < 1 ms (no Lean involved) |

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
- **The trusted computing base** is Lean's kernel, Mathlib, and `LeanAI/Audit.lean`.
- **Remote search backends** (Loogle, LeanSearch) are external services; `lean_find_proof`
  works offline and is authoritative.

## Reproducing a verdict

Every ledger row stores the toolchain and Mathlib revision, so a third party can rebuild the
exact environment:

```
toolchain   leanprover/lean4:v4.33.0
mathlib     db584cd6d46c92f209a44c0f1c829460d327499d
```

## Setup from scratch

```bash
cd lean && lake exe cache get && lake build     # Mathlib (~3.6 GB cached)
cd ../repl && lake build                        # REPL, toolchain must match lean/
cd .. && python3 -m leanai.cli doctor
python3 tests/test_adversarial.py
./install.sh                                    # enable the skill and MCP server
```

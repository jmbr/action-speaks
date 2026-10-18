# Compatibility

This file records the client and dependency versions tested with each action-speaks release.
Combinations not listed here may work, but have not been tested.

## v0.3.0

| Component | Version |
|---|---|
| Python | 3.12.3 |
| Lean | 4.34.0 |
| Mathlib | 5ed296525643 |
| Physlib | d410e856abdd |
| Cslib | 990e65a685be |
| Lean REPL | 193cf4bb9a22 |
| Loogle | 9f11169aaebf |
| GitHub Copilot CLI | 1.0.98 |
| pi | 1.3.0 |

The checkout was built as described in the [installation guide](INSTALL.md) and installed
with `python3 install.py`, on Linux.

### Command line

| Check | Result |
|---|---|
| `action-speaks doctor` | Genuine proof verified; `sorry` and vacuous proofs rejected |
| Local Loogle | `search '\|- Irrational (Real.sqrt _)' --backend loogle` returned 6 Mathlib hits |

### GitHub Copilot CLI

Skill and MCP server installed by `install.py`. Run with `copilot -p ... --allow-all-tools`
from an empty directory, so no repository instructions were loaded.

| Check | Result |
|---|---|
| Skill routing | "Is it true that for every natural number n greater than 5, n squared exceeds 25?" triggered `skill(action-speaks)` |
| MCP tool calls | The agent called `statement`, then `verify`, which returned `VERIFIED` |

`tests/skill_trigger_eval.sh` measures routing over a wider set of prompts.

### pi

The skill was loaded explicitly with `pi --print "/skill:action-speaks ..."` from an empty
directory. Asked whether 2^n > n^3 for all n ≥ 10, the agent reported a machine-checked
result for `∀ (n : ℕ), 10 ≤ n → n ^ 3 < 2 ^ n`.

Automatic skill routing in pi, and MCP through a pi adapter, were not tested.

## Not tested

macOS, Windows, the HTTP server with external clients, VS Code, Claude Code, Codex, and
the Copilot cloud agent. Supporting MCP or Agent Skills in principle does not replace
client-specific testing.

# action-speaks — check an agent's mathematical claims

action-speaks checks Lean 4 proofs written by AI agents. It uses Mathlib for mathematics,
Physlib for physics, and Cslib for computer science. You can call it from Python, the
command line, an HTTP service, or an MCP tool.

It checks more than whether a proof compiles: it rejects incomplete proofs and untrusted
axioms, and looks for contradictory or unnecessary assumptions. **You still need to check
that the Lean statement matches the claim you intended.**

## Start here

| Task | Guide |
|---|---|
| Install and build action-speaks | [Installation guide](docs/INSTALL.md) |
| Enable it in an agent | [Agent setup](docs/SETUP-AGENTS.md) |
| Use Python, HTTP, MCP, or the CLI | [Integration guide](docs/INTEGRATION.md) |
| Work through mathematical examples | [Cookbook](docs/COOKBOOK.md) |
| Follow the agent workflow | [AGENTS.md](AGENTS.md) |
| Keep proofs in a Lean project | [Project-backed proofs](docs/PROJECTS.md) |
| Recall proofs or reproduce a verdict | [Ledger and reproducibility](docs/LEDGER.md) |
| Develop or release action-speaks | [Contributing guide](CONTRIBUTING.md) |

Once installed:

```bash
action-speaks doctor
action-speaks statement '(n : Nat) (h : 5 < n) : 25 < n * n'
action-speaks search 'sum of two even numbers is even'
action-speaks close '25 < n * n' -b '(n : Nat) (h : 5 < n)'
action-speaks verify proof.lean -c "My claim" --require-nontrivial
action-speaks log --stats
```

Starting Lean takes seconds. Run the daemon once and the commands above answer in
milliseconds:

```bash
action-speaks serve --install     # a systemd user service, or a launchd agent on macOS
action-speaks serve               # or just run it in the foreground
```

For repeated checks from Python, keep a session open:

```python
from action_speaks import Harness

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
useful while developing a proof, but they are not evidence for a claim. action-speaks checks the
proof's axiom dependencies and allows only `propext`, `Classical.choice`, and `Quot.sound`.

A complete proof can also prove the wrong thing. action-speaks runs two additional checks:

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
kernel check. action-speaks therefore rechecks eligible local declarations with the kernel before
examining their axiom dependencies.

The audit also runs in the submission's environment, where modified audit commands could
report false results. To detect this, action-speaks adds a known-bad proof, called a *canary*,
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

## Related tools

- [SafeVerify](https://github.com/GasStationManager/SafeVerify) checks proofs against a supplied
  specification. Its kernel-replay approach informed this project.
- [lean4checker](https://github.com/leanprover/lean4checker) rechecks declarations with Lean's kernel.
- [LeanTool](https://github.com/GasStationManager/LeanTool) provides a Lean feedback loop and
  contradiction checks.
- [lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp) exposes Lean's language server and search tools.

action-speaks focuses on claims whose statements are written by the agent, rather than supplied
as a fixed specification.

## License and authorship

Apache 2.0; see [LICENSE](LICENSE). Lean, Mathlib, Physlib, Cslib, and Loogle also use Apache 2.0.
Coding agents contributed under human direction and review; commit trailers identify
co-authored commits.

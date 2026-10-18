# AGENTS.md

This file is for agents working on action-speaks itself. To *use* action-speaks for checking
claims, follow the [agent workflow](docs/AGENT-WORKFLOW.md) or install the skill in
`skills/action-speaks/`. [CONTRIBUTING.md](CONTRIBUTING.md) covers setup, commit messages,
and releases in more detail.

## Layout

| Path | What lives there |
|---|---|
| `lean/ActionSpeaks/Audit.lean` | Lean audit commands: the authoritative soundness check |
| `action_speaks/guard.py` | Source-level refusals for what the audit cannot see |
| `action_speaks/verify.py` | Verification pipeline and verdicts |
| `action_speaks/repl.py`, `action_speaks/config.py` | Lean processes, session pool, paths |
| `action_speaks/ledger*.py`, `action_speaks/search.py` | Stored results, recall, lemma search |
| `action_speaks/cli.py`, `mcp_server.py`, `http_server.py` | Entry points over `harness.py` |
| `skills/action-speaks/` | The Agent Skill, and its reference guide |
| `tests/` | The pytest suite; `lean`-marked tests need a built checkout |
| `tests/skill_trigger_eval.sh` | Does the skill fire on the right prompts? Drives an agent CLI, so pytest does not run it |
| `noxfile.py` | Every check, defined once |

## Checks

```bash
.venv/bin/nox -s quick                 # Lean-free tests, about a second
.venv/bin/nox -s lint format types     # seconds
.venv/bin/nox -s skills                # validate skills/ and scan it with SkillSpector
.venv/bin/nox -s tests                 # everything; minutes, and needs lean/ built
.venv/bin/nox -s fix                   # apply what `format` asks for
.venv/bin/nox -s tests -- -k ledger    # arguments after -- go to pytest
```

`prek install` wires these sessions to git hooks: hygiene, secret scanning, `quick`, and
`skills` on commit; the Conventional Commits check on the message; the default nox sessions
on push.

Define a new check as a nox session and call it from `.pre-commit-config.yaml`; do not
write it in the hook file. Only checks with no nox equivalent (file hygiene, commit messages,
Gitleaks) stay as pinned upstream hooks.

## Invariants

- **Lean's kernel decides; Python never does.** Python may refuse source or report what
  Lean found, but never conclude that a proof is valid on its own.
- **Refuse, never rewrite.** `guard.py` rejects dangerous source. Silently editing a proof
  produces a verdict about a program nobody wrote.
- **No runtime dependencies.** `dependencies = []` keeps the trusted base to Lean, the
  libraries, and `Audit.lean`. Development tools belong in the `dev` extra.
- **Install editable.** The package locates `lean/` relative to its own source, so a
  non-editable copy finds nothing.
- **The ledger is append-only.** Re-verifying appends a row; nothing updates or deletes one.
- **CLI subcommands and MCP tools share names.** A workflow written for one transfers to
  the other unchanged.
- **stdout belongs to the MCP protocol.** In `mcp_server.py`, diagnostics go to stderr.
- **Documented examples are tested.** Heredoc proofs in the skill docs must verify, and
  cookbook blocks carry an `-- expect:` line. `tests/test_names.py` rejects retired names
  and Lean versions or library revisions that differ from the pins.
- **Tests that start Lean carry the `lean` marker**, or `nox -s quick` stops being quick.

## Changing the skill

The name "action-speaks" says nothing about mathematics, so the `description` in
`skills/action-speaks/SKILL.md` does all the routing. After changing it, run
`tests/skill_trigger_eval.sh` (main set, then holdout) and compare the pass counts with the
previous description.

## Conventions

- Tests are named as sentences stating the claim
  (`test_console_script_runs_without_a_virtualenv`).
- Comments explain why, not what.
- ruff with `line-length = 100` decides formatting; do not hand-format around it.
- Use American spelling in code, comments, and documentation.
- Commit subjects are Conventional Commits; the `commit-msg` hook rejects anything else.
  Keep the body short: why the change is right.

## Releases

On every push to `main`, python-semantic-release derives the version from the commit history.
**It writes the version, tag, and `CHANGELOG.md`; never edit them by hand.** Commitizen only
validates messages and must never bump.

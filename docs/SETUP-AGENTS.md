# Enabling the verifier in an agent

Everything needed lives in this repository:

```
skills/nullius/              the skill (SKILL.md, references/, scripts/)
mcp/copilot-mcp-config.json  MCP server entry, with the repo path templated
install.sh                   symlinks the skill and the CLI, merges the MCP entry
```

```bash
./install.sh              # skill + `nullius` on PATH + MCP
./install.sh --dry-run    # show what would change
./install.sh --skill      # skill only (e.g. for pi, which has no MCP)
./install.sh --cli        # just put `nullius` on PATH
./install.sh --uninstall  # remove exactly what it installed
```

Nothing here requires activating a virtualenv. Every path installed is absolute: the MCP
entry names the interpreter the package was installed into, the skill wrapper resolves the
repository through its own symlink, and `~/.local/bin/nullius` is a generated wrapper that
hard-codes the interpreter. Agents commonly launch tools with a stripped environment — no
profile, no PATH to `~/.elan/bin`, no virtualenv — and that case is tested.

The skill is **symlinked, not copied**, so editing it here takes effect immediately and there
is only ever one copy. The MCP entry is **merged** into any existing `mcp-config.json`
(backing the file up first), so other servers you have configured are preserved.

## Skill or MCP?

Two mechanisms, and they are not alternatives — use both where you can.

| | Skill | MCP server |
|---|---|---|
| Tells the agent **when** and **why** to verify | yes | barely (tool descriptions only) |
| Gives the agent **tools** to call | via shell script | yes, natively |
| Works in pi | yes | **no** — pi has no MCP support, by design |
| Works in Copilot CLI | yes | yes |
| Cost when unused | ~2 lines in the prompt | a process + tool schemas in context |

The skill is what makes an agent *reach for* verification unprompted; the MCP server is the
lower-latency way to actually call it.

A project-level `AGENTS.md` is **not** a substitute. It only applies when the agent is working
inside this repository, and the entire point is to verify claims made anywhere.

## The skill

Installed to `~/.agents/skills/nullius` — a directory both pi and Copilot scan, so a
single symlink serves both.

```
skills/nullius/
├── SKILL.md               # frontmatter + workflow (only the description is always loaded)
├── references/GUIDE.md    # failure catalogue, worked examples (loaded on demand)
└── scripts/nullius         # wrapper; resolves the repo through the symlink, runs from any cwd
```

Only the `description` sits in the system prompt, which is why it enumerates concrete triggers
(inequality, bound, closed form, convergence, termination, counterexample) rather than saying
"helps with math". The body loads when a task matches.

Check that pi sees it:

```bash
pi --print "/skill:nullius"
```

### Other harnesses

Claude Code and Codex read their own directories:

```bash
ln -s ~/sources/nullius/skills/nullius ~/.claude/skills/nullius
```

Or point pi's settings at them: `{ "skills": ["~/.claude/skills", "~/.codex/skills"] }`.

## The MCP server (Copilot)

`install.sh` writes this into `~/.copilot/mcp-config.json`, substituting the repository path:

```json
{
  "mcpServers": {
    "nullius": {
      "type": "local",
      "command": "/path/to/nullius/.venv/bin/python3",
      "args": ["-m", "nullius.mcp_server"],
      "cwd": "/path/to/nullius",
      "env": {"PYTHONUNBUFFERED": "1"},
      "tools": ["*"]
    }
  }
}
```

`install.sh` writes this for you, pointing `command` at the interpreter the package was
installed into so the server cannot be caught out by whatever `python3` means in the agent's
environment. Without a `.venv` it falls back to a bare `python3` with `PYTHONPATH` set.

Tools: `verify`, `statement`, `search`, `close`, `log` — the same names as the CLI
subcommands. Restart Copilot after installing.

The server keeps one warm Lean session per process, so the first call pays ~2.6 s for the
Mathlib, Physlib and Cslib imports and the rest are milliseconds — a real advantage over the skill's shell path,
which starts a fresh session per invocation.

**pi deliberately has no MCP support** ("It intentionally does not include built-in MCP,
sub-agents, permission popups..."). Under pi the skill's `scripts/nullius` wrapper is the
transport. For heavy pi use, write a pi extension wrapping `nullius.Harness` to keep sessions
warm.

## Verified working

Copilot MCP, spawned exactly as configured, from an unrelated cwd:

```
initialize: {'name': 'nullius', 'version': '0.1.0'} 2024-11-05
tools: ['verify', 'statement', 'search', 'close', 'log']
verify -> VERIFIED: t | ∀ (n : ℕ), 5 < n → 25 < n * n
preflight -> CONTRADICTORY
```

pi (`deepseek-v4-flash:cloud`), asked to verify "for every real x, x² + 1 > 0", read the
skill, hit two rejections, repaired, and reported:

> **Verdict: VERIFIED** — `∀ (x : ℝ), x * x + 1 > 0` … **Honest note on the path there:** two
> naive attempts were *rejected* first — `positivity` alone failed to close the goal, and
> `sq_nonneg x` alone didn't match because it produces `0 ≤ x ^ 2` while the goal has `x * x`.

The ledger independently corroborates that account — entries #24 and #25 failed
`elaboration`, #26 verified. That is the property worth having: the agent's narrative of its
own work is checkable against a record it does not write.

## Choosing per harness

- **Copilot:** both. MCP for speed, skill for judgement about when to verify.
- **pi:** skill only.
- **Batch / CI / RL:** neither — import `nullius.Harness` directly and keep a warm pool. See
  `INTEGRATION.md`.

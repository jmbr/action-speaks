# Set up nullius for an agent

First [install and build nullius](../README.md#setup-from-scratch). Then run:

```bash
./install.sh --dry-run    # Preview changes
./install.sh              # Install the command, skill, and Copilot MCP entry
nullius doctor
```

Restart Copilot after installing the MCP entry.

## Choose what to install

| Component | Purpose |
|---|---|
| Skill | Teaches the agent when to verify and how to interpret results |
| MCP server | Lets the agent call nullius tools directly and reuse a Lean session |
| CLI | Runs checks from a shell |

Use both the skill and MCP where supported. For agents without MCP, such as the pi setup
described here, use the skill's shell wrapper. For a Python application or batch job, use
[`Harness`](INTEGRATION.md#python) directly.

You can install components separately:

```bash
./install.sh --skill
./install.sh --cli
./install.sh --uninstall
```

The installer links the skill rather than copying it, so edits in this checkout take effect
immediately. It backs up and merges Copilot's MCP configuration, preserving other servers.
Installed commands use the checkout's interpreter; you do not need to activate a virtualenv.

## Skill

The skill is installed at `~/.agents/skills/nullius`, which pi and Copilot CLI can discover.

```text
skills/nullius/
├── SKILL.md               # When to use nullius and the verification workflow
├── references/GUIDE.md    # Error reference and worked examples
└── scripts/nullius        # Shell wrapper
```

The skill description tells the agent which tasks should trigger verification. The full
instructions are loaded when the skill is used. Unlike this repository's `AGENTS.md`, the
installed skill can be used from other projects.

To check discovery in pi:

```bash
pi --print "/skill:nullius"
```

For clients using a different skill directory, link the same source there. For example:

```bash
mkdir -p ~/.claude/skills
ln -s /path/to/nullius/skills/nullius ~/.claude/skills/nullius
```

pi can also read additional directories through its settings:

```json
{"skills": ["~/.claude/skills", "~/.codex/skills"]}
```

### Shell wrapper syntax

The skill wrapper resolves the checkout through its symlink, so it works from any directory:

```bash
~/.agents/skills/nullius/scripts/nullius verify "My claim" < proof.lean
```

This differs from the installed CLI:

```bash
nullius verify proof.lean -c "My claim"
```

Both use the same verifier and configured ledger. Each shell invocation starts a new Lean
session; use MCP or the Python API for repeated checks.

## MCP server for Copilot

`install.sh` adds this entry to `~/.copilot/mcp-config.json`, using your checkout's paths:

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

Without `.venv`, the installer uses `python3` and sets `PYTHONPATH` to the checkout.
The server exposes `verify`, `statement`, `search`, `close`, and `log`. It keeps a Lean
session open to avoid reloading the libraries for every request.

## Troubleshooting

Run `nullius doctor` to check paths, library versions, and basic verification behavior.
If it fails, use its error message to identify the missing build or configuration.
`NULLIUS_ROOT` can point the wrapper at a different built checkout.

If an agent cannot find the tools, check its skill directory or MCP configuration and restart
the client. See the [integration guide](INTEGRATION.md) for direct Python, HTTP, and CLI use.

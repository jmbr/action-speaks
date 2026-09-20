# Set up action-speaks for an agent

The [installation guide](INSTALL.md) prepares one checkout. This guide connects that built
checkout to agent clients on the current machine. After building, run:

```bash
python3 install.py --dry-run    # Preview changes
python3 install.py              # Install the command, skill, and Copilot MCP entry
action-speaks doctor
```

Restart Copilot after installing the MCP entry.

## Choose what to install

| Component | Purpose |
|---|---|
| Skill | Teaches the agent when to verify and how to interpret results |
| MCP server | Lets the agent call action-speaks tools directly and reuse a Lean session |
| CLI | Runs checks from a shell |

Use both the skill and MCP where supported. For agents without MCP, such as the pi setup
described here, the skill instructs the agent to run the `action-speaks` command. For a
Python application or batch job, use [`Harness`](INTEGRATION.md#python) directly.

You can install components separately:

```bash
python3 install.py --skill
python3 install.py --cli
python3 install.py --uninstall
```

The installer links the skill rather than copying it, so edits in this checkout take effect
immediately. It backs up and merges Copilot's MCP configuration, preserving other servers.
Installed commands use the checkout's interpreter; you do not need to activate a virtualenv.

## Skill

The skill is installed at `~/.agents/skills/action-speaks`, which pi and Copilot CLI can discover.

```text
skills/action-speaks/
├── SKILL.md               # When to use action-speaks and the verification workflow
└── references/GUIDE.md    # Error reference and worked examples
```

The skill description tells the agent which tasks should trigger verification. The full
instructions are loaded when the skill is used. Unlike this repository's `AGENTS.md`, the
installed skill can be used from other projects.

To check discovery in pi:

```bash
pi --print "/skill:action-speaks"
```

For clients using a different skill directory, link the same source there. For example:

```bash
mkdir -p ~/.claude/skills
ln -s /path/to/action-speaks/skills/action-speaks ~/.claude/skills/action-speaks
```

pi can also read additional directories through its settings:

```json
{"skills": ["~/.claude/skills", "~/.codex/skills"]}
```

### Running checks

The skill tells the agent to run the installed command, which works from any directory:

```bash
action-speaks verify proof.lean -c "My claim"
printf '%s\n' "$SRC" | action-speaks verify - -c "My claim"
```

Each invocation starts a new Lean session unless the daemon is running; see
[the session daemon](INTEGRATION.md#session-daemon).

## MCP server for Copilot

`install.py` adds this entry to `~/.copilot/mcp-config.json`, using your checkout's paths:

```json
{
  "mcpServers": {
    "action-speaks": {
      "type": "local",
      "command": "/path/to/action-speaks/.venv/bin/python3",
      "args": ["-m", "action_speaks.mcp_server"],
      "cwd": "/path/to/action-speaks",
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

Run `action-speaks doctor` to check paths, library versions, and basic verification behavior.
If it fails, use its error message to identify the missing build or configuration.
`ACTION_SPEAKS_ROOT` can point the command at a different built checkout.

If an agent cannot find the tools, check its skill directory or MCP configuration and restart
the client. See the [integration guide](INTEGRATION.md) for direct Python, HTTP, and CLI use.

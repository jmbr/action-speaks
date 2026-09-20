# Using nullius in an application

| Interface | Best for | Lean session lifetime |
|---|---|---|
| Python `Harness` | Python applications and batches | Reused until the harness closes |
| HTTP | Applications in other languages | Reused by the server |
| MCP | Agents calling tools during a conversation | Reused by the server |
| CLI | One-off checks and shell scripts | New session for each invocation |

Loading the libraries takes several seconds. Use a persistent interface for repeated checks
instead of starting the CLI in a loop.

All interfaces require a built checkout. Follow [Setup from scratch](../README.md#setup-from-scratch),
including the editable Python install:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Python

```python
from nullius import Harness

with Harness(pool_size=1).warm() as h:
    v = h.verify(
        "theorem main (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
        claim="if n > 5 then n squared exceeds 25",
        require_nontrivial=True,
    )
    if v.verified:
        print(v.statement)
    else:
        print(v.feedback())
```

`Verdict` includes the status, elaborated statement, checks, axioms, library revisions, and
elapsed time. Use `to_dict()` or `to_json()` for structured output, `render()` for a summary,
and `feedback()` for suggested repairs.

The following snippets assume `h` is still inside its `with Harness(...)` block.

### Batches

```python
verdicts = h.verify_many([
    {"source": src_a, "claim": "...", "require_nontrivial": True},
    {"source": src_b, "claim": "...", "require_nontrivial": True},
])
```

Results keep the input order. Increase `pool_size` to allow concurrent checks; each active
check uses one Lean session.

### Repair loops

Provide a function that generates Lean source from a prompt and any previous feedback:

```python
def propose(feedback, round):
    prompt = base_prompt
    if feedback is not None:
        prompt += f"\n\nPrevious attempt failed:\n{feedback}"
    return model.generate(prompt)

verdict, history = h.prove(
    propose, claim="...", max_rounds=3, require_nontrivial=True
)
print(f"{len(history)} round(s) -> {verdict.status}")
```

The loop stops on success or after `max_rounds`. A failed tactic calls for another proof
attempt; contradictory assumptions call for a revised statement.

### Statement checks and search

```python
info = h.check_statement("(n : Nat) (h1 : n > 5) (h2 : n < 3) : n = 42")
if not info["ok"]:
    print(info)
elif info["vacuous"]:
    print("Review the assumptions:", info["vacuity_witness"])

h.search("sum of two even numbers is even")
h.search("Real.sqrt, |- _ ≤ _", backend="ledger")
h.search("Real.sqrt, |- _ ≤ _", backend="loogle", include_ledger=True)
h.find_proof("25 < n * n", binders="(n : Nat) (h : 5 < n)")
```

Ledger shape search is opt-in. Add `refresh_ledger=True` to explicitly recheck and build
its cache; see [ledger search](#optional-local-ledger-search) below.

`check_statement` also returns related ledger entries in `recall` when logging is enabled.
To look up stored work directly:

```python
for hit in h.recall(text="gradient descent step size"):
    print(hit.render())
```

Matches are suggestions for reuse, not new verification results. Compare the statements and
re-verify the stored source.

### Training or ranking proof attempts

You can use `v.verified` as a success signal and `v.checks` for diagnostics. If you assign
partial credit for passed checks, treat it as a training heuristic, not evidence of a proof.
Keep `require_nontrivial=True`, and check the statement against the intended task: even a
verified theorem can be irrelevant to that task.

## HTTP

```bash
python3 -m nullius.http_server --port 8823 --pool 2
```

```bash
curl -s localhost:8823/verify -H 'Content-Type: application/json' \
  -d '{"source":"theorem t (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
       "claim":"if n > 5 then n squared exceeds 25",
       "require_nontrivial":true}'
```

The response contains the verdict fields, plus `feedback` and `render`. For example, these
are selected fields from a successful response:

```json
{
  "verified": true,
  "status": "verified",
  "statement": "∀ (n : ℕ), 5 < n → 25 < n * n",
  "axioms": ["propext", "Classical.choice", "Quot.sound"]
}
```

| Endpoint | Purpose |
|---|---|
| `GET /health` | Server configuration and library revisions |
| `GET /ledger?limit=20` | Recent attempts and ledger statistics |
| `GET /ledger/123` | Read one entry, including its source and target |
| `POST /verify` | Check one proof |
| `POST /verify_many` | Check an `items` array of proof submissions |
| `POST /statement` | Check a statement and return related earlier work |
| `POST /search` | Search for lemmas |
| `POST /find_proof` | Ask Lean for a tactic for a goal |

The server binds to localhost by default and has no authentication. Lean processes
submitted source; the verifier is not a security sandbox. Keep the service off untrusted
networks.

## MCP

```json
{
  "mcpServers": {
    "nullius": {
      "command": "/path/to/nullius/.venv/bin/python3",
      "args": ["-m", "nullius.mcp_server"],
      "cwd": "/path/to/nullius"
    }
  }
}
```

The tools are `verify`, `statement`, `search`, `close`, and `log`. Give the agent the
[workflow instructions](../AGENTS.md), or install the skill as described in
[agent setup](SETUP-AGENTS.md).

Requests are handled **one at a time**: the server reads a line from stdin, answers it, and
only then reads the next. So a long verification delays whatever the agent asks for next, and
`NULLIUS_POOL_SIZE` does not change that — a single server has no pool of its own. That is
the current behavior, not a commitment; making one server concurrent would change how it owns
its work, and is not planned here.

Each client starts its own server process, so **run the [session daemon](#session-daemon)**
if more than one agent uses this machine. With it running, the servers send their checking to
it and start no Lean process of their own; without it, each holds its own session and its own
copy of the libraries, which is several gigabytes each. `NULLIUS_NO_DAEMON=1` forces the work
back into the server process.

## CLI

```bash
nullius verify proof.lean -c "claim" --json --require-nontrivial
printf '%s\n' "$SRC" | nullius verify - --json
nullius log --recall 'gradient descent step size'
```

For `verify`, exit code 0 means verified, 1 means not verified, and 2 indicates a
configuration or command-line error. Use `--no-log` to avoid reading or writing the ledger.

The installed `nullius` command takes a **file path** after `verify`; use `-` for stdin.
The skill's `scripts/nullius` wrapper has different syntax: it takes a claim and reads the
proof from stdin, or from `-f FILE`.

## Session daemon

Starting Lean loads the libraries and takes seconds. `nullius serve` keeps warm sessions in
one process per user and per checkout, and the CLI uses it when it is running:

```bash
nullius serve                 # foreground, Ctrl-C to stop
nullius serve --install       # write the service files for this platform
nullius serve --print-units   # show them without writing anything
```

With the daemon running, `verify`, `statement` and `close` send their work to it instead of
starting Lean; without it they behave exactly as before. `--no-daemon`, or
`NULLIUS_NO_DAEMON=1`, forces the work into the calling process, which is what you want when
reproducing a verdict against a known checkout.

The daemon does the checking only. It writes no ledger entries: the caller records the
verdict, with its own tag and project, into its own ledger.

### Where the socket lives

The socket goes in `$XDG_RUNTIME_DIR/nullius/<checkout>.sock`. That variable is the one in
the XDG specification with no defined default, because its guarantees — owned by you, mode
0700, lifetime bound to the login session — cannot be created by an application. It is
honored wherever it is set, and on Linux it is `pam_systemd` that sets it, so it is missing
under `sudo -i`, under cron, and in minimal containers as well as on other systems. When it
is absent nullius falls back to a private `TMPDIR`, then to `$XDG_CACHE_HOME/nullius/run`,
and says so, as the specification asks. `NULLIUS_SOCKET` overrides the choice; a path over
104 bytes is rejected, since that is the shorter of the two platform limits.

The socket is named after the checkout, so two checkouts never answer for one another. That
matters because a verdict records the toolchain and library revisions it was checked
against.

### Running it as a service

`nullius serve --install` writes a systemd user unit on Linux or a launchd agent on macOS,
pins `NULLIUS_ROOT` and the checkout's own interpreter, and prints the commands to enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now nullius-<checkout>.socket
loginctl enable-linger "$USER"      # or the daemon stops when you log out
systemctl --user status nullius-<checkout>.service
```

The unit is socket-activated: systemd owns the socket and starts the daemon on first use, so
an idle machine is not holding a warm pool. A daemon killed rather than stopped leaves its
socket file behind; the next start removes it rather than failing to bind.

## Optional local ledger search

Default searches are unchanged. Use Loogle patterns to search earlier verified targets:

```bash
nullius search 'Real.sqrt, |- _ ≤ _' --backend ledger --refresh-ledger
nullius search 'Real.sqrt, |- _ ≤ _' --backend ledger
nullius search 'Real.sqrt, |- _ ≤ _' --backend loogle --include-ledger
```

`backend="ledger"` searches only the ledger. `include_ledger` adds a separate ledger group
to `loogle` or `both`; it is invalid with remote-only backends. Ledger source stays local.
With `both`, only the original query goes to the existing remote natural-language search.

HTTP `POST /search` and MCP `search` accept the same keys. For example, this explicitly
refreshes before searching:

```json
{"query": "Real.sqrt, |- _ ≤ _", "backend": "ledger", "refresh_ledger": true}
```

For library and ledger results together, use `"backend": "loogle", "include_ledger": true`.
Python `Harness.search` uses the same keyword flags, with Python booleans.

**Refresh is explicit.** Ordinary ledger searches never invoke the compiler, Lake, the
verifier, or an index build. A missing or outdated cache returns an error asking for
`--refresh-ledger` (API: `refresh_ledger=true`). Refresh requires `backend="ledger"` or
`include_ledger`; `Harness(log=False)` rejects explicit ledger access.

Refresh rechecks formerly verified targets against the current environment and indexes only
those that pass rechecking and export. Identical source/target pairs are deduplicated;
helpers are not separate hits. The cache holds immutable audited `.olean` entries and a
small, target-only Loogle index, not a rebuilt Mathlib index.

Export copies kernel-level theorem, definition, and opaque dependency terms with names
remapped, not source text. This initial version excludes targets whose dependency closure
contains locally declared inductives, structures, or recursors. Index result metadata lists
exclusions; failures do not show that a claim is unprovable. These are export limitations,
not changes to the proof language accepted by the verifier.

**Reuse source, not generated aliases.** Use a hit's row ID to retrieve its current source:
CLI `nullius log --id 123 --json`, MCP `log { "id": 123 }`, or HTTP `GET /ledger/123`.
Include the needed declarations and verify a fresh submission. Earlier proofs are not
preloaded, and `close` is unchanged. Index and single-row source reads do not create, migrate,
or write the ledger.

## Operation and configuration

- **Pool size:** each concurrent check needs a Lean process, and each holds roughly 7.5 GB
  resident, about 4 GB of it private; the remainder is shared `.olean` pages, so a second
  session costs much less than the first. Increase the pool only as needed, and measure. The pool bounds the whole server, including requests that
  name a project: those reuse the same warm sessions, since selecting a project changes the
  ledger rather than the verifier tree. Requests beyond the pool wait for a free session.
- **Isolation:** each submission starts from the preloaded library environment. Definitions
  from earlier submissions are not retained for later ones.
- **Daemon:** `nullius serve` shares one warm pool with every CLI call and MCP server for
  that checkout, which is what keeps several agents from holding several copies of the
  libraries. `NULLIUS_SOCKET` sets the socket path and `NULLIUS_NO_DAEMON=1` disables its
  use. An MCP server still answers its own requests sequentially.
- **Timeouts:** a timed-out Lean process is killed. A later call starts a replacement.
- **Logging:** verdicts are appended to `ledger.sqlite3` with their source and library
  versions. `Harness(log=False)` disables ledger use.

| Environment variable | Default |
|---|---|
| `NULLIUS_ROOT` | Checkout containing the Python package |
| `NULLIUS_LEAN_DIR` | `<root>/lean` |
| `NULLIUS_REPL_BIN` | `<lean_dir>/.lake/packages/repl/.lake/build/bin/repl`; legacy `<root>/repl/` is also checked |
| `NULLIUS_LAKE_BIN` | `lake` on PATH, then standard elan locations |
| `NULLIUS_LEDGER` | `<root>/ledger.sqlite3` |
| `NULLIUS_LEDGER_INDEX_DIR` | `<root>/build/ledger-search` |
| `NULLIUS_LEDGER_REFRESH_TIMEOUT` | 900 seconds |
| `NULLIUS_LOOGLE_BIN` | `<root>/vendor/loogle/.lake/build/bin/loogle`, if built |
| `NULLIUS_LOOGLE_MODULE` | `NulliusAll` |
| `NULLIUS_POOL_SIZE` | 2; an MCP server has no pool of its own, but the daemon it uses does |
| `NULLIUS_COMMAND_TIMEOUT` | 120 seconds |
| `NULLIUS_STARTUP_TIMEOUT` | 300 seconds |
| `NULLIUS_LEAN_THREADS` | 4 |

## Reporting results

For registered Lean projects, see [project-backed proofs](PROJECTS.md). The project owns its
modules and dependencies; its ledger records module references, environment fingerprints,
and history without source snapshots. Use `Harness(project=...)` and `verify_module(...)`,
or the corresponding project arguments on the CLI and MCP tools.

Display `verdict.statement` beside the informal claim. Verification checks the formal proof;
it does not check that the formalization expresses the intended claim. The contradiction
and unnecessary-assumption probes can miss problems, so they do not replace this comparison.

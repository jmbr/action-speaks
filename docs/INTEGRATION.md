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
h.find_proof("25 < n * n", binders="(n : Nat) (h : 5 < n)")
```

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

## Operation and configuration

- **Pool size:** each concurrent check needs a Lean process. Increase the pool only as
  needed and measure memory use.
- **Isolation:** each submission starts from the preloaded library environment. Definitions
  from earlier submissions are not retained for later ones.
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
| `NULLIUS_LOOGLE_BIN` | `<root>/vendor/loogle/.lake/build/bin/loogle`, if built |
| `NULLIUS_LOOGLE_MODULE` | `NulliusAll` |
| `NULLIUS_POOL_SIZE` | 2 |
| `NULLIUS_COMMAND_TIMEOUT` | 120 seconds |
| `NULLIUS_STARTUP_TIMEOUT` | 300 seconds |
| `NULLIUS_LEAN_THREADS` | 4 |

## Reporting results

Display `verdict.statement` beside the informal claim. Verification checks the formal proof;
it does not check that the formalization expresses the intended claim. The contradiction
and unnecessary-assumption probes can miss problems, so they do not replace this comparison.

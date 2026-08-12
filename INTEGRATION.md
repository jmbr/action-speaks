# Using nullius from a harness

Four integration paths, in rough order of how tightly coupled they are.

| Path | Use when | Startup cost |
|---|---|---|
| Python API (`Harness`) | your harness is Python | once per process |
| HTTP service | your harness is not Python, or runs elsewhere | once per server |
| MCP server | the agent should call it as a tool, mid-conversation | once per server |
| CLI | one-off checks, shell scripts, CI | **once per invocation** |

The Mathlib import costs ~2.5 s and ~7 GB per Lean session. Everything below except the CLI
pays that once and then answers in milliseconds. Do not shell out to the CLI in a loop.

## 1. Python

```python
from nullius import Harness

with Harness(pool_size=4).warm() as h:
    v = h.verify(
        "theorem main (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
        claim="if n > 5 then n squared exceeds 25",
    )
    if v.verified:
        print(v.statement)     # ∀ (n : ℕ), 5 < n → 25 < n * n
    else:
        print(v.feedback())    # what to tell the model
```

`Verdict` carries `verified`, `status`, `statement`, `axioms`, `checks`, `provenance`,
`elapsed`, `to_dict()`, `to_json()`, `render()` (for logs) and `feedback()` (for the model).

### Batch, concurrently

```python
verdicts = h.verify_many([
    {"source": src_a, "claim": "..."},
    {"source": src_b, "claim": "..."},
])
```

Order is preserved; concurrency is capped at the pool size. A batch of 6 finishes in ~0.1 s
on a warm pool of 3.

### Repair loop

The common pattern: verify, feed the failure back, resubmit.

```python
def propose(feedback, round):
    prompt = base_prompt if feedback is None else f"{base_prompt}\n\nPrevious attempt failed:\n{feedback}"
    return model.generate(prompt)

verdict, history = h.prove(propose, claim="...", max_rounds=3)
print(f"{len(history)} round(s) -> {verdict.status}")
```

`feedback()` is written to be actionable rather than descriptive. For a vacuous theorem it
says *fix the statement, not the proof* — which is the correction the model actually needs
and rarely makes on its own.

### Pre-flight

Check the statement before spending rounds on a proof:

```python
info = h.check_statement("(n : Nat) (h1 : n > 5) (h2 : n < 3) : n = 42")
if info["vacuous"]:
    ...   # hypotheses contradict; no proof of this is worth anything
```

### Finding lemmas

```python
h.search("sum of two even numbers is even")            # natural language + shape
h.find_proof("25 < n * n", binders="(n : Nat) (h : 5 < n)")  # asks Lean; cannot hallucinate
```

### As a reward signal

For RL or best-of-n, `verified` is the boolean, and the checks give partial credit:

```python
def reward(v):
    if v.verified:
        return 1.0
    passed = sum(c.passed for c in v.checks)
    return 0.5 * passed / max(1, len(v.checks))
```

Note that `not_vacuous` and `hypotheses_used` are what stop a model from farming reward by
emitting easy-but-empty theorems. Keep `require_nontrivial=True` when the score matters.

## 2. HTTP

```bash
python3 -m nullius.http_server --port 823 --pool 4
```

```bash
curl -s localhost:823/verify -H 'Content-Type: application/json' \
  -d '{"source":"theorem t (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
       "claim":"if n>5 then n^2>25"}'
```

```json
{"verified": true,
 "status": "verified",
 "statement": "∀ (n : ℕ), 5 < n → 25 < n * n",
 "axioms": ["propext", "Classical.choice", "Quot.sound"],
 "checks": [{"name": "static_guard", "passed": true}, ...],
 "feedback": "VERIFIED. Cite this as machine-checked, ...",
 "provenance": {"toolchain": "leanprover/lean4:v4.33.0", "mathlib_rev": "db584cd6..."}}
```

Endpoints: `GET /health`, `GET /ledger`, `POST /verify`, `/verify_many`, `/statement`,
`/search`, `/find_proof`. Binds to localhost and has no authentication — submissions run
arbitrary elaboration in Lean, so keep it off untrusted networks.

## 3. MCP

```json
{"mcpServers": {"nullius": {
  "command": "python3", "args": ["-m", "nullius.mcp_server"],
  "cwd": "/home/jmbr/sources/nullius"}}}
```

Tools: `lean_verify`, `lean_check_statement`, `lean_search_lemma`, `lean_find_proof`,
`lean_ledger`. Put `AGENTS.md` in the system prompt so the agent knows the workflow and the
rejection rules.

## 4. CLI

```bash
python3 -m nullius.cli verify proof.lean -c "claim" --json --require-nontrivial
echo "$SRC" | python3 -m nullius.cli verify - --json
```

Exit code is 0 when verified, 1 otherwise, so it drops into CI directly. Each invocation
starts its own Lean session.

## Operational notes

- **Pool sizing.** One in-flight verification needs one Lean session (~7 GB resident, though
  `.olean` pages are shared, so the marginal session costs far less than the first). Size the
  pool to your concurrency, not your core count.
- **Isolation.** Every check branches off a pristine Mathlib environment, so one submission
  cannot leave definitions behind for the next. This is a soundness property, not just
  hygiene.
- **Timeouts.** A wedged Lean process cannot be interrupted politely; on timeout the session
  is killed and replaced. Set `NULLIUS_COMMAND_TIMEOUT` (default 120 s).
- **Ledger.** Every verdict is appended to `ledger.sqlite3` with the toolchain and Mathlib
  revision. Pass `log=False` to `Harness` for throwaway runs; keep it on when the verdicts
  are evidence you may need to defend later.

## Environment variables

| Variable | Default |
|---|---|
| `NULLIUS_LEAN_DIR` | `<repo>/lean` |
| `NULLIUS_REPL_BIN` | `<repo>/repl/.lake/build/bin/repl` |
| `NULLIUS_LEDGER` | `<repo>/ledger.sqlite3` |
| `NULLIUS_POOL_SIZE` | 2 |
| `NULLIUS_COMMAND_TIMEOUT` | 120 |
| `NULLIUS_STARTUP_TIMEOUT` | 300 |
| `NULLIUS_LEAN_THREADS` | 4 |

## What you still have to do yourself

The verifier certifies that the proof is sound and that the theorem is not empty. It cannot
certify that the Lean statement means what your English claim meant. Always surface
`verdict.statement` next to the claim — in the transcript, the PR comment, wherever the
claim is presented — so the mismatch is visible to a human. That comparison is the residual
trust, and it is the one place a harness should not fully automate.

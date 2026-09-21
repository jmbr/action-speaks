# Contributing to action-speaks

Start with a checkout built according to the [installation guide](docs/INSTALL.md), then
install the development tools:

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/prek install
.venv/bin/nox -s lint format
.venv/bin/nox -s types
.venv/bin/nox -s tests
```

Use `nox -s fix` to apply formatting. The tests are pytest tests: `nox -s quick` runs the
Lean-free ones in about a second, and `nox -s tests` runs everything, including the
documentation suite that verifies the cookbook's Lean blocks and the skill guides' proof
examples. Tests needing a built `lean/` checkout carry the `lean` marker, so
`pytest -m "not lean"` selects the rest. Arguments reach pytest after `--`, for example
`nox -s tests -- -k adversarial`.

Commit hooks are deliberately few: file hygiene, a Conventional Commits check, plus
`nox -s quick`. The Lean-backed tests need a built checkout and take minutes, so run
`nox -s tests` before pushing rather than on every commit. Use
`.venv/bin/prek run --all-files` to run all hooks.

## Commit messages

Subjects follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), so
that release tooling can read the history rather than have somebody summarize it. `prek
install` wires up a `commit-msg` hook that rejects anything else.

```text
feat: add a per-user session daemon
fix(daemon): stop a failed bind unlinking the winner's socket
docs: say that the MCP server answers one request at a time
feat!: rename the environment variables
```

`feat` is a minor bump and `fix` a patch one. `docs`, `test`, `build`, `ci`, `refactor`,
`perf`, `style` and `chore` release nothing. A `!` before the colon, or a `BREAKING CHANGE:`
footer, marks a breaking change; below 1.0 that is still a minor bump, since 2.0 would claim
a stability this project has not offered yet. Scopes are optional and free-form.

Only the subject is constrained. Keep the body short: why the change is right, not what the
diff already shows.

## Releases

`python-semantic-release` reads the history and decides the version; Commitizen never bumps.

```bash
.venv/bin/semantic-release --noop version --print   # what the next version would be
```

`feat` gives a minor bump, `fix` a patch one. Below 1.0 a breaking change stays minor. The
version lives in `action_speaks/__init__.py` and nowhere else; `CHANGELOG.md` is generated.
Tags are `vX.Y.Z`, starting from `v0.1.0`, which marks where Conventional Commits were
adopted.

`.gitea/workflows/release.yml` runs after every push to `main`, after the complete test suite.
It supplies the instance URL and short-lived `GITEA_TOKEN`, updates the version and changelog,
pushes `chore(release): vX.Y.Z` and the tag, and creates the Gitea release. Non-releasing
commits make no release; the generated release commit is skipped on its second workflow run.
The workflow may also be dispatched manually.

This Gitea instance uses HTTP, so the remote explicitly permits an insecure connection.
No package, PyPI upload, release artifact, or GitHub release is produced.

## Project layout

| Path | Purpose |
|---|---|
| `lean/ActionSpeaks/Audit.lean` | Lean audit commands |
| `lean/lakefile.toml`, `lean/lake-manifest.json` | Dependency requirements and locked revisions |
| `lean/ActionSpeaksAll.lean` | Combined imports for the local search index |
| `action_speaks/config.py`, `action_speaks/repl.py` | Configuration, Lean processes, and session pool |
| `action_speaks/guard.py`, `action_speaks/verify.py` | Source restrictions, verification pipeline, and verdicts |
| `action_speaks/ledger.py`, `action_speaks/search.py` | Stored results, recall, and lemma search |
| `action_speaks/harness.py` | Python API |
| `action_speaks/cli.py`, `action_speaks/http_server.py`, `action_speaks/mcp_server.py` | CLI and server entry points |
| `skills/action-speaks/`, `mcp/`, `install.py` | Agent instructions and installation |
| `tests/`, `noxfile.py`, `.pre-commit-config.yaml` | Tests and development checks |
| `scripts/check_updates.py` | Which Lean release the pins could move to, and what it costs |
| `contrib/` | Separate contributions, not part of the verifier build |

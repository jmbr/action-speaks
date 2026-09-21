"""Development sessions: `nox -s lint`, `nox -s tests`, and so on.

Two properties of this project shape every session below, and both were measured rather than
assumed, because both fail quietly:

* **The package must be installed editable.** `action-speaks` is a thin driver for a Lean project of
  several gigabytes that has to be built locally, and it locates that project relative to its
  own source file. A plain `session.install(".")` copies the Python into the session's
  `site-packages`, where it then looks for `lean/` beside the copy and finds nothing. Every
  session that imports the package therefore uses `-e`.

* **Lean dominates the runtime.** The tests that need a built `lean/` checkout — a Lean
  session, a `lake build`, or a local Loogle index — carry the `lean` marker declared in
  pyproject.toml. `quick` deselects them and finishes in about a second; `tests` runs
  everything and takes minutes. Marking them is what makes that split possible, so a new
  test that starts Lean needs the marker too, or `quick` stops being quick.

`lint`, `format` and `types` need neither Lean nor a built tree, so they are the ones worth
running in a loop. `tests` needs a built checkout; see "Setup from scratch" in the README.

    nox                      # lint, format, types, tests
    nox -s lint format       # seconds
    nox -s fix               # apply what `format` asks for
    nox -s quick             # the Lean-free tests alone, ~1 s
    nox -s tests             # minutes, and needs lean/ built
    nox -s tests -- -k ledger  # arguments after `--` reach pytest
"""

import nox

nox.options.sessions = ["lint", "format", "types", "tests"]
nox.options.reuse_existing_virtualenvs = True

# Everything of ours that is Python. `examples/` is included because it is documentation
# people copy from, and this file because a linter that exempts itself is a poor advertisement.
SOURCES = ["action_speaks", "tests", "examples", "install.py", "noxfile.py"]


@nox.session
def lint(session):
    """Rules and their exceptions live in `[tool.ruff.lint]` in pyproject.toml."""
    session.install("ruff")
    session.run("ruff", "check", *SOURCES)


@nox.session
def format(session):
    """Report formatting and import-order drift without rewriting anything."""
    session.install("ruff")
    session.run("ruff", "check", "--select", "I", *SOURCES)
    session.run("ruff", "format", "--check", "--diff", *SOURCES)


@nox.session
def fix(session):
    """The counterpart to `format`: apply what it would ask for."""
    session.install("ruff")
    session.run("ruff", "check", "--select", "I", "--fix", *SOURCES)
    session.run("ruff", "format", *SOURCES)


@nox.session
def types(session):
    """Settings come from `[tool.pyright]` in pyproject.toml."""
    session.install("-e", ".")
    session.install("basedpyright")
    session.run("basedpyright", "action_speaks")


@nox.session
def mypy(session):
    """Not in the default set: it and pyright disagree, and one opinion is enough to act on."""
    session.install("-e", ".")
    session.install("mypy")
    session.run("mypy", "action_speaks")


@nox.session
def quick(session):
    """Everything that needs no Lean, for when only prose or Python has changed."""
    session.install("-e", ".", "pytest")
    session.run("pytest", "-m", "not lean", *session.posargs)


@nox.session
def tests(session):
    """The real suite. Requires `lean/` to be built — see the README.

    Arguments after `--` are passed through, so `nox -s tests -- -x -k adversarial` works.
    """
    session.install("-e", ".", "pytest")
    session.run("pytest", *session.posargs)

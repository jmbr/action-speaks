"""Development sessions: `nox -s lint`, `nox -s tests`, and so on.

Two properties of this project shape every session below, and both were measured rather than
assumed, because both fail quietly:

* **The package must be installed editable.** `nullius` is a thin driver for a Lean project of
  several gigabytes that has to be built locally, and it locates that project relative to its
  own source file. A plain `session.install(".")` copies the Python into the session's
  `site-packages`, where it then looks for `lean/` beside the copy and finds nothing. Every
  session that imports the package therefore uses `-e`.

* **The tests are not pytest.** Each is a standalone script with a `main()` returning an exit
  code, because they double as the commit hooks in `.pre-commit-config.yaml` and have to run
  with no test runner present. Pointing pytest at `tests/` collects **zero** tests and exits
  0 — a green run that checked nothing, which is precisely the failure this repository exists
  to argue against. They are invoked directly instead.

`lint`, `format` and `types` need neither Lean nor a built tree, so they are the ones worth
running in a loop. `tests` needs a built checkout; see "Setup from scratch" in the README.

    nox                      # lint, format, types, tests
    nox -s lint format       # seconds
    nox -s fix               # apply what `format` asks for
    nox -s quick             # the Lean-free test alone, ~0.1 s
    nox -s tests             # minutes, and needs lean/ built
"""

from pathlib import Path

import nox
import nox.command

nox.options.sessions = ["lint", "format", "types", "tests"]
nox.options.reuse_existing_virtualenvs = True

# Everything of ours that is Python. `examples/` is included because it is documentation
# people copy from, and this file because a linter that exempts itself is a poor advertisement.
SOURCES = ["nullius", "tests", "examples", "noxfile.py"]

# Cheapest first, so a broken environment surfaces before minutes go into Lean. Only
# `check_names` is Lean-free; the others need a built checkout, and each skips itself
# gracefully when an optional piece (Loogle, an installed entry point) is absent.
TEST_SCRIPTS = [
    "tests/check_names.py",
    "tests/test_entrypoints.py",
    "tests/test_search.py",
    "tests/test_adversarial.py",
    "tests/test_docs.py",
]


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
    session.run("basedpyright", "nullius")


@nox.session
def mypy(session):
    """Not in the default set: it and pyright disagree, and one opinion is enough to act on."""
    session.install("-e", ".")
    session.install("mypy")
    session.run("mypy", "nullius")


@nox.session
def quick(session):
    """The Lean-free check alone, for when only prose or naming has changed."""
    session.install("-e", ".")
    session.run("python", "tests/check_names.py")


@nox.session
def tests(session):
    """The real suite. Requires `lean/` to be built — see the README.

    Every script runs even when an earlier one fails, so a single failure cannot hide the
    rest; the session then fails with the full list. That matters more here than usual,
    because these checks are deliberately independent of each other.
    """
    listed = {Path(s).name for s in TEST_SCRIPTS}
    present = {p.name for p in Path("tests").glob("*.py")}
    if missing := present - listed:
        session.error(
            f"not listed in TEST_SCRIPTS, so never run: {', '.join(sorted(missing))}. "
            "Add them there (and as a hook in .pre-commit-config.yaml)."
        )

    session.install("-e", ".")
    failures = []
    for script in TEST_SCRIPTS:
        session.log(f"--- {script}")
        try:
            session.run("python", script)
        except nox.command.CommandFailed:
            failures.append(script)
    if failures:
        session.error(f"{len(failures)} failed: {', '.join(failures)}")

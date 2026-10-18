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

`lint`, `format`, `types` and `skills` need neither Lean nor a built tree, so they are the ones
worth running in a loop. `tests` needs a built checkout; see "Setup from scratch" in the README.

    nox                      # lint, format, types, skills, tests
    nox -s lint format       # seconds
    nox -s skills            # validate and security-scan skills/
    nox -s fix               # apply what `format` asks for
    nox -s quick             # the Lean-free tests alone, ~1 s
    nox -s tests             # minutes, and needs lean/ built
    nox -s tests -- -k ledger  # arguments after `--` reach pytest
"""

from pathlib import Path

import nox

nox.options.sessions = ["lint", "format", "types", "skills", "tests"]
nox.options.reuse_existing_virtualenvs = True

# Everything of ours that is Python. `examples/` is included because it is documentation
# people copy from, and this file because a linter that exempts itself is a poor advertisement.
SOURCES = ["action_speaks", "tests", "examples", "install.py", "noxfile.py"]

PYPROJECT = nox.project.load_toml("pyproject.toml")


def install_group(session, group):
    """Install a `[dependency-groups]` entry from pyproject.toml, the one place tools are named."""
    session.install(*nox.project.dependency_groups(PYPROJECT, group))


@nox.session
def lint(session):
    """Rules and their exceptions live in `[tool.ruff.lint]` in pyproject.toml."""
    install_group(session, "lint")
    session.run("ruff", "check", *SOURCES)


@nox.session
def format(session):
    """Report formatting and import-order drift without rewriting anything."""
    install_group(session, "lint")
    session.run("ruff", "check", "--select", "I", *SOURCES)
    session.run("ruff", "format", "--check", "--diff", *SOURCES)


@nox.session
def fix(session):
    """The counterpart to `format`: apply what it would ask for."""
    install_group(session, "lint")
    session.run("ruff", "check", "--select", "I", "--fix", *SOURCES)
    session.run("ruff", "format", *SOURCES)


@nox.session
def types(session):
    """Settings come from `[tool.pyright]` in pyproject.toml."""
    session.install("-e", ".")
    install_group(session, "types")
    session.run("basedpyright", "action_speaks")


@nox.session
def mypy(session):
    """Not in the default set: it and pyright disagree, and one opinion is enough to act on."""
    session.install("-e", ".")
    install_group(session, "mypy")
    session.run("mypy", "action_speaks")


@nox.session
def skills(session):
    """Validate every Agent Skill under skills/ and scan it for security findings.

    The scan is static (`--no-llm`), so it needs no credentials and gives the same answer
    every time. Without `--fail-on-findings` SkillSpector reports findings but exits 0.
    """
    directories = sorted(path for path in Path("skills").glob("*") if path.is_dir())
    if not directories:
        session.skip("no skills to validate")
    install_group(session, "skills")
    for directory in directories:
        # The PyPI release of skills-ref installs its command as `agentskills`.
        session.run("agentskills", "validate", str(directory))
        session.run("skillspector", "scan", str(directory), "--no-llm", "--fail-on-findings")


@nox.session
def quick(session):
    """Everything that needs no Lean, for when only prose or Python has changed."""
    session.install("-e", ".")
    install_group(session, "test")
    session.run("pytest", "-m", "not lean", *session.posargs)


@nox.session
def tests(session):
    """The real suite. Requires `lean/` to be built — see the README.

    Arguments after `--` are passed through, so `nox -s tests -- -x -k adversarial` works.
    """
    session.install("-e", ".")
    install_group(session, "test")
    session.run("pytest", *session.posargs)

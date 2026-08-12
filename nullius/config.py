"""Locating the Lean project, the REPL binary, and tuning knobs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


def _missing_lean_dir_message(lean_dir: Path) -> str:
    """Explain a missing Lean tree, distinguishing the two ways to arrive here.

    The usual cause is a copied (non-editable) install: this package is a driver for a Lean
    project of several gigabytes that has to be built locally, and it locates that project
    relative to its own source file. Copy the Python into `site-packages` and it looks for
    the Lean tree beside the copy, where nothing was ever built. `pip install -e .` from a
    checkout keeps the two together, which is why that is the documented way in.
    """
    base = f"Lean project directory not found: {lean_dir}"
    if "site-packages" in str(lean_dir):
        return (
            f"{base}\n"
            "This looks like a copied install. nullius drives a Lean project that must be "
            "built from a checkout, so install it from one with `pip install -e .`, or set "
            "NULLIUS_ROOT to the checkout you built."
        )
    return f"{base}\nSet NULLIUS_ROOT to the checkout containing lean/, or build it there."


@dataclass
class Config:
    lean_dir: Path
    repl_bin: Path
    lake_bin: Path
    ledger_path: Path
    loogle_bin: Path | None = None
    loogle_module: str = "NulliusAll"
    startup_timeout: float = 300.0
    command_timeout: float = 120.0
    lean_threads: int = 4
    pool_size: int = 2

    @classmethod
    def discover(cls) -> "Config":
        root = Path(os.environ.get("NULLIUS_ROOT", ROOT))
        lean_dir = Path(os.environ.get("NULLIUS_LEAN_DIR", root / "lean"))
        repl_bin = Path(os.environ["NULLIUS_REPL_BIN"]) if "NULLIUS_REPL_BIN" in os.environ \
            else cls._find_repl(root, lean_dir)
        lake = os.environ.get("NULLIUS_LAKE_BIN") or shutil.which("lake")
        if not lake:
            raise ConfigError("`lake` not found on PATH; is elan installed?")
        ledger = Path(os.environ.get("NULLIUS_LEDGER", root / "ledger.sqlite3"))
        return cls(
            lean_dir=lean_dir,
            repl_bin=repl_bin,
            lake_bin=Path(lake),
            ledger_path=ledger,
            loogle_bin=cls._find_loogle(root),
            loogle_module=os.environ.get("NULLIUS_LOOGLE_MODULE", "NulliusAll"),
            command_timeout=float(os.environ.get("NULLIUS_COMMAND_TIMEOUT", 120.0)),
            startup_timeout=float(os.environ.get("NULLIUS_STARTUP_TIMEOUT", 300.0)),
            lean_threads=int(os.environ.get("NULLIUS_LEAN_THREADS", 4)),
            pool_size=int(os.environ.get("NULLIUS_POOL_SIZE", 2)),
        )

    @staticmethod
    def _find_loogle(root: Path) -> Path | None:
        """Locate a local Loogle binary, if one has been built.

        Optional by design: without it, shape search falls back to the hosted service, so a
        checkout that never runs `scripts/build-loogle.sh` still works.
        """
        explicit = os.environ.get("NULLIUS_LOOGLE_BIN")
        if explicit:
            return Path(explicit)
        candidate = root / "vendor" / "loogle" / ".lake" / "build" / "bin" / "loogle"
        return candidate if candidate.exists() else None

    @staticmethod
    def _find_repl(root: Path, lean_dir: Path) -> Path:
        """Locate the REPL binary.

        The REPL is a lake dependency of the Lean project, so lake builds it into
        `.lake/packages/repl/` with the toolchain pinned by `lake-manifest.json`. A
        hand-cloned `repl/` at the repository root is still honoured, for setups predating
        that change.
        """
        candidates = [
            lean_dir / ".lake" / "packages" / "repl" / ".lake" / "build" / "bin" / "repl",
            root / "repl" / ".lake" / "build" / "bin" / "repl",
        ]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    def validate(self) -> None:
        if not self.lean_dir.is_dir():
            raise ConfigError(_missing_lean_dir_message(self.lean_dir))
        if not (self.lean_dir / "lakefile.toml").exists():
            raise ConfigError(f"No lakefile.toml in {self.lean_dir}")
        if not self.repl_bin.exists():
            raise ConfigError(
                f"REPL binary not found at {self.repl_bin}.\n"
                f"Build it with:  cd {self.lean_dir} && lake build repl"
            )

    def toolchain(self) -> str:
        f = self.lean_dir / "lean-toolchain"
        return f.read_text().strip() if f.exists() else "unknown"

    def mathlib_rev(self) -> str:
        return self.package_rev("mathlib")

    def package_rev(self, name: str) -> str:
        manifest = self.lean_dir / "lake-manifest.json"
        if not manifest.exists():
            return "unknown"
        try:
            data = json.loads(manifest.read_text())
        except json.JSONDecodeError:
            return "unknown"
        for pkg in data.get("packages", []):
            if pkg.get("name") == name:
                return pkg.get("rev", "unknown")
        return "unknown"

    def loogle_rev(self) -> str:
        """The revision of the local Loogle checkout, if there is one.

        Recorded because a lemma search is part of how a proof was arrived at, and because
        the local index reflects *these* Mathlib and Physlib revisions rather than whatever
        the hosted service last deployed.
        """
        if not self.loogle_bin:
            return ""
        repo = Path(self.loogle_bin).parents[3]
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            return out.stdout.strip() if out.returncode == 0 else ""
        except Exception:
            return ""

    def provenance(self) -> dict[str, str]:
        """Everything needed to reproduce a verification on another machine."""
        prov = {
            "toolchain": self.toolchain(),
            "mathlib_rev": self.mathlib_rev(),
            "repl_rev": self.package_rev("repl"),
            # Recorded because Physlib ships deliberately incomplete results, and which
            # ones are complete changes between revisions. A verdict citing it is only
            # reproducible against the exact revision it was checked at.
            "physlib_rev": self.package_rev("Physlib"),
            "lean_dir": str(self.lean_dir),
            "repl_bin": str(self.repl_bin),
        }
        rev = self.loogle_rev()
        if rev:
            prov["loogle_rev"] = rev
        return prov

"""Locating the Lean project, the REPL binary, and tuning knobs."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    lean_dir: Path
    repl_bin: Path
    lake_bin: Path
    ledger_path: Path
    startup_timeout: float = 300.0
    command_timeout: float = 120.0
    lean_threads: int = 4
    pool_size: int = 2

    @classmethod
    def discover(cls) -> "Config":
        root = Path(os.environ.get("LEANAI_ROOT", ROOT))
        lean_dir = Path(os.environ.get("LEANAI_LEAN_DIR", root / "lean"))
        repl_bin = Path(os.environ["LEANAI_REPL_BIN"]) if "LEANAI_REPL_BIN" in os.environ \
            else cls._find_repl(root, lean_dir)
        lake = os.environ.get("LEANAI_LAKE_BIN") or shutil.which("lake")
        if not lake:
            raise ConfigError("`lake` not found on PATH; is elan installed?")
        ledger = Path(os.environ.get("LEANAI_LEDGER", root / "ledger.sqlite3"))
        return cls(
            lean_dir=lean_dir,
            repl_bin=repl_bin,
            lake_bin=Path(lake),
            ledger_path=ledger,
            command_timeout=float(os.environ.get("LEANAI_COMMAND_TIMEOUT", 120.0)),
            startup_timeout=float(os.environ.get("LEANAI_STARTUP_TIMEOUT", 300.0)),
            lean_threads=int(os.environ.get("LEANAI_LEAN_THREADS", 4)),
            pool_size=int(os.environ.get("LEANAI_POOL_SIZE", 2)),
        )

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
            raise ConfigError(f"Lean project directory not found: {self.lean_dir}")
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

    def provenance(self) -> dict[str, str]:
        """Everything needed to reproduce a verification on another machine."""
        return {
            "toolchain": self.toolchain(),
            "mathlib_rev": self.mathlib_rev(),
            "repl_rev": self.package_rev("repl"),
            "lean_dir": str(self.lean_dir),
            "repl_bin": str(self.repl_bin),
        }

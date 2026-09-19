"""Optional ledger shape search, using isolated databases and caches."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nullius import ledger_search as LS  # noqa: E402
from nullius import search as S  # noqa: E402
from nullius.config import Config  # noqa: E402
from nullius.ledger import Ledger  # noqa: E402
from nullius.repl import Session  # noqa: E402
from nullius.verify import Verifier  # noqa: E402

SOURCE = """
namespace Demo
def helper (n : Nat) := n + 1
theorem main (n : Nat) : helper n = n + 1 := rfl
end Demo
"""
POLYMORPHIC = """
namespace Demo
universe u
section
variable {a : Type u} [Zero a]
private def helper : a := 0
theorem main : (helper : a) = 0 := rfl
end
end Demo
"""
UNSUPPORTED = """
inductive LocalToken | token
theorem token_eq : LocalToken.token = LocalToken.token := rfl
"""
QUERY = "|- (_ : Nat) = _"


def routing() -> None:
    with (
        patch.object(S, "loogle", return_value=S.SearchResult("q", "loogle-local")),
        patch.object(LS, "search_ledger") as ledger,
    ):
        S.search("q", backend="loogle")
        ledger.assert_not_called()
        S.search("q", backend="loogle", include_ledger=True)
        assert ledger.call_count == 1
    for args in (
        {"backend": "leansearch", "include_ledger": True},
        {"backend": "loogle-remote", "include_ledger": True},
        {"backend": "loogle", "refresh_ledger": True},
        {"backend": "loogle", "include_ledger": "false"},
        {"backend": "ledger", "refresh_ledger": 1},
    ):
        with patch.object(S, "_http_json") as remote:
            try:
                S.search("q", **args)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid options accepted")
            remote.assert_not_called()
    with (
        patch.object(S, "loogle", return_value=S.SearchResult("q", "loogle-local")),
        patch.object(S, "leansearch", return_value=S.SearchResult("q", "leansearch")) as remote,
        patch.object(LS, "search_ledger", return_value=S.SearchResult("q", "loogle-ledger")),
    ):
        results = S.search("q", backend="both", include_ledger=True)
        assert [r.backend for r in results] == ["loogle-local", "leansearch", "loogle-ledger"]
        remote.assert_called_once_with("q", limit=10, timeout=20.0)


def deadline_test(directory: Path) -> None:
    binary = directory / "unresponsive"
    binary.write_text(
        f"#!{sys.executable}\nimport time\nprint('Loogle is ready.', flush=True)\ntime.sleep(30)\n"
    )
    binary.chmod(0o700)
    session = S.LoogleSession(
        binary=binary,
        lean_dir=directory,
        lake_bin=Path(sys.executable),
        module="Unused",
        direct=True,
    )
    try:
        session.start(timeout=5)
        proc = session.proc
        start = time.monotonic()
        result = session.query("q", timeout=0.1)
        assert result.error and "reply" in result.error
        assert time.monotonic() - start < 5
        assert proc is not None and proc.poll() is not None
    finally:
        session.close()


def integration(config: Config, directory: Path) -> None:
    cfg = replace(
        config,
        ledger_path=directory / "ledger.sqlite3",
        ledger_index_dir=directory / "index",
        ledger_refresh_timeout=300,
    )
    assert not LS.search_ledger(QUERY, config=cfg).hits
    assert not cfg.ledger_path.exists()
    assert not cfg.ledger_index_dir.exists()
    ledger = Ledger(cfg.ledger_path)
    with Session(cfg) as session:
        vf = Verifier(session, cfg)
        for source, target in (
            (SOURCE, "Demo.main"),
            (POLYMORPHIC, "Demo.main"),
            (UNSUPPORTED, "token_eq"),
        ):
            v = vf.verify(source, target=target)
            assert v.verified, v.render()
            ledger.record(v, source, tag="fixture")
        rejected = vf.verify("theorem bad : True := by sorry", target="bad")
        assert not rejected.verified
        ledger.record(rejected, "theorem bad : True := by sorry")
        before = ledger.stats()["total"]
        assert LS.search_ledger(QUERY, config=cfg).error
        assert not cfg.ledger_index_dir.exists()
        with patch.object(S, "_http_json", side_effect=AssertionError("network used")):
            result = LS.search_ledger(QUERY, refresh=True, config=cfg)
        assert not result.error, result.render()
        assert result.hits and all(h.name == "Demo.main" for h in result.hits), result.render()
        assert result.index is not None and len(result.index["excluded"]) == 1
        assert "unsupported local declaration" in result.index["excluded"][0]["reason"]
        assert ledger.stats()["total"] == before
        all_hits = LS.search_ledger('"main"', config=cfg)
        assert len(all_hits.hits) == 2, all_hits.render()
        assert all(h.ledger and h.source == "loogle-ledger" for h in all_hits.hits)
        no_library = LS.search_ledger("Nat.succ", config=cfg)
        assert all(h.ledger is not None for h in no_library.hits)
        no_helper = LS.search_ledger('"helper"', config=cfg)
        assert not no_helper.hits
        pointer = LS._cache_dir(cfg) / "current.json"
        snapshot = json.loads(pointer.read_text())
        assert Path(snapshot["index_file"]).stat().st_size < 1_000_000
        assert all(s.direct and s.index_mode == "read" for s in LS._sessions.values())
        mtimes = {p: Path(p).stat().st_mtime_ns for p in snapshot["artifacts"]}
        with (
            patch.object(LS, "_run", side_effect=AssertionError("ordinary search built")),
            patch.object(
                LS.Verifier, "verify", side_effect=AssertionError("ordinary search verified")
            ),
        ):
            assert LS.search_ledger(QUERY, config=cfg).hits
            assert LS.search_ledger(QUERY, refresh=True, config=cfg).hits
        assert mtimes == {p: Path(p).stat().st_mtime_ns for p in snapshot["artifacts"]}
        # Duplicate records only add metadata, not a new proof module.
        ledger.record(vf.verify(SOURCE, target="Demo.main"), SOURCE, tag="duplicate")
        with patch.object(LS, "_run", side_effect=AssertionError("duplicate source rebuilt")):
            duplicate = LS.search_ledger('"main"', config=cfg)
            assert any(h.ledger and len(h.ledger["ids"]) == 2 for h in duplicate.hits)
        extra_source = "theorem added (n : Nat) : n + 0 = n := by omega"
        ledger.record(vf.verify(extra_source, target="added"), extra_source)
        assert LS.search_ledger(QUERY, config=cfg).error
        old_pointer = pointer.read_bytes()
        run = LS._run

        def interrupt(command, cwd, deadline, config):
            if "--run" in command:
                raise LS.LedgerSearchError("simulated interrupted index build")
            return run(command, cwd, deadline, config)

        with patch.object(LS, "_run", side_effect=interrupt):
            interrupted = LS.search_ledger(QUERY, refresh=True, config=cfg)
            assert interrupted.error and "interrupted" in interrupted.error
        assert pointer.read_bytes() == old_pointer
        updated = LS.search_ledger('"added"', refresh=True, config=cfg)
        assert updated.hits and not updated.error, updated.render()
        for entry in snapshot["entries"]:
            for file in entry["artifacts"]:
                assert Path(file).stat().st_mtime_ns == mtimes[file], "unchanged module rebuilt"
        with patch.object(LS, "_environment_key", return_value="changed-environment"):
            assert LS.search_ledger(QUERY, config=cfg).error
        # Broken metadata is an error, never a traceback or a normal empty result.
        pointer_text = pointer.read_text()
        malformed = json.loads(pointer_text)
        malformed["entries"] = [42]
        pointer.write_text(json.dumps(malformed))
        assert LS.search_ledger(QUERY, config=cfg).error
        repaired = LS.search_ledger(QUERY, refresh=True, config=cfg)
        assert repaired.hits and not repaired.error, repaired.render()
        assert repaired.index and repaired.index["repairs"]
        local_name = re.search(r"NulliusLedgerEntries\.[^\s(]+", result.hits[0].signature)
        assert local_name is not None
        assert LS.search_ledger(local_name.group(), config=cfg).hits
        assert not vf.verify(
            "theorem leaked (n : Nat) : Demo.helper n = n + 1 := rfl",
            target="leaked",
        ).verified
    S.close_sessions(cfg)
    assert not LS._sessions
    print("Ledger shape search, isolation, incremental refresh, and retrieval metadata: OK")


def main() -> int:
    routing()
    with tempfile.TemporaryDirectory(prefix="nullius-ledger-tests-") as temp:
        directory = Path(temp)
        deadline_test(directory)
        config = Config.discover()
        lock = directory / "lock"
        with LS._build_lock(lock, time.monotonic() + 5):
            try:
                with LS._build_lock(lock, time.monotonic() + 0.05):
                    raise AssertionError("concurrent writer acquired the lock")
            except LS.LedgerSearchError:
                pass
        try:
            LS._run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                directory,
                time.monotonic() + 0.1,
                config,
            )
        except LS.LedgerSearchError:
            pass
        else:
            raise AssertionError("build deadline did not stop the process")
        if not config.loogle_bin or not config.loogle_bin.exists():
            print("Local Loogle unavailable; integration portion skipped")
        else:
            try:
                integration(config, directory)
            finally:
                S.close_sessions()
    print("Ledger search tests: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

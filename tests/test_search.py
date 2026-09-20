"""Check the local Loogle backend, when one has been built.

Shape search is the route an agent reaches for before it knows a lemma's name, so a silent
regression here is expensive: the agent falls back to guessing, which is the failure mode
the whole tool exists to prevent.

Two properties matter and neither is covered elsewhere:

* the local index reaches Physlib, Cslib and FloatLib, which is the reason for running
  Loogle locally
  at all — the hosted service has neither in its index, and a search that cannot see a library
  the verifier can is worse than useless, because its silence looks like an answer;
* an absent binary is *reported* rather than quietly answered from the hosted index, whose
  contents describe a different Mathlib. The hosted service stays reachable, but only when
  asked for by name.

Skipped when no local Loogle is built, so this is safe in a commit hook.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.lean

from action_speaks import search as S  # noqa: E402
from action_speaks.config import Config  # noqa: E402

# One declaration from each library in the prelude, to prove a single index spans all three.
# Neither Physlib's nor Cslib's root module imports all of Mathlib, so indexing any one of
# them alone leaves a hole; `lean/ActionSpeaksAll.lean` exists precisely to close it.
LIBRARIES = [
    ("physlib", "ClassicalMechanics.FreeParticle.linearMomentum", "Physlib."),
    ("mathlib", "|- Irrational (Real.sqrt _)", "Mathlib."),
    ("cslib", "Cslib.LambdaCalculus.LocallyNameless.Untyped.Term.confluent_fullBeta", "Cslib."),
    ("floatlib", "FloatLib.Numerics.LimbArray.size_shiftLeft", "FloatLib."),
]


@pytest.fixture(scope="module")
def config() -> Config:
    try:
        return Config.discover()
    except Exception as exc:  # pragma: no cover - environment specific
        pytest.fail(f"cannot read configuration: {exc}")


@pytest.fixture(scope="module")
def session(config: Config) -> Iterator[S.LoogleSession]:
    if not config.loogle_bin or not Path(config.loogle_bin).exists():
        pytest.skip("no local loogle built (scripts/build-loogle.sh)")
    session = S.local_loogle_session()
    yield session
    session.close()


@pytest.mark.parametrize(
    ("label", "query", "expect_module"), LIBRARIES, ids=[x[0] for x in LIBRARIES]
)
def test_local_index_reaches_every_prelude_library(
    session: S.LoogleSession, label: str, query: str, expect_module: str
) -> None:
    result = session.query(query, limit=5)
    modules = [hit.module for hit in result.hits]
    assert result.hits, f"{label}: no hits for {query}: {result.error}"
    assert any(module.startswith(expect_module) for module in modules), (
        f"{label}: expected a hit from {expect_module}*, got {modules or result.error}"
    )


def test_absent_binary_reports_itself_rather_than_substituting(monkeypatch) -> None:
    """With no binary, shape search must say so rather than answering from another library."""
    monkeypatch.setenv("ACTION_SPEAKS_LOOGLE_BIN", "/nonexistent/loogle")
    monkeypatch.setattr(S, "_local_loogle", None)  # force rediscovery with the patched environment
    absent = S.loogle("Nat.succ_le_succ", limit=1)
    assert not absent.hits
    assert absent.error and "loogle-remote" in absent.error, (
        "an absent local index should report itself and point at backend='loogle-remote', "
        f"got backend={absent.backend} hits={len(absent.hits)} error={absent.error!r}"
    )


def test_explicit_remote_routes_to_the_hosted_service() -> None:
    """A network failure is not this test's business, so only the routing is asserted."""
    explicit = S.search("Nat.succ_le_succ", limit=1, backend="loogle-remote")
    assert len(explicit) == 1
    assert explicit[0].backend == "loogle"

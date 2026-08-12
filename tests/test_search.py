"""Check the local Loogle backend, when one has been built.

Shape search is the route an agent reaches for before it knows a lemma's name, so a silent
regression here is expensive: the agent falls back to guessing, which is the failure mode
the whole tool exists to prevent.

Two properties matter and neither is covered elsewhere:

* the local index reaches Physlib, which is the reason for running Loogle locally at all —
  the hosted service has no Physlib in its index, and a search that cannot see a library the
  verifier can is worse than useless, because its silence looks like an answer;
* an absent binary is *reported* rather than quietly answered from the hosted index, whose
  contents describe a different Mathlib. The hosted service stays reachable, but only when
  asked for by name.

Skipped (exit 0) when no local Loogle is built, so this is safe in a commit hook.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nullius import search as S  # noqa: E402
from nullius.config import Config  # noqa: E402

# A Physlib declaration and a Mathlib one, to prove a single index spans both. Physlib's
# root module does not import all of Mathlib, so indexing either library alone leaves a hole;
# `lean/NulliusAll.lean` exists precisely to close it.
PHYSLIB_QUERY = "ClassicalMechanics.FreeParticle.linearMomentum"
MATHLIB_QUERY = "|- Irrational (Real.sqrt _)"


def main() -> int:
    try:
        cfg = Config.discover()
    except Exception as exc:
        print(f"cannot read configuration: {exc}")
        return 1

    if not cfg.loogle_bin or not Path(cfg.loogle_bin).exists():
        print("no local loogle built (scripts/build-loogle.sh) — skipping")
        return 0

    failures: list[str] = []
    session = S.local_loogle_session()

    for label, query, expect_module in (
        ("physlib", PHYSLIB_QUERY, "Physlib."),
        ("mathlib", MATHLIB_QUERY, "Mathlib."),
    ):
        res = session.query(query, limit=5)
        modules = [h.module for h in res.hits]
        ok = bool(res.hits) and any(m.startswith(expect_module) for m in modules)
        print(f"  {'ok  ' if ok else 'FAIL'}  {label:8s} {len(res.hits)} hit(s)  {query}")
        if not ok:
            failures.append(
                f"{label}: expected a hit from {expect_module}*, got {modules or res.error}"
            )

    session.close()

    # With no binary, shape search must say so rather than answering from a different
    # library. The hosted service has to be asked for by name.
    os.environ["NULLIUS_LOOGLE_BIN"] = "/nonexistent/loogle"
    S._local_loogle = None  # force rediscovery with the patched environment
    absent = S.loogle("Nat.succ_le_succ", limit=1)
    ok = not absent.hits and bool(absent.error) and "loogle-remote" in (absent.error or "")
    print(f"  {'ok  ' if ok else 'FAIL'}  absent binary reports, does not substitute")
    if not ok:
        failures.append(
            "an absent local index should report itself and point at backend='loogle-remote', "
            f"got backend={absent.backend} hits={len(absent.hits)} error={absent.error!r}"
        )

    # ...and the hosted service must still be reachable when named explicitly. A network
    # failure is not this test's business, so only the routing is asserted.
    explicit = S.search("Nat.succ_le_succ", limit=1, backend="loogle-remote")
    ok = len(explicit) == 1 and explicit[0].backend == "loogle"
    print(f"  {'ok  ' if ok else 'FAIL'}  explicit remote routes to the hosted service")
    if not ok:
        failures.append(f"backend='loogle-remote' should query the hosted service, got {explicit}")

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("Local shape search covers Mathlib and Physlib; the hosted index is never implicit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The entry point for programmatic use.

`Harness` owns a pool of warm Lean sessions and is safe to call from several threads. Create
one per process, keep it for the lifetime of the run, and call `verify` as often as you like:
the Mathlib import is paid once per pooled session, not once per call.

    from nullius import Harness

    with Harness(pool_size=4) as h:
        v = h.verify("theorem t (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
                     claim="if n > 5 then n^2 > 25")
        if v.verified:
            print(v.statement)

For a repair loop, feed `v.feedback()` back to the model and resubmit; `prove` does exactly
that for you given a callable that produces Lean source.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from . import search as S
from .config import Config
from .ledger import Ledger
from .repl import SessionPool, borrow
from .verify import Verdict, Verifier


@dataclass
class Attempt:
    """One round of a repair loop."""

    round: int
    source: str
    verdict: Verdict


class Harness:
    """Thread-safe, pooled access to the verifier."""

    def __init__(
        self,
        pool_size: int | None = None,
        config: Config | None = None,
        log: bool = True,
        tag: str | None = None,
    ):
        self.config = config or Config.discover()
        self.pool = SessionPool(self.config, size=pool_size)
        self.ledger = Ledger(self.config.ledger_path) if log else None
        self.tag = tag
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def warm(self) -> "Harness":
        """Start every pooled session now, so the first `verify` is not slow."""
        self.pool.warm()
        return self

    def close(self) -> None:
        self.pool.close()
        # Also release the local Loogle process and its in-memory index, if search was used.
        S.local_loogle_session().close()

    def __enter__(self) -> "Harness":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- verification ------------------------------------------------------

    def verify(
        self,
        source: str,
        claim: str | None = None,
        target: str | None = None,
        require_nontrivial: bool = False,
        check_vacuity: bool = True,
        timeout: float | None = None,
        tag: str | None = None,
    ) -> Verdict:
        """Verify one submission. Blocks until a pooled session is free."""
        with borrow(self.pool) as session:
            verdict = Verifier(session, self.config).verify(
                source,
                target=target,
                claim=claim,
                require_nontrivial=require_nontrivial,
                check_vacuity=check_vacuity,
                timeout=timeout,
            )
        if self.ledger is not None:
            with self._lock:
                self.ledger.record(verdict, source, tag=tag or self.tag)
        return verdict

    def verify_many(
        self,
        items: Sequence[str | dict[str, Any]],
        max_workers: int | None = None,
        **kwargs: Any,
    ) -> list[Verdict]:
        """Verify a batch concurrently, preserving input order.

        Each item is either Lean source, or a dict of keyword arguments for `verify`
        (`{"source": ..., "claim": ...}`). Concurrency is capped at the pool size, since
        each in-flight check needs its own Lean session.
        """
        workers = min(max_workers or self.pool.size, self.pool.size, max(1, len(items)))

        def one(item: str | dict[str, Any]) -> Verdict:
            if isinstance(item, str):
                return self.verify(item, **kwargs)
            merged = {**kwargs, **item}
            return self.verify(**merged)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(one, items))

    def prove(
        self,
        propose: Callable[[str | None, int], str],
        claim: str | None = None,
        max_rounds: int = 3,
        **kwargs: Any,
    ) -> tuple[Verdict, list[Attempt]]:
        """Run a repair loop.

        `propose(feedback, round)` returns Lean source; it is called with `None` on the first
        round and with the previous verdict's `feedback()` thereafter. Returns the final
        verdict and the full history, so a caller can see how many rounds it took and what
        went wrong.
        """
        history: list[Attempt] = []
        feedback: str | None = None
        verdict: Verdict | None = None
        for r in range(max_rounds):
            source = propose(feedback, r)
            verdict = self.verify(source, claim=claim, **kwargs)
            history.append(Attempt(r, source, verdict))
            if verdict.verified:
                break
            feedback = verdict.feedback()
        assert verdict is not None
        return verdict, history

    # -- pre-flight and search --------------------------------------------

    def check_statement(self, statement: str, timeout: float | None = None) -> dict[str, Any]:
        """Elaborate a statement without proving it; reports contradictory hypotheses."""
        with borrow(self.pool) as session:
            return Verifier(session, self.config).check_statement(statement, timeout=timeout)

    def search(self, query: str, backend: str = "both", limit: int = 8) -> list[S.SearchResult]:
        """Find a lemma. `loogle` is the local index; `loogle-remote` the hosted service."""
        return S.search(query, limit=limit, backend=backend)

    def find_proof(
        self,
        goal: str,
        binders: str = "",
        tactics: tuple[str, ...] = ("exact?", "apply?"),
    ) -> S.SearchResult:
        """Ask Lean what closes `goal`. Cannot hallucinate a lemma name."""
        with borrow(self.pool) as session:
            return S.local_search(session, goal, tactics=tactics, binders=binders)

    # -- introspection -----------------------------------------------------

    def provenance(self) -> dict[str, str]:
        return self.config.provenance()

    def stats(self) -> dict[str, Any]:
        return self.ledger.stats() if self.ledger else {"ledger": "disabled"}


def verify_once(source: str, claim: str | None = None, **kwargs: Any) -> Verdict:
    """One-shot convenience for scripts. Starts and tears down a session, so it pays the
    Mathlib import every call - use `Harness` for anything repeated.

    Named `verify_once` rather than `verify` so that it does not shadow the `nullius.verify`
    module when re-exported from the package.
    """
    with Harness(pool_size=1, **{k: kwargs.pop(k) for k in ("config", "log") if k in kwargs}) as h:
        return h.verify(source, claim=claim, **kwargs)

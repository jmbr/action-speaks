"""The entry point for programmatic use.

`Harness` owns a pool of warm Lean sessions and is safe to call from several threads. Create
one per process, keep it for the lifetime of the run, and call `verify` as often as you like:
the Mathlib import is paid once per pooled session, not once per call.

    from action_speaks import Harness

    with Harness(pool_size=4) as h:
        v = h.verify("theorem t (n : Nat) (h : 5 < n) : 25 < n * n := by nlinarith",
                     claim="if n > 5 then n^2 > 25")
        if v.verified:
            print(v.statement)

For a repair loop, feed `v.feedback()` back to the model and resubmit; `prove` does exactly
that for you given a callable that produces Lean source.

`Harness(project="name")` selects a registered project's ledger without changing snippet
imports. `verify_module("Module", "target")` audits the current checkout instead; builds
require an explicit `build=True` and a trusted registration.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import search as S
from .config import Config
from .ledger import Ledger
from .ledger_transfer import read_entry_reference
from .project_check import verify_project
from .repl import SessionPool, borrow
from .verify import Verdict, Verifier


def validate_module_request(config: Config, module: str, target: str) -> None:
    """Reject incomplete or untrusted project execution before opening a ledger."""
    if config.project_id is None:
        raise ValueError("module verification requires a registered project")
    if not config.project_trusted:
        raise ValueError("project is untrusted; register it with --trust before executing Lean")
    if not isinstance(module, str) or not module.strip():
        raise ValueError("module must be a nonempty Lean module name")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("module verification requires a target declaration")


def validate_derived_from(config: Config, reference: str | None, *, log: bool) -> str | None:
    """Resolve a prior entry to its stable origin without creating or migrating a ledger."""
    if reference is None:
        return None
    if not log:
        raise ValueError("derived_from requires ledger access, which is disabled (log=False)")
    if config.project_id is None:
        raise ValueError("derived_from requires a registered project")
    row = read_entry_reference(config.ledger_path, reference)
    if row is None:
        raise ValueError(f"ledger reference {reference!r} not found in this project")
    return row.get("reference") or row.get("origin") or reference


def filter_project_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    status: str | None = None,
    tag: str | None = None,
    recall: str | None = None,
) -> list[dict[str, Any]]:
    """Filter merged history in memory, keeping project log reads migration-free."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if status is not None and status not in ("verified", "rejected", "error"):
        raise ValueError("status must be verified, rejected or error")
    if tag is not None and not isinstance(tag, str):
        raise ValueError("tag must be a string")
    if recall is not None and not isinstance(recall, str):
        raise ValueError("recall must be a string")
    words = re.findall(r"\w+", recall.casefold())[:12] if recall is not None else []
    selected = []
    for row in rows:
        if status is not None and row["status"] != status:
            continue
        if tag is not None and row.get("tag") != tag:
            continue
        if recall is not None:
            text = f"{row.get('claim') or ''} {row.get('statement') or ''}".casefold()
            if not row["verified"] or not any(word in text for word in words):
                continue
        selected.append(row)
    selected.sort(key=lambda row: (row.get("created_at") or 0, row["id"]), reverse=True)
    return selected[:limit]


def project_history_stats(rows: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    return {
        "total": len(rows),
        "verified": sum(bool(row["verified"]) for row in rows),
        "by_status": dict(Counter(row["status"] for row in rows)),
        "path": str(path),
    }


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
        *,
        project: str | Path | None = None,
        pool: SessionPool | None = None,
    ):
        self.config = config or Config.discover()
        if project is not None:
            self.config = self.config.for_project(project)
        self.ledger = Ledger(self.config.ledger_path) if log else None
        if self.ledger is not None and self.config.project_id is not None:
            self.ledger.bind_project(self.config.project_id)
        # A borrowed pool outlives this harness. Selecting a project changes the ledger and
        # the project metadata, not `lean_dir`, `repl_bin` or the prelude, so a pooled session
        # is the same session whichever project a caller names, and a server can hand its one
        # warm pool to every request rather than starting a second set of Lean processes.
        self.owns_pool = pool is None
        self.pool = pool if pool is not None else SessionPool(self.config, size=pool_size)
        self.tag = tag
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def warm(self) -> "Harness":
        """Start every pooled session now, so the first `verify` is not slow."""
        self.pool.warm()
        return self

    def close(self) -> None:
        try:
            if self.owns_pool:
                self.pool.close()
        finally:
            S.close_sessions(self.config)

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
                if self.config.project_id is None:
                    self.ledger.record(verdict, source, tag=tag or self.tag)
                else:
                    self.ledger.record(
                        verdict, source, tag=tag or self.tag, project_id=self.config.project_id
                    )
        return verdict

    def verify_module(
        self,
        module: str,
        target: str,
        *,
        claim: str | None = None,
        require_nontrivial: bool = False,
        build: bool = False,
        timeout: float | None = None,
        tag: str | None = None,
        derived_from: str | None = None,
    ) -> Verdict:
        """Audit a target in the current project checkout; building is opt-in."""
        validate_module_request(self.config, module, target)
        origin = validate_derived_from(self.config, derived_from, log=self.ledger is not None)
        verdict = verify_project(
            self.config,
            module,
            target,
            claim=claim,
            require_nontrivial=require_nontrivial,
            build=build,
            timeout=timeout,
        )
        if self.ledger is not None:
            with self._lock:
                self.ledger.record(
                    verdict,
                    "",
                    tag=tag or self.tag,
                    record_kind="project",
                    project_id=self.config.project_id,
                    module=module,
                    environment_id=verdict.provenance.get("environment_id"),
                    derived_from=origin,
                )
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
        """Elaborate a statement without proving it; reports contradictory hypotheses.

        Also consults the ledger: by this point Lean has produced the elaborated statement,
        which finds the same theorem however it was previously written. Hooking the step the
        agent already takes beats adding one it must remember.
        """
        with borrow(self.pool) as session:
            res = Verifier(session, self.config).check_statement(statement, timeout=timeout)
        if self.ledger is not None and res.get("ok"):
            hits = self.ledger.recall(
                statement=res.get("statement"),
                text=statement,
                current=self.config.provenance(),
            )
            res["recall"] = [h.render() for h in hits]
        return res

    def recall(
        self,
        source: str | None = None,
        statement: str | None = None,
        text: str | None = None,
        limit: int = 4,
    ) -> list[Any]:
        """Prior verifications bearing on a claim. Pointers to re-check, never evidence."""
        if self.ledger is None:
            return []
        return self.ledger.recall(
            source=source,
            statement=statement,
            text=text,
            limit=limit,
            current=self.config.provenance(),
        )

    def search(
        self,
        query: str,
        backend: str = "both",
        limit: int = 8,
        *,
        include_ledger: bool = False,
        refresh_ledger: bool = False,
    ) -> list[S.SearchResult]:
        """Find a lemma; ledger access is opt-in and unavailable with `log=False`."""
        if self.ledger is None and (backend == "ledger" or include_ledger or refresh_ledger):
            raise ValueError("ledger access is disabled (Harness(log=False))")
        return S.search(
            query,
            limit=limit,
            backend=backend,
            include_ledger=include_ledger,
            refresh_ledger=refresh_ledger,
            config=self.config,
        )

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

    Named `verify_once` rather than `verify` so that it does not shadow the `action_speaks.verify`
    module when re-exported from the package.
    """
    with Harness(
        pool_size=1, **{k: kwargs.pop(k) for k in ("config", "log", "project") if k in kwargs}
    ) as h:
        return h.verify(source, claim=claim, **kwargs)

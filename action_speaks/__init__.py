"""action-speaks: machine-checked backing for an agent's mathematical claims.

Typical use from a harness:

    from action_speaks import Harness

    with Harness(pool_size=4).warm() as h:
        v = h.verify(source, claim="...")
        if not v.verified:
            retry(v.feedback())
"""

from .config import Config, ConfigError
from .harness import Attempt, Harness, verify_once
from .ledger import Ledger, Recollection
from .repl import ReplError, ReplTimeout, Session, SessionPool
from .search import (
    LoogleSession,
    SearchResult,
    leansearch,
    local_loogle_session,
    local_search,
    loogle,
    loogle_remote,
)
from .verify import Check, Status, Verdict, Verifier

__version__ = "0.2.2"

__all__ = [
    "Harness",
    "Attempt",
    "verify_once",
    "Verdict",
    "Verifier",
    "Check",
    "Status",
    "Session",
    "SessionPool",
    "ReplError",
    "ReplTimeout",
    "Config",
    "ConfigError",
    "Ledger",
    "Recollection",
    "SearchResult",
    "loogle",
    "loogle_remote",
    "leansearch",
    "local_search",
    "LoogleSession",
    "local_loogle_session",
    "__version__",
]

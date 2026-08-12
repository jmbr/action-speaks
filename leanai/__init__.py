"""lean-ai: machine-checked backing for an agent's mathematical claims.

Typical use from a harness:

    from leanai import Harness

    with Harness(pool_size=4).warm() as h:
        v = h.verify(source, claim="...")
        if not v.verified:
            retry(v.feedback())
"""

from .config import Config, ConfigError
from .harness import Attempt, Harness, verify_once
from .ledger import Ledger
from .repl import ReplError, ReplTimeout, Session, SessionPool
from .search import SearchResult, leansearch, local_search, loogle
from .verify import Check, Status, Verdict, Verifier

__version__ = "0.1.0"

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
    "SearchResult",
    "loogle",
    "leansearch",
    "local_search",
    "__version__",
]

"""Shared test setup.

The important thing here is that a test must never reach a daemon that happens to be running
on the developer's machine. `nullius verify` prefers one when it is listening, so without
this a CLI test would quietly exercise somebody else's warm sessions — and pass or fail for
reasons that have nothing to do with the code under test. `tests/test_daemon.py` clears this
again for the cases that are about the daemon itself.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_ambient_daemon(monkeypatch) -> None:
    monkeypatch.setenv("NULLIUS_NO_DAEMON", "1")

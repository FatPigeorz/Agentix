"""agentix.log — sandbox-side `logging` records ferried to the host.

This module is a thin bridge for the *third* observability pillar
(distinct from `agentix.trace`). Workers don't need a custom API: just
use stdlib `logging`:

    import logging
    logger = logging.getLogger(__name__)
    logger.info("hello from sandbox")

At worker boot, `install_worker_bridge()` adds a `logging.Handler` to
the root logger that emits each `LogRecord` on the `/log` SIO
namespace. The host's `RuntimeClient` auto-registers a consumer that
forwards records into the host's own `logging` system, so they appear
in host logs untouched.
"""

from __future__ import annotations

import logging

__all__ = ["install_worker_bridge"]


def install_worker_bridge(level: int = logging.NOTSET) -> logging.Handler:
    """Install the bridge handler on the root logger. Idempotent."""
    from agentix.log._bridge import WorkerLogHandler

    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, WorkerLogHandler):
            return h
    handler = WorkerLogHandler()
    handler.setLevel(level)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return handler

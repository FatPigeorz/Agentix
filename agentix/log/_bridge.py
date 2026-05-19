"""`/log` SIO namespace — worker handler + host replayer."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import socketio

from agentix import sio as _sio

NAMESPACE = "/log"


# ── worker side ───────────────────────────────────────────────────


class _WorkerLogNamespace(_sio.Namespace):
    namespace = NAMESPACE


_namespace_singleton: _WorkerLogNamespace | None = None


def _get_worker_namespace() -> _WorkerLogNamespace:
    global _namespace_singleton
    if _namespace_singleton is None:
        _namespace_singleton = _WorkerLogNamespace()
        _sio.register_namespace(_namespace_singleton)
    return _namespace_singleton


class WorkerLogHandler(logging.Handler):
    """Translate `LogRecord`s into `/log:record` events.

    Avoids self-recursion: `agentix.log` is excluded from forwarding to
    prevent feedback if our own debug logs were ever enabled.
    """

    _EXCLUDED_LOGGERS = ("agentix.sio", "agentix.log")

    def emit(self, record: logging.LogRecord) -> None:
        if any(record.name.startswith(prefix) for prefix in self._EXCLUDED_LOGGERS):
            return
        if not _sio._is_installed():
            return
        try:
            payload = _record_payload(record)
            ns = _get_worker_namespace()
            asyncio.get_running_loop().create_task(ns.emit("record", payload))
        except RuntimeError:
            # No running loop — drop the record (worker is between async ticks).
            pass
        except Exception:
            self.handleError(record)


# Fields LogRecord defines natively; everything else on `record.__dict__`
# is treated as a user-provided `extra={...}` field and forwarded.
_STD_RECORD_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
    }
)


def _record_payload(record: logging.LogRecord) -> dict[str, Any]:
    extras = {k: v for k, v in record.__dict__.items() if k not in _STD_RECORD_KEYS and not k.startswith("_")}
    return {
        "name": record.name,
        "level": record.levelname,
        "levelno": record.levelno,
        "message": record.getMessage(),
        "created": record.created,
        "pathname": record.pathname,
        "lineno": record.lineno,
        "funcName": record.funcName,
        "module": record.module,
        "exc_text": record.exc_text
        or (logging.Formatter().formatException(record.exc_info) if record.exc_info else None),
        "stack_info": record.stack_info,
        "extras": extras or None,
    }


# ── host side ─────────────────────────────────────────────────────


class HostLogNamespace(socketio.AsyncClientNamespace):
    """Replays inbound `/log:record` events into the host's `logging` tree.

    Each forwarded record is dispatched against the same logger name it
    had in the sandbox, so existing host-side handlers/formatters pick it
    up naturally.
    """

    def __init__(self) -> None:
        super().__init__(NAMESPACE)

    async def trigger_event(self, event: str, *args: Any) -> Any:
        if event in ("connect", "disconnect", "connect_error"):
            return
        if event != "record":
            return
        from agentix.runtime.client._sio_facade import _decode

        payload = _decode(args[0]) if args else None
        if not isinstance(payload, dict):
            return
        _replay_record(payload)


def _replay_record(payload: dict[str, Any]) -> None:
    logger = logging.getLogger(str(payload.get("name", "agentix.sandbox")))
    levelno = int(payload.get("levelno", logging.INFO))
    if not logger.isEnabledFor(levelno):
        return
    extras = payload.get("extras") or {}
    record = logger.makeRecord(
        name=logger.name,
        level=levelno,
        fn=str(payload.get("pathname", "")),
        lno=int(payload.get("lineno", 0)),
        msg=str(payload.get("message", "")),
        args=(),
        exc_info=None,
        extra=dict(extras),
    )
    record.funcName = str(payload.get("funcName", ""))
    record.module = str(payload.get("module", ""))
    if payload.get("exc_text"):
        record.exc_text = str(payload["exc_text"])
    if payload.get("stack_info"):
        record.stack_info = str(payload["stack_info"])
    logger.handle(record)


__all__ = ["HostLogNamespace", "WorkerLogHandler"]

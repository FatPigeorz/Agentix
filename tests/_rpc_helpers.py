from __future__ import annotations

import inspect
from typing import Any

from agentix.invoke import detect_shape
from agentix.runtime.shared.callables import display_name_for, dump_callable
from agentix.runtime.shared.models import RemoteRequest


def request_for(
    fn: Any,
    *,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
    call_id: str | None = None,
) -> RemoteRequest:
    sig = inspect.signature(fn, eval_str=True)
    return RemoteRequest(
        callable_payload=dump_callable(fn),
        display_name=display_name_for(fn),
        shape=detect_shape(fn, sig),
        args=args or [],
        kwargs=kwargs or {},
        call_id=call_id,
    )

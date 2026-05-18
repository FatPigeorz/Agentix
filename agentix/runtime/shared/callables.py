"""Callable serialization for remote execution.

`RuntimeClient.remote(fn, ...)` accepts any Python callable. The wire
always carries a stdlib pickle payload. Pickle is the Python-native
callable reference mechanism for module-level functions/classes and
pickleable callable objects. Lambdas and local closures are intentionally
outside this boundary.
"""

from __future__ import annotations

import pickle
from typing import Any


def display_name_for(fn: Any) -> str:
    """Best-effort name for logs and error messages."""
    module = getattr(fn, "__module__", None)
    qualname = getattr(fn, "__qualname__", None)
    if isinstance(module, str) and module and isinstance(qualname, str) and qualname:
        return f"{module}::{qualname}"
    name = getattr(fn, "__name__", None)
    if isinstance(module, str) and module and isinstance(name, str) and name:
        return f"{module}::{name}"
    cls = type(fn)
    cls_module = getattr(cls, "__module__", "")
    cls_qualname = getattr(cls, "__qualname__", cls.__name__)
    return f"{cls_module}::{cls_qualname}" if cls_module else cls_qualname


def dump_callable(fn: Any) -> bytes:
    """Serialize a callable for execution in the runtime worker."""
    if not callable(fn):
        raise TypeError(f"remote value must be callable (got {type(fn).__name__})")
    return pickle.dumps(fn)


def load_callable(payload: bytes) -> Any:
    """Deserialize a callable sent by the client."""
    fn = pickle.loads(payload)
    if not callable(fn):
        raise TypeError(f"serialized remote value is not callable (got {type(fn).__name__})")
    return fn


__all__ = ["display_name_for", "dump_callable", "load_callable"]

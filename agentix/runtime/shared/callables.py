"""Callable identification + serialization.

`RemoteCallable` is the wire type for a remote callable: a `str`
subclass that stores `base64(pickle.dumps(fn))`. Both ends use its
classmethod / instance method to cross the wire — no separate free
functions.

Round-trip works for anything `pickle.dumps` handles: top-level
functions (carries `module::qualname`), bound methods (carries instance),
`functools.partial` (carries bound args), and callable instances.
Lambdas and local closures are intentionally outside the boundary —
pickle can't serialize them.

`display_name_for(fn)` is a host/worker-local helper for log lines and
error messages. It is not shipped on the wire — both ends recompute it
from their own fn reference.
"""

from __future__ import annotations

import base64
import pickle
from collections.abc import Callable
from typing import Any


def display_name_for(fn: Any) -> str:
    """Best-effort name for logs, error messages, and span attrs."""
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


class RemoteCallable(str):
    """Wire form of a remote callable: a string carrying
    `base64(pickle.dumps(fn))`.

    Subclasses `str` so it's directly serializable via msgpack / json /
    any text protocol with no special handling. Use the classmethod to
    construct one from a local fn, and `resolve()` to recover the fn
    on the receiving end.
    """

    __slots__ = ()

    @classmethod
    def _resolve(cls, fn: Callable[..., Any]) -> RemoteCallable:
        """Encode a Python callable as a `RemoteCallable` string."""
        if not callable(fn):
            raise TypeError(f"remote value must be callable (got {type(fn).__name__})")
        encoded = base64.b64encode(pickle.dumps(fn)).decode("ascii")
        return cls(encoded)

    def resolve(self) -> Callable[..., Any]:
        """Decode this string back into a Python callable."""
        fn = pickle.loads(base64.b64decode(self.encode("ascii")))
        if not callable(fn):
            raise TypeError(f"resolved value is not callable (got {type(fn).__name__})")
        return fn


__all__ = ["RemoteCallable", "display_name_for"]

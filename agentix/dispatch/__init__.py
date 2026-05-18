"""Server-side RPC dispatch.

`Dispatcher(target).dispatch(req)` looks up `req.method` on `target` and
invokes it; methods bind lazily on first call (TypeAdapter compile is
cached per method).

Split into:

  - `shape`       — call-shape detection (`unary` / `stream` / `bidi`)
  - `bound`       — `_BoundMethod` record + arg coercion helper
  - `dispatcher`  — the `Dispatcher` class itself

Public surface: `Dispatcher`. The shape module is also exported for
clients that want to probe a callable's wire shape host-side without
binding.
"""

from agentix.dispatch.dispatcher import Dispatcher
from agentix.dispatch.shape import Shape, detect_shape

__all__ = [
    "Dispatcher",
    "Shape",
    "detect_shape",
]

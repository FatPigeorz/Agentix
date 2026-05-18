"""Server-side callable invocation helpers.

`FunctionInvoker().call_unary(fn, req)` validates args against `fn`'s
signature, invokes the callable, and serializes the result.

Split into:

  - `shape`       — call-shape detection (`unary` / `stream` / `bidi`)
  - `bound`       — `_BoundMethod` record + arg coercion helper
  - `invoker`     — the `FunctionInvoker` class itself

Internal surface: `FunctionInvoker` and `detect_shape`.
"""

from agentix.invoke.invoker import FunctionInvoker
from agentix.invoke.shape import Shape, detect_shape

__all__ = [
    "FunctionInvoker",
    "Shape",
    "detect_shape",
]

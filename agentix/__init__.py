"""agentix — typed RPC for sandboxed Python namespaces.

`agentix` is a namespace-extensible regular package. The framework
ships its own subpackages (`agentix.cli`, `agentix.dispatch`,
`agentix.runtime`, ...); third-party namespaces contribute additional
subpackages under `agentix.<short>` (e.g. `agentix.bash`). The
`pkgutil.extend_path` call below makes
`agentix.__path__` aggregate every `agentix/` directory on `sys.path`,
so a namespace dist installing files at `<site-packages>/agentix/bash/`
becomes importable as `from agentix.bash import Bash`. Reserved
framework subpackages are listed in CLAUDE.md.
"""

import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)

from agentix.dispatch import Dispatcher
from agentix.rpc import Bidi, Channel, RemoteCall, Stream, Unary
from agentix.runtime.client import RemoteCallError, RuntimeClient

__version__ = "0.1.0"

__all__ = [
    "Bidi",
    "Channel",
    "Dispatcher",
    "RemoteCall",
    "RemoteCallError",
    "RuntimeClient",
    "Stream",
    "Unary",
    "__version__",
]

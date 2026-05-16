"""agentix — typed Python namespaces for sandbox-based agent workflows.

`agentix` is a namespace-extensible regular package. The framework
ships its own subpackages (`agentix.cli`, `agentix.dispatch`,
`agentix.runtime`, …); third-party namespaces contribute additional
subpackages under `agentix.<short>` (e.g. `agentix.bash`,
`agentix.claude_code`). The `pkgutil.extend_path` call below makes
`agentix.__path__` aggregate every `agentix/` directory on `sys.path`,
so a namespace dist installing files at `<site-packages>/agentix/bash/`
becomes importable as `from agentix.bash import Bash`. Reserved
framework subpackages are listed in CLAUDE.md.
"""

import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)

# `trace` is imported eagerly so namespace impls can `from agentix import trace`
# without circular-import gymnastics. It has no runtime deps and registers an
# emitter only when the server boots, so this is cheap.
from agentix import trace
from agentix.deployment.base import Sandbox
from agentix.deployment.docker import DockerDeployment
from agentix.dispatch import Dispatcher
from agentix.models import SandboxConfig, SandboxInfo
from agentix.rollout import RolloutPool
from agentix.rpc import Bidi, Channel, RemoteCall, Stream, Unary
from agentix.runtime.client import RemoteCallError, RuntimeClient
from agentix.runtime.models import LogRecord, TraceEvent

__version__ = "0.1.0"

__all__ = [
    "Bidi",
    "Channel",
    "Dispatcher",
    "DockerDeployment",
    "LogRecord",
    "RemoteCall",
    "RemoteCallError",
    "RolloutPool",
    "RuntimeClient",
    "Sandbox",
    "SandboxConfig",
    "SandboxInfo",
    "Stream",
    "TraceEvent",
    "Unary",
    "__version__",
    "trace",
]

"""Abstract deployment interface: sandbox CRUD."""

from __future__ import annotations

from abc import ABC, abstractmethod

from agentix.models import SandboxConfig, SandboxInfo


class Deployment(ABC):
    """Sandbox lifecycle management.

    Each infrastructure backend (Docker, K8s, Daytona, Modal)
    implements this interface. The orchestrator doesn't care
    which one is used.
    """

    @abstractmethod
    async def create(self, config: SandboxConfig) -> SandboxInfo:
        """Create a sandbox.

        One step:
        1. Create container/sandbox from task_image
        2. Inject runtime closure
        3. Inject agent closure
        4. Set PATH
        5. Start agentix-server

        Returns SandboxInfo with runtime_url for HTTP communication.
        """

    @abstractmethod
    async def get(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox status."""

    @abstractmethod
    async def update(self, sandbox_id: str, config: SandboxConfig) -> SandboxInfo:
        """Update sandbox (e.g. swap agent closure)."""

    @abstractmethod
    async def delete(self, sandbox_id: str) -> None:
        """Destroy sandbox and release resources."""

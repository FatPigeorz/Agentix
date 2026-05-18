"""Real importable target for the subprocess worker tests.

Lives in tests/ so the worker subprocess can `import _worker_target`
after we add tests/ to PYTHONPATH (no pip install required).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import BaseModel


class EchoResult(BaseModel):
    msg: str


class Echo:
    @staticmethod
    async def echo(msg: str) -> EchoResult:
        return EchoResult(msg=f"echo:{msg}")

    @staticmethod
    async def counter(n: int) -> AsyncIterator[int]:
        for i in range(n):
            yield i

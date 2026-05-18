"""Real importable target for the subprocess worker tests.

Lives in tests/ so the worker subprocess can import
`tests._worker_target` without a separate package install.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from pydantic import BaseModel

from agentix import Channel


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


async def echo(msg: str) -> EchoResult:
    return await Echo.echo(msg)


async def counter(n: int) -> AsyncIterator[int]:
    async for item in Echo.counter(n):
        yield item


async def boom() -> str:
    raise RuntimeError("kaboom")


def add(a: int, b: int) -> int:
    return a + b


class Prefixer:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    async def __call__(self, msg: str) -> EchoResult:
        return EchoResult(msg=f"{self.prefix}:{msg}")

    async def bound(self, msg: str) -> EchoResult:
        return EchoResult(msg=f"bound:{self.prefix}:{msg}")


prefixer = Prefixer("instance")


class UserMsg(BaseModel):
    text: str


class ReplyMsg(BaseModel):
    text: str


async def chat(messages: Channel[UserMsg], prefix: str = "say:") -> AsyncIterator[ReplyMsg]:
    async for message in messages:
        yield ReplyMsg(text=f"{prefix}{message.text}")


async def slow_counter() -> AsyncIterator[int]:
    while True:
        await asyncio.sleep(1)
        yield 1

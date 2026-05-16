"""Files primitive — sandbox file upload / download as an Agentix namespace.

Usage:

    from agentix import RuntimeClient
    from agentix.files import Files

    async with RuntimeClient(sandbox.runtime_url) as c:
        r = await c.remote(Files.upload, path="/workspace/input.txt", content=b"hello")
        print(r.size)

        data = await c.remote(Files.download, path="/workspace/output.txt")

Files are encoded as pydantic `bytes` (base64 in the JSON wire form).
Suitable for kB–MB sized files; very large blobs should ship via a
purpose-built binary `WirePattern` rather than the unary JSON path.

One-file namespace: the `Files` class carries its method bodies directly,
no `_impl.py` split. Namespaces with heavier deps use lazy imports inside
methods rather than a stub/impl two-file pattern.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agentix.namespace import Namespace

UPLOAD_ROOT = Path(os.environ.get("AGENTIX_UPLOAD_ROOT", "/workspace")).resolve()


@dataclass
class UploadResult:
    """What `Files.upload` returns — the resolved sandbox-side path and
    the number of bytes written."""

    path: str
    size: int


def _resolve_within(path: str) -> Path:
    """Return `path` resolved, asserting it stays inside `UPLOAD_ROOT`.

    The resolve-before-open pattern is race-free: a symlink-after-check
    swap can only land on a path the resolver was already happy with.
    """
    p = Path(path).resolve()
    if not p.is_relative_to(UPLOAD_ROOT):
        raise PermissionError(f"Path {p} outside allowed root {UPLOAD_ROOT}")
    return p


class Files(Namespace):
    """Sandbox file I/O primitive.

    Writes/reads are confined to `$AGENTIX_UPLOAD_ROOT` (default
    `/workspace`). Paths outside that root raise `PermissionError`.
    """

    @staticmethod
    async def upload(path: str, content: bytes) -> UploadResult:
        """Write `content` to `path` inside the sandbox.

        Creates parent directories as needed. `path` must resolve under
        the upload-root; otherwise raises `PermissionError`.
        """
        p = _resolve_within(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return UploadResult(path=str(p), size=len(content))

    @staticmethod
    async def download(path: str) -> bytes:
        """Read the contents of `path` from inside the sandbox.

        Raises `FileNotFoundError` / `IsADirectoryError` /
        `PermissionError` for the corresponding filesystem conditions.
        """
        p = _resolve_within(path)
        return p.read_bytes()

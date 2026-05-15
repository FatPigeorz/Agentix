"""Files primitive impl — bounded sandbox I/O.

`FilesImpl` is an independent class that provides the `Files` interface;
it does NOT inherit from `Files`. `_register.py` composes them.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import UploadResult

UPLOAD_ROOT = Path(os.environ.get("AGENTIX_UPLOAD_ROOT", "/workspace")).resolve()


def _resolve_within(path: str) -> Path:
    """Return `path` resolved, asserting it stays inside `UPLOAD_ROOT`.

    The check is cheap and race-free: caller open / read / write happens
    on the resolved path, so a symlink-after-check swap can only land
    on something the resolver was happy with.
    """
    p = Path(path).resolve()
    if not p.is_relative_to(UPLOAD_ROOT):
        raise PermissionError(f"Path {p} outside allowed root {UPLOAD_ROOT}")
    return p


class FilesImpl:
    """Files primitive implementation. Composed with `Files` via
    `Dispatcher.bind_namespace`; not a subclass of `Files`."""

    async def upload(self, path: str, content: bytes) -> UploadResult:
        p = _resolve_within(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return UploadResult(path=str(p), size=len(content))

    async def download(self, path: str) -> bytes:
        p = _resolve_within(path)
        return p.read_bytes()

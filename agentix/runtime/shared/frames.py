"""Wire-frame type tags + call-kind tags for `agentix.runtime.shared.rpc`.

These are the values of the `type` (and `kind`) fields in msgpack frames
flowing between the runtime multiplexer and namespace workers over
stdin/stdout. Keeping them in one place means a typo is an
`AttributeError` at import time, not a silent protocol break at runtime.
"""

from __future__ import annotations

# ─── runtime → worker frame types ─────────────────────────────────────
CALL = "call"
BIDI_IN = "bidi_in"
BIDI_END_IN = "bidi_end_in"
CANCEL = "cancel"
SHUTDOWN = "shutdown"

# ─── worker → runtime frame types ─────────────────────────────────────
READY = "ready"
BOOT_ERROR = "boot_error"
RESULT = "result"
ERROR = "error"
STREAM_ITEM = "stream_item"
STREAM_END = "stream_end"

# ─── call kinds (the `kind` field of a `call` frame) ──────────────────
KIND_UNARY = "unary"
KIND_STREAM = "stream"
KIND_BIDI = "bidi"

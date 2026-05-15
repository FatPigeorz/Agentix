"""Socket.IO event-name constants — the single source of truth for the
strings on the wire between RuntimeClient and the runtime server.

Both `agentix.runtime.server.sio` and `agentix.runtime.client.client`
import from here. Typing one of these strings inline in either file
risks a silent client/server mismatch.

Naming convention: the constant name mirrors the event-name string
(`STREAM_ITEM` → `"stream:item"`). Use `EVENT_*` only if disambiguating
is needed (currently it isn't).
"""

from __future__ import annotations

# Server-streaming call (one request → many output items)
STREAM = "stream"
STREAM_ITEM = "stream:item"
STREAM_END = "stream:end"
STREAM_ERROR = "stream:error"

# Bidirectional call (many input items → many output items, interleaved)
BIDI_START = "bidi:start"
BIDI_IN = "bidi:in"
BIDI_END_IN = "bidi:end_in"
BIDI_OUT = "bidi:out"
BIDI_END = "bidi:end"
BIDI_ERROR = "bidi:error"

# Cancel an in-flight stream / bidi call by call_id.
CANCEL = "cancel"

# Log subscription (Python logging records broadcast to subscribers).
LOG = "log"
LOGS_SUBSCRIBE = "logs:subscribe"
LOGS_UNSUBSCRIBE = "logs:unsubscribe"

# Trace subscription (RL / observability events).
TRACE = "trace"
TRACES_SUBSCRIBE = "traces:subscribe"
TRACES_UNSUBSCRIBE = "traces:unsubscribe"

# Socket.IO room name for trace subscribers.
TRACES_ROOM = "traces"

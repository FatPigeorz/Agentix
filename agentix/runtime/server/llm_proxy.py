"""LLM-provider reverse-proxy mounted on the runtime server.

Closure SDKs are pointed at `http://127.0.0.1:8000/_llm/<provider>/...`
instead of the provider's real base URL. Every call forwards verbatim
to the upstream and emits two trace events:

  llm_request   {provider, method, path, body}
  llm_response  {provider, status, body}

A streaming response body is captured opportunistically — bytes up to
`AGENTIX_LLM_PROXY_TRACE_LIMIT` are recorded once the stream finishes.
Auth headers from the caller (API key, OAuth) pass through unchanged;
none of that state lives in this module.

Kept as its own router so the runtime's core built-ins (exec / upload /
download) aren't intermixed with what is an observability / RL surface.
"""

from __future__ import annotations

import json
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

import agentix.trace as _trace

_LLM_UPSTREAMS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai":    "https://api.openai.com",
}

_BODY_LIMIT = int(os.environ.get("AGENTIX_LLM_PROXY_TRACE_LIMIT", str(64 * 1024)))

# Module-level httpx client reuses the connection pool across the many
# proxy calls a single RL run generates. Closed when the runtime tears
# down (the process exit is good enough — no explicit aclose hook needed).
_client = httpx.AsyncClient(timeout=None)

router = APIRouter()


def _trace_body(raw: bytes) -> object:
    """Decode body for trace inclusion. JSON preserved; otherwise truncated string."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw[:_BODY_LIMIT].decode(errors="replace")


@router.api_route(
    "/_llm/{provider}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
)
async def llm_proxy(provider: str, path: str, request: Request) -> Response:
    upstream = _LLM_UPSTREAMS.get(provider)
    if upstream is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown LLM provider {provider!r}; known: {sorted(_LLM_UPSTREAMS)}",
        )
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length"}
    }

    _trace.emit("llm_request", {
        "provider": provider,
        "method": request.method,
        "path": "/" + path,
        "body": _trace_body(body),
    })

    upstream_req = _client.build_request(
        request.method, f"{upstream}/{path}",
        headers=headers, content=body, params=request.query_params,
    )
    upstream_resp = await _client.send(upstream_req, stream=True)
    out_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in {"transfer-encoding", "content-encoding", "content-length"}
    }

    async def _stream_and_trace():
        collected = bytearray()
        try:
            async for chunk in upstream_resp.aiter_raw():
                if len(collected) < _BODY_LIMIT:
                    collected.extend(chunk[:_BODY_LIMIT - len(collected)])
                yield chunk
        finally:
            await upstream_resp.aclose()
            _trace.emit("llm_response", {
                "provider": provider,
                "status": upstream_resp.status_code,
                "body": _trace_body(bytes(collected)),
            })

    return StreamingResponse(
        _stream_and_trace(),
        status_code=upstream_resp.status_code,
        headers=out_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )

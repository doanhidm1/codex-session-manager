"""
FastAPI Reverse Proxy Server for DeepSeek Responses API.
Handles request interception, streaming forwarding, and turn reconciliation.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .adapter import adapt_responses_body, get_active_session_mappings
from .config import (
    DEEPSEEK_UPSTREAM,
    PROXY_HOST,
    PROXY_PORT,
    UPSTREAM_CONNECT_TIMEOUT_SEC,
    UPSTREAM_TIMEOUT_SEC,
)
from .reconciler import reconcile_automations, reconcile_delegation_turns

logger = logging.getLogger("deepseek_proxy.server")

upstream_client: Optional[httpx.AsyncClient] = None


async def _background_reconciler_loop():
    """Periodically reconciles cross-session delegation turns and automations in background."""
    while True:
        try:
            await asyncio.sleep(2.0)
            reconcile_delegation_turns()
            reconcile_automations()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug("Background reconciler error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manages the shared HTTPX async client connection pool and background reconciler."""
    global upstream_client
    upstream_client = httpx.AsyncClient(
        base_url=DEEPSEEK_UPSTREAM,
        timeout=httpx.Timeout(UPSTREAM_TIMEOUT_SEC, connect=UPSTREAM_CONNECT_TIMEOUT_SEC),
        follow_redirects=True,
    )
    logger.info("DeepSeek Reverse Proxy upstream pool ready -> %s", DEEPSEEK_UPSTREAM)
    reconciler_task = asyncio.create_task(_background_reconciler_loop())
    try:
        yield
    finally:
        reconciler_task.cancel()
        try:
            await reconciler_task
        except asyncio.CancelledError:
            pass
        if upstream_client:
            await upstream_client.aclose()
        logger.info("DeepSeek Reverse Proxy upstream pool closed.")


app = FastAPI(title="Codex DeepSeek Reverse Proxy", lifespan=lifespan)


@app.get("/health")
async def health():
    """Health check endpoint for daemon status."""
    return {"status": "ok", "upstream": DEEPSEEK_UPSTREAM}


@app.post("/shutdown")
async def shutdown():
    """Administrative endpoint to gracefully terminate the daemon."""
    logger.info("Shutdown requested via /shutdown endpoint.")

    async def _do_shutdown():
        await asyncio.sleep(0.5)
        if sys.platform == "win32":
            os._exit(0)
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_do_shutdown())
    return {"status": "shutting_down"}


DELEGATION_STREAM_REPLACEMENTS = [
    # Fix deprecated dynamic namespace to MCP namespace
    (b'"namespace":"codex_app"', b'"namespace":"mcp__codex_app"'),
    (b'"namespace": "codex_app"', b'"namespace": "mcp__codex_app"'),
    # Fix prefixed tool names to standard MCP tool + namespace
    (
        b'"name":"mcp__codex_app__send_message_to_thread"',
        b'"name":"send_message_to_thread","namespace":"mcp__codex_app"',
    ),
    (
        b'"name": "mcp__codex_app__send_message_to_thread"',
        b'"name": "send_message_to_thread", "namespace": "mcp__codex_app"',
    ),
    (b'"name":"mcp__codex_app__read_thread"', b'"name":"read_thread","namespace":"mcp__codex_app"'),
    (b'"name": "mcp__codex_app__read_thread"', b'"name": "read_thread", "namespace": "mcp__codex_app"'),
    (b'"name":"mcp__codex_app__wait_threads"', b'"name":"wait_threads","namespace":"mcp__codex_app"'),
    (b'"name": "mcp__codex_app__wait_threads"', b'"name": "wait_threads", "namespace": "mcp__codex_app"'),
    (b'"name":"mcp__codex_app__automation_update"', b'"name":"automation_update","namespace":"mcp__codex_app"'),
    (b'"name": "mcp__codex_app__automation_update"', b'"name": "automation_update", "namespace": "mcp__codex_app"'),
]


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"])
async def proxy_all(request: Request, background_tasks: BackgroundTasks):
    full_path = "/" + request.path_params.get("path", "")
    req_body = await request.body()

    # Intercept and adapt POST /responses
    is_responses_api = request.method == "POST" and "responses" in full_path
    replacements = []
    if is_responses_api:
        req_body = adapt_responses_body(req_body)
        oai_to_ds, _ = get_active_session_mappings()
        replacements = [(oai.encode("utf-8"), ds.encode("utf-8")) for oai, ds in oai_to_ds.items()]
        replacements.extend(DELEGATION_STREAM_REPLACEMENTS)

    # Filter headers to forward
    excluded_headers = {"host", "content-length", "connection"}
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in excluded_headers}

    if upstream_client is None:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "Proxy upstream client not initialized.", "type": "proxy_error"}},
        )

    try:
        req = upstream_client.build_request(
            method=request.method,
            url=full_path,
            params=request.query_params,
            headers=fwd_headers,
            content=req_body,
        )

        resp = await upstream_client.send(req, stream=True)

        resp_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in {"content-length", "connection", "transfer-encoding", "content-encoding"}
        }

        async def stream_generator():
            try:
                buffer = b""
                overlap = 80
                async for chunk in resp.aiter_raw():
                    if replacements:
                        buffer += chunk
                        for oai_b, ds_b in replacements:
                            buffer = buffer.replace(oai_b, ds_b)
                        if len(buffer) > overlap:
                            to_yield = buffer[:-overlap]
                            buffer = buffer[-overlap:]
                            yield to_yield
                    else:
                        yield chunk
                if buffer:
                    if replacements:
                        for oai_b, ds_b in replacements:
                            buffer = buffer.replace(oai_b, ds_b)
                    yield buffer
            finally:
                await resp.aclose()
                # If this was a Responses API stream, trigger reconciliation in background
                if is_responses_api:
                    await asyncio.sleep(0.5)
                    reconcile_delegation_turns()
                    reconcile_automations()

        return StreamingResponse(
            stream_generator(),
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    except Exception as e:
        logger.error("Upstream request failed: %s", e)
        return Response(
            content=json.dumps(
                {"error": {"message": f"DeepSeek Proxy upstream error: {str(e)}", "type": "proxy_error"}}
            ),
            status_code=502,
            media_type="application/json",
        )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    port = PROXY_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    uvicorn.run(app, host=PROXY_HOST, port=port, log_level="info")


if __name__ == "__main__":
    main()

"""
FastAPI Reverse Proxy Server for DeepSeek Responses API.
Handles request interception, streaming forwarding, and turn reconciliation.
"""

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional, Set

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None

from .adapter import adapt_responses_body, get_active_session_mappings
from .config import (
    DEEPSEEK_UPSTREAM,
    PROXY_HOST,
    PROXY_PORT,
    UPSTREAM_CONNECT_TIMEOUT_SEC,
    UPSTREAM_TIMEOUT_SEC,
)
from . import reqlog
from .adapter import archived_thread_ids
from .reconciler import (
    find_misdirected_automation_updates,
    reconcile_automations,
    reconcile_delegation_turns,
    retarget_automation,
)

logger = logging.getLogger("deepseek_proxy.server")

upstream_client: Optional[httpx.AsyncClient] = None

_key_cache: dict = {"mtime": None, "key": None}


def _decode_request_body(body: bytes, encoding: str) -> bytes:
    """The built-in OpenAI provider (ChatGPT auth) zstd-compresses request bodies; DeepSeek and the adapter need plain JSON."""
    enc = encoding.lower().strip()
    if not body or not enc or enc == "identity":
        return body
    if enc == "zstd":
        import zstandard

        return zstandard.ZstdDecompressor().decompressobj().decompress(body)
    if enc == "gzip":
        import gzip

        return gzip.decompress(body)
    logger.warning("Unsupported request content-encoding %r, forwarding body as-is", enc)
    return body


def _deepseek_api_key() -> Optional[str]:
    """DeepSeek key from config.toml ([model_providers.deepseek]) or DEEPSEEK_API_KEY, cached by file mtime."""
    cfg = os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "config.toml")
    try:
        if tomllib is None:
            raise RuntimeError("tomllib unavailable (Python < 3.11); set DEEPSEEK_API_KEY instead")
        mtime = os.path.getmtime(cfg)
        if _key_cache["mtime"] != mtime:
            with open(cfg, "rb") as f:
                prov = tomllib.load(f).get("model_providers", {}).get("deepseek", {})
            _key_cache.update(mtime=mtime, key=prov.get("experimental_bearer_token") or prov.get("api_key"))
    except Exception as e:
        logger.debug("Could not read DeepSeek key from config.toml: %s", e)
    return _key_cache["key"] or os.environ.get("DEEPSEEK_API_KEY")


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


_MCP_FLAT_NAME_RE = re.compile(rb'"name":(\s*)"(mcp__[A-Za-z0-9_-]+?)__([A-Za-z0-9_.-]+)"')


def _mcp_namespaces(body: bytes) -> Set[str]:
    """Namespaces offered to the model in this request (MCP servers appear as {"type": "namespace", "name": "mcp__x"})."""
    try:
        tools = json.loads(body.decode("utf-8")).get("tools") or []
    except Exception:
        return set()
    return {
        t["name"]
        for t in tools
        if isinstance(t, dict) and t.get("type") == "namespace" and str(t.get("name", "")).startswith("mcp__")
    }


def split_flat_mcp_tool_names(buf: bytes, namespaces: Set[str]) -> bytes:
    """
    DeepSeek sometimes calls an MCP tool by its flattened name ("mcp__linear__get_issue") instead of
    name + namespace, which Codex rejects as "unsupported call". Split it for every known namespace.
    """
    if not namespaces:
        return buf

    def fix(m: "re.Match[bytes]") -> bytes:
        flat = (m.group(2) + b"__" + m.group(3)).decode("utf-8", "replace")
        # Longest namespace wins (e.g. mcp__codex_app over mcp__codex)
        for ns in sorted(namespaces, key=len, reverse=True):
            if flat.startswith(ns + "__") and len(flat) > len(ns) + 2:
                sp = m.group(1)
                return b'"name":' + sp + json.dumps(flat[len(ns) + 2 :]).encode() + b"," + sp + b'"namespace":' + sp + json.dumps(ns).encode()
        return m.group(0)

    return _MCP_FLAT_NAME_RE.sub(fix, buf)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"])
async def proxy_all(request: Request, background_tasks: BackgroundTasks):
    full_path = "/" + request.path_params.get("path", "")
    req_body = _decode_request_body(await request.body(), request.headers.get("content-encoding", ""))

    # Intercept and adapt POST /responses
    is_responses_api = request.method == "POST" and "responses" in full_path
    replacements = []
    mcp_namespaces: Set[str] = set()
    if is_responses_api:
        req_body = adapt_responses_body(req_body)
        mcp_namespaces = _mcp_namespaces(req_body)
        oai_to_ds, _ = get_active_session_mappings()
        replacements = [(oai.encode("utf-8"), ds.encode("utf-8")) for oai, ds in oai_to_ds.items()]
        replacements.extend(DELEGATION_STREAM_REPLACEMENTS)

    # Filter headers to forward
    # accept-encoding is dropped: the body is re-streamed raw while content-encoding is stripped, so a compressed upstream reply would reach the client undecodable
    # content-encoding is dropped because the body was decoded above
    excluded_headers = {"host", "content-length", "connection", "accept-encoding", "content-encoding"}
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in excluded_headers}
    # Clients forced onto the built-in OpenAI provider (e.g. the mobile app) send a ChatGPT token; DeepSeek needs its own key
    ds_key = _deepseek_api_key()
    if ds_key:
        for h in [k for k in fwd_headers if k.lower() in ("authorization", "chatgpt-account-id")]:
            del fwd_headers[h]
        fwd_headers["authorization"] = f"Bearer {ds_key}"

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

        own_thread_id = ""
        if is_responses_api:
            try:
                own_thread_id = str(json.loads(req_body.decode("utf-8")).get("prompt_cache_key") or "")
            except Exception:
                own_thread_id = ""
        log_rec = reqlog.describe_request(req_body, dict(request.headers)) if is_responses_api and reqlog.enabled() else None
        started = time.time()
        resp = await upstream_client.send(req, stream=True)

        resp_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in {"content-length", "connection", "transfer-encoding", "content-encoding"}
        }

        async def stream_generator():
            tail = b""
            try:
                buffer = b""
                # Must exceed the longest pattern so a match split across chunks is completed before it is emitted
                overlap = 512
                async for chunk in resp.aiter_bytes():
                    if log_rec is not None or own_thread_id:
                        tail = (tail + chunk)[-262144:]
                    if replacements:
                        buffer += chunk
                        for oai_b, ds_b in replacements:
                            buffer = buffer.replace(oai_b, ds_b)
                        buffer = split_flat_mcp_tool_names(buffer, mcp_namespaces)
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
                        buffer = split_flat_mcp_tool_names(buffer, mcp_namespaces)
                    yield buffer
            finally:
                await resp.aclose()
                reqlog.finish(log_rec, resp.status_code, tail, started)
                if own_thread_id and b"automation_update" in tail:
                    background_tasks_fix = find_misdirected_automation_updates(tail, archived_thread_ids())
                    if background_tasks_fix:
                        # Let Codex apply the tool call first, then point the automation back at the calling thread
                        await asyncio.sleep(3.0)
                        for aid, bad in background_tasks_fix.items():
                            if retarget_automation(aid, own_thread_id):
                                logger.warning(
                                    "Automation %s targeted archived thread %s; retargeted to calling thread %s",
                                    aid, bad, own_thread_id,
                                )
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

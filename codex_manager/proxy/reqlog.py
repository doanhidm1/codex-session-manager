"""
Per-request diagnostics for the DeepSeek proxy (JSON lines, size-rotated).

Each /responses request is logged with:
- where its input first differs from the previous request of the same conversation (prefix-cache breaks),
- byte sizes of the tool definitions per namespace (e.g. how much chrome-devtools adds to every request),
- byte sizes of tool outputs in the history per tool,
- usage reported by DeepSeek (input / cached / output tokens) once the response finishes.

Disable with DEEPSEEK_PROXY_LOG=0. Default file: ~/.codex/log/deepseek-proxy.jsonl
"""

import hashlib
import json
import os
import time
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional

MAX_BYTES = 20 * 1024 * 1024
_MAX_SESSIONS = 64
_prev_items: "OrderedDict[str, List[str]]" = OrderedDict()
_last_seen: Dict[str, float] = {}


def enabled() -> bool:
    return os.environ.get("DEEPSEEK_PROXY_LOG", "1") not in ("0", "false", "no")


def log_path() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.environ.get("DEEPSEEK_PROXY_LOG_FILE") or os.path.join(home, "log", "deepseek-proxy.jsonl")


def _write(rec: Dict[str, Any]) -> None:
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _item_label(it: Any) -> str:
    if not isinstance(it, dict):
        return "?"
    t = it.get("type", "?")
    if t == "message":
        return f"message:{it.get('role')}"
    return str(t)


def describe_request(body: bytes, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Build the request part of a log record (called with the adapted body actually sent upstream)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    items = data.get("input") if isinstance(data.get("input"), list) else []
    key = str(data.get("prompt_cache_key") or headers.get("session_id") or headers.get("conversation_id") or "?")

    encoded = [json.dumps(it, ensure_ascii=False, sort_keys=True) for it in items]
    hashes = [hashlib.sha1(e.encode("utf-8")).hexdigest()[:12] for e in encoded]
    prev = _prev_items.get(key)
    diverge = None
    if prev is not None:
        n = 0
        for a, b in zip(prev, hashes):
            if a != b:
                break
            n += 1
        if n < len(prev):
            diverge = {
                "item_index": n,
                "items_before": len(prev),
                "bytes_before_divergence": sum(len(e) for e in encoded[:n]),
                "changed_item": _item_label(items[n]) if n < len(items) else "removed",
            }
    _prev_items[key] = hashes
    _prev_items.move_to_end(key)
    while len(_prev_items) > _MAX_SESSIONS:
        _prev_items.popitem(last=False)
    now = time.time()
    idle = round(now - _last_seen[key], 1) if key in _last_seen else None
    _last_seen[key] = now

    tools_bytes: Counter = Counter()
    for t in data.get("tools") or []:
        if isinstance(t, dict):
            name = t.get("name") if t.get("type") == "namespace" else (t.get("type") if t.get("type") != "function" else "functions")
            tools_bytes[str(name)] += len(json.dumps(t, ensure_ascii=False))

    call_names: Dict[str, str] = {}
    out_bytes: Counter = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("type") in ("function_call", "custom_tool_call"):
            call_names[str(it.get("call_id"))] = f"{it.get('namespace') or ''}:{it.get('name')}".lstrip(":")
        elif it.get("type") in ("function_call_output", "custom_tool_call_output"):
            out = it.get("output")
            out_bytes[call_names.get(str(it.get("call_id")), "?")] += len(
                out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
            )

    reasoning = data.get("reasoning") if isinstance(data.get("reasoning"), dict) else {}
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session": key[:36],
        "model": data.get("model"),
        "effort": reasoning.get("effort"),
        "stream": data.get("stream"),
        "body_bytes": len(body),
        "items": len(items),
        "idle_sec": idle,
        "diverge": diverge,
        "tools_bytes": dict(tools_bytes.most_common()),
        "tool_output_bytes": dict(out_bytes.most_common(12)),
        "extra_keys": sorted(k for k in data if k not in ("model", "input", "tools", "stream", "reasoning", "instructions")),
    }


def extract_usage(tail: bytes) -> Optional[Dict[str, Any]]:
    """Find the last "usage" object in a (SSE or JSON) response tail."""
    text = tail.decode("utf-8", "replace")
    pos = text.rfind('"usage"')
    if pos < 0:
        return None
    start = text.find("{", pos)
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except Exception:
                    return None
    return None


def finish(rec: Optional[Dict[str, Any]], status: int, tail: bytes, started: float) -> None:
    if rec is None or not enabled():
        return
    rec["status"] = status
    rec["latency_ms"] = int((time.time() - started) * 1000)
    usage = extract_usage(tail) or {}
    details = usage.get("input_tokens_details") or {}
    rec["usage"] = {
        "input": usage.get("input_tokens"),
        "cached": details.get("cached_tokens"),
        "output": usage.get("output_tokens"),
        "reasoning": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
    }
    _write(rec)

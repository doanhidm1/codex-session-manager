"""
Protocol adaptation for DeepSeek Responses API.
Converts Codex Desktop synthetic function_call_output items
into compliant user messages, neutralizes legacy code-mode exec tool calls,
and injects direct function calling directives for DeepSeek.
"""

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("deepseek_proxy.adapter")

TOOL_DIRECTIVE = (
    "[CRITICAL SYSTEM DIRECTIVE: TOOL EXECUTION CONSTRAINTS]\n"
    "You are operating in standard function-calling mode with direct tool execution.\n"
    "The JavaScript code interpreter `exec` tool is disabled and unavailable. Any call to `exec` will fail with 'unsupported call: exec'.\n"
    "DO NOT call `exec` or write JavaScript code with `tools.exec_command` or `Promise.all`.\n"
    "You MUST call native direct tools:\n"
    '- Shell Commands: Call `exec_command` with argument `{"cmd": "<command>"}` directly.\n'
    '- Read Other Sessions: Call `read_thread` with argument `{"threadId": "<thread_id>", "turnLimit": 3}` (or `mcp__codex_app__read_thread`).\n'
    '- Message Other Sessions: Call `send_message_to_thread` with argument `{"threadId": "<thread_id>", "content": "<message>"}` (or `mcp__codex_app__send_message_to_thread`).\n'
    "- Other MCP tools: Call them directly by their registered tool name.\n"
    "Any mentions of `exec` or `legacy_exec` in previous conversation turns are from a deprecated environment; do not imitate them."
)

USER_TOOL_REMINDER = "\n\n[System directive: Execute tasks using direct tools (`exec_command`, `read_thread`, `send_message_to_thread`). Always use active (ds) thread IDs. Do NOT call `exec`.]"


def get_mapping_db_path(custom_path: Optional[str] = None) -> str:
    """Resolves the path to session_manager.sqlite."""
    if custom_path:
        return custom_path
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    user_prof = os.environ.get("USERPROFILE")
    default_db = os.path.join(codex_home, "session_manager.sqlite")
    if not os.path.exists(default_db) and user_prof:
        cand = os.path.join(user_prof, ".codex", "session_manager.sqlite")
        if os.path.exists(cand):
            return cand
    return default_db


def get_active_session_mappings(
    mapping_db_path: Optional[str] = None,
) -> Tuple[Dict[str, str], List[Tuple[str, str, str]]]:
    """
    Retrieves active (openai_thread_id -> deepseek_thread_id) mappings and pair info from SQLite.
    Returns:
        (oai_to_ds_dict, [(name, openai_id, deepseek_id), ...])
    """
    db_path = get_mapping_db_path(mapping_db_path)
    if not os.path.exists(db_path):
        return {}, []

    try:
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT name, openai_thread_id, deepseek_thread_id FROM session_pairs WHERE is_active = 1 ORDER BY name ASC"
            ).fetchall()
            oai_to_ds = {r[1]: r[2] for r in rows if r[1] and r[2]}
            pairs_info = [(r[0], r[1], r[2]) for r in rows if r[1] and r[2]]
            return oai_to_ds, pairs_info
    except Exception as e:
        logger.debug("Failed to read session mappings from %s: %s", db_path, e)
        return {}, []


def adapt_responses_body(body_bytes: bytes, mapping_db_path: Optional[str] = None) -> bytes:
    """
    Inspects and adapts the JSON body for DeepSeek Responses API.
    1. Neutralizes historical 'exec' tool calls into 'legacy_exec'.
    2. Converts synthetic function_call_output items without preceding
       function_call or lacking call_id into compliant user messages.
    3. Remaps historical OpenAI thread IDs to active DeepSeek clone IDs.
    4. Injects tool execution directives and active DeepSeek thread registry
       to ensure DeepSeek uses direct tools and targets the right sessions.
    5. Filters out any disabled 'exec' tool definition from tools list.
    """
    try:
        data = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return body_bytes

    if not isinstance(data, dict):
        return body_bytes

    adapted = False

    # 1. Filter out disabled 'exec' tool if present in tools list
    tools = data.get("tools")
    if isinstance(tools, list):
        filtered_tools = []
        for t in tools:
            if isinstance(t, dict):
                name = t.get("name") or t.get("function", {}).get("name")
                if name == "exec":
                    adapted = True
                    continue
            filtered_tools.append(t)
        if adapted:
            data["tools"] = filtered_tools

    oai_to_ds, pairs_info = get_active_session_mappings(mapping_db_path)

    input_items = data.get("input")
    if not isinstance(input_items, list):
        if adapted:
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
        return body_bytes

    # Remap historical OpenAI thread IDs to active DeepSeek clone IDs
    if oai_to_ds:
        input_json = json.dumps(input_items, ensure_ascii=False)
        id_remapped = False
        for oai_id, ds_id in oai_to_ds.items():
            if oai_id in input_json:
                input_json = input_json.replace(oai_id, ds_id)
                id_remapped = True
        if id_remapped:
            input_items = json.loads(input_json)
            adapted = True
            logger.info("Transparently remapped historical OpenAI thread ID(s) to active DeepSeek ID(s) in input.")

    # 2. Collect all valid tool call IDs produced by preceding function_calls
    known_call_ids: Set[str] = set()
    for item in input_items:
        if isinstance(item, dict) and item.get("type") == "function_call":
            cid = item.get("call_id") or item.get("id")
            if cid:
                known_call_ids.add(str(cid))

    # 3. Inspect items, neutralize legacy exec calls, and adapt synthetic outputs
    new_input: List[Dict[str, Any]] = []
    for item in input_items:
        if not isinstance(item, dict):
            new_input.append(item)
            continue

        itype = item.get("type")

        # Neutralize historical 'exec' calls from OpenAI era
        if itype in ("function_call", "custom_tool_call") and item.get("name") == "exec":
            item["name"] = "legacy_exec"
            adapted = True

        if itype in ("function_call_output", "custom_tool_call_output") and item.get("name") == "exec":
            item["name"] = "legacy_exec"
            adapted = True

        if itype == "function_call_output":
            cid = item.get("call_id")
            # If missing call_id or call_id is not in known_call_ids, convert to user message
            if not cid or str(cid) not in known_call_ids:
                out_content = item.get("output", "")
                if isinstance(out_content, (dict, list)):
                    out_content = json.dumps(out_content, ensure_ascii=False)
                else:
                    out_content = str(out_content)

                user_msg: Dict[str, Any] = {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": out_content,
                        }
                    ],
                }
                new_input.append(user_msg)
                adapted = True
                name = item.get("name") or item.get("id") or "unnamed"
                logger.info("Adapted synthetic function_call_output (%s) to user message.", name)
                continue

        new_input.append(item)

    # 4. Construct effective directive with active DeepSeek thread registry
    effective_directive = TOOL_DIRECTIVE
    if pairs_info:
        registry_lines = [
            "\n\n[ACTIVE DEEPSEEK THREAD REGISTRY]",
            "You are operating in an active DeepSeek-mode workspace.",
            "When calling `automation_update`, `send_message_to_thread`, or `read_thread`, you MUST ONLY use the active DeepSeek thread IDs:",
        ]
        for name, oai_id, ds_id in pairs_info:
            registry_lines.append(f"- {name} (ds): {ds_id}")
        registry_lines.extend(
            [
                "When creating heartbeat automations for yourself, set `targetThreadId` to your own active DeepSeek session ID (NOT the legacy OpenAI original ID).",
                "NEVER target or message the original OpenAI threads.",
            ]
        )
        effective_directive += "\n".join(registry_lines)

    # Inject tool directive into developer message (or prepend if absent)
    dev_injected = False
    for item in new_input:
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "developer":
            content = item.get("content")
            if isinstance(content, list):
                content.append({"type": "input_text", "text": effective_directive})
                dev_injected = True
                adapted = True
                break

    if not dev_injected:
        new_input.insert(
            0,
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": effective_directive}],
            },
        )
        adapted = True

    # 5. Append reminder to the last user message
    for item in reversed(new_input):
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, list) and len(content) > 0:
                last_block = content[-1]
                if isinstance(last_block, dict) and "text" in last_block:
                    last_block["text"] += USER_TOOL_REMINDER
                    adapted = True
                    break

    if adapted:
        data["input"] = new_input
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    return body_bytes

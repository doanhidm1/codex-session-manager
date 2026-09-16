"""
Protocol adaptation for DeepSeek Responses API.
Converts Codex Desktop synthetic function_call_output items
into compliant user messages, neutralizes legacy code-mode exec tool calls,
and injects direct function calling directives for DeepSeek.
"""

import json
import logging
from typing import Any, Dict, List, Set

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

USER_TOOL_REMINDER = "\n\n[System directive: Execute tasks using direct tools (`exec_command`, `read_thread`, `send_message_to_thread`). Do NOT call `exec`.]"


def adapt_responses_body(body_bytes: bytes) -> bytes:
    """
    Inspects and adapts the JSON body for DeepSeek Responses API.
    1. Neutralizes historical 'exec' tool calls into 'legacy_exec'.
    2. Converts synthetic function_call_output items without preceding
       function_call or lacking call_id into compliant user messages.
    3. Injects tool execution directives to ensure DeepSeek uses direct
       tools (exec_command, read_thread, send_message_to_thread).
    4. Filters out any disabled 'exec' tool definition from tools list.
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

    input_items = data.get("input")
    if not isinstance(input_items, list):
        if adapted:
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
        return body_bytes

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

    # 4. Inject tool directive into developer message (or prepend if absent)
    dev_injected = False
    for item in new_input:
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "developer":
            content = item.get("content")
            if isinstance(content, list):
                content.append({"type": "input_text", "text": TOOL_DIRECTIVE})
                dev_injected = True
                adapted = True
                break

    if not dev_injected:
        new_input.insert(
            0,
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": TOOL_DIRECTIVE}],
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

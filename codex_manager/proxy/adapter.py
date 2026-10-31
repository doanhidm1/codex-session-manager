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
    '- Read Other Sessions: Call `read_thread` with namespace `mcp__codex_app` and argument `{"threadId": "<thread_id>", "turnLimit": 3}`.\n'
    '- Message Other Sessions: Call `send_message_to_thread` with namespace `mcp__codex_app` and argument `{"threadId": "<thread_id>", "prompt": "<message>"}`.\n'
    '- Wait Other Sessions: Call `wait_threads` with namespace `mcp__codex_app` and argument `{"targets": [{"threadId": "<thread_id>"}], "timeoutMs": 45000}`.\n'
    "- Heartbeat Automations: Call `automation_update` with namespace `mcp__codex_app`.\n"
    "- Other MCP tools: Call them directly with their proper namespace.\n"
    "CRITICAL CONSTRAINTS FOR INTER-SESSION DELEGATION TOOLS:\n"
    "- Always use namespace `mcp__codex_app` for `send_message_to_thread`, `read_thread`, `wait_threads`, and `automation_update`.\n"
    "- NEVER use the deprecated dynamic `codex_app` namespace.\n"
    "- In `send_message_to_thread`, the message parameter name is `prompt` (NOT `content`).\n"
    "- Do NOT prefix the tool name as `mcp__codex_app__send_message_to_thread`; use name `send_message_to_thread` with namespace `mcp__codex_app`.\n"
    "Any mentions of `exec` or `legacy_exec` in previous conversation turns are from a deprecated environment; do not imitate them.\n"
    "TOKEN EFFICIENCY & COMMAND BATCHING GUIDELINES:\n"
    "- BATCH COMMANDS: Combine related shell inspection or file check commands into a single compound command (e.g. `cmd1; cmd2` or small script) instead of issuing single commands across dozens of separate turns.\n"
    "- AVOID TIGHT POLLING: Do not query thread status or poll files in rapid repetitive loops. Keep status updates concise and focused.\n"
    "CI/CD & LONG-RUNNING ASYNCHRONOUS TASK MONITORING (CRITICAL):\n"
    "- NEVER WAIT FOR CI IN A SHELL LOOP: Absolutely do NOT run long-running shell loops (`while true; sleep ...`, repeated curl/glab polling) in cmd/terminal to wait for CI/CD or build jobs. Long-running blocking commands flood context with logs, waste token budget, and trigger fatal SSE idle timeouts (`stream disconnected before completion: idle timeout waiting for SSE`).\n"
    "- SCHEDULE INSTEAD OF BLOCKING: Inspect CI status ONCE via a quick command. If CI is still in progress (`running`/`pending`), keep or create this thread's heartbeat per the rules below, report current status, and END THE TURN immediately.\n"
    "HEARTBEAT / AUTOMATION RULES (CRITICAL):\n"
    "- At most ONE heartbeat per thread. If one exists, update it with `automation_update`; never create another.\n"
    "- Use heartbeats only to wait for external events (CI, deploy, review). Do work that can be done now in this turn instead of scheduling a heartbeat to continue it.\n"
    "- Waiting on another thread: ask it via `send_message_to_thread` to notify you when done, then PAUSE your heartbeat. Do not poll other threads.\n"
    "- Interval matches the real wait: CI/pipeline 20-30 minutes, deploy/review 30-60 minutes, never below 15 minutes.\n"
    "- Backoff: after 2 consecutive checks with no change, double the interval (max 60 minutes).\n"
    "- On a terminal state (pass/fail handled, or blocked on a human), PAUSE or delete the heartbeat in that same turn.\n"
    "- Keep the heartbeat prompt under 500 characters: only what to check next and the stop condition; detailed state stays in the thread."
)


USER_TOOL_REMINDER = "\n\n[System directive: Execute tasks using direct tools (`exec_command`, `read_thread`, `send_message_to_thread` with namespace `mcp__codex_app` and parameter `prompt`). Never run shell loops to wait for CI; check once, schedule automation, and end the turn. Always use active (ds) thread IDs. Do NOT call `exec`.]"


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


def archived_thread_ids(state_db: Optional[str] = None) -> Set[str]:
    """IDs of archived threads in state_5.sqlite (empty set if unavailable)."""
    path = state_db or os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "state_5.sqlite")
    if not os.path.exists(path):
        return set()
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as conn:
            return {r[0] for r in conn.execute("SELECT id FROM threads WHERE archived = 1")}
    except Exception as e:
        logger.debug("Failed to read archived threads from %s: %s", path, e)
        return set()


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


_CALL_TYPES = ("function_call", "custom_tool_call")
_OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output")


def _is_call(it: Any) -> bool:
    # Any paired call item: function_call, custom_tool_call, tool_search_call, ... (carries a call_id)
    t = it.get("type") if isinstance(it, dict) else None
    return bool(t) and t.endswith("_call") and bool(it.get("call_id"))


def _is_output(it: Any) -> bool:
    # Matching outputs: function_call_output, custom_tool_call_output, tool_search_output, ...
    t = it.get("type") if isinstance(it, dict) else None
    return bool(t) and t.endswith("_output") and bool(it.get("call_id"))


def keep_tool_outputs_adjacent(items: List[Any]) -> List[Any]:
    """
    Defer any non-tool item that appears while tool calls are still waiting for their outputs, so every
    call is directly followed by its output. Returns the same list object when nothing had to move.
    """
    answered = {
        str(it.get("call_id"))
        for it in items
        if _is_output(it)
    }
    out: List[Any] = []
    deferred: List[Any] = []
    pending: Set[str] = set()
    for it in items:
        if _is_call(it):
            cid = str(it.get("call_id") or "")
            if cid in answered:
                pending.add(cid)
            out.append(it)
        elif _is_output(it):
            out.append(it)
            pending.discard(str(it.get("call_id") or ""))
            if not pending and deferred:
                out.extend(deferred)
                deferred = []
        elif pending:
            deferred.append(it)
        else:
            out.append(it)
    out.extend(deferred)
    return items if out == items else out


TOOL_IMAGE_NOTE = "[image returned by this tool call; it is attached in the next user message]"


def move_tool_images_to_user_messages(items: List[Any]) -> List[Any]:
    """
    DeepSeek accepts images only in user messages (images elsewhere are dropped or rejected), but Codex returns
    screenshots / view_image results inside tool outputs. Replace each such image with a short note and attach
    the images in a user message placed right after that output. Deterministic, so the prompt prefix stays
    cache-stable. Returns the same list object when there is nothing to move.
    """
    out: List[Any] = []
    moved = False
    for it in items:
        if isinstance(it, dict) and it.get("type") in _OUTPUT_TYPES and isinstance(it.get("output"), list):
            images = [p for p in it["output"] if isinstance(p, dict) and p.get("type") == "input_image"]
            if images:
                it["output"] = [
                    {"type": "input_text", "text": TOOL_IMAGE_NOTE} if (isinstance(p, dict) and p.get("type") == "input_image") else p
                    for p in it["output"]
                ]
                out.append(it)
                out.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"Image(s) from tool call {it.get('call_id', '')}:"}]
                        + images,
                    }
                )
                moved = True
                continue
        out.append(it)
    return out if moved else items


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

        # Remap deprecated dynamic 'codex_app' namespace to 'mcp__codex_app' in input history
        if itype in ("function_call", "custom_tool_call"):
            if item.get("namespace") == "codex_app":
                item["namespace"] = "mcp__codex_app"
                adapted = True
            fname = str(item.get("name", ""))
            if "::" in fname:
                item["name"] = fname.replace("::", "__")
                fname = item["name"]
                adapted = True
            if fname.startswith("mcp__codex_app__"):
                item["name"] = fname[len("mcp__codex_app__") :]
                item["namespace"] = "mcp__codex_app"
                adapted = True

        if itype in ("function_call_output", "custom_tool_call_output"):
            if item.get("namespace") == "codex_app":
                item["namespace"] = "mcp__codex_app"
                adapted = True
            fname = str(item.get("name", ""))
            if "::" in fname:
                item["name"] = fname.replace("::", "__")
                fname = item["name"]
                adapted = True
            if fname.startswith("mcp__codex_app__"):
                item["name"] = fname[len("mcp__codex_app__") :]
                item["namespace"] = "mcp__codex_app"
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

    # 3.4 Images: keep them in user messages, move tool-output images into user messages (DeepSeek vision rule)
    with_images = move_tool_images_to_user_messages(new_input)
    if with_images is not new_input:
        new_input = with_images
        adapted = True

    # 3.5 DeepSeek requires each tool output right after its call; hooks (e.g. a PostToolUse developer
    # message) can land in between, which DeepSeek rejects with "No tool output found for tool call ..."
    reordered = keep_tool_outputs_adjacent(new_input)
    if reordered is not new_input:
        new_input = reordered
        adapted = True
        logger.info("Moved item(s) that sat between a tool call and its output after the output.")

    # 4. Construct effective directive with active DeepSeek thread registry
    effective_directive = TOOL_DIRECTIVE
    # The model cannot know its own thread ID; without it, "target yourself" made it pick another thread from the
    # registry (seven heartbeats ended up on one archived thread). Codex sends the thread ID as prompt_cache_key.
    own_id = str(data.get("prompt_cache_key") or "")
    archived = archived_thread_ids()
    live_pairs = [(name, oai_id, ds_id) for name, oai_id, ds_id in pairs_info if ds_id not in archived]
    if live_pairs or own_id:
        registry_lines = ["\n\n[ACTIVE DEEPSEEK THREAD REGISTRY]", "You are operating in an active DeepSeek-mode workspace."]
        if own_id:
            own_name = next((f"{n} (ds)" for n, _, d in live_pairs if d == own_id), "this conversation")
            registry_lines.append(
                f"YOUR OWN THREAD ID is {own_id} ({own_name}). Heartbeat automations about your own work MUST use "
                f"`targetThreadId` = {own_id}. Never target another thread unless the user explicitly asks."
            )
        if live_pairs:
            registry_lines.append(
                "Other active (non-archived) DeepSeek threads you may read or message when the task requires it:"
            )
            for name, oai_id, ds_id in live_pairs:
                if ds_id != own_id:
                    registry_lines.append(f"- {name} (ds): {ds_id}")
        registry_lines.append("NEVER target or message the original OpenAI threads or archived threads.")
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

    # 5. Append the reminder to every user message. Adding it only to the latest one moved it on each new user
    # message, changing an earlier part of the prompt and invalidating DeepSeek's prefix cache from that point on.
    for item in new_input:
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, list) and len(content) > 0:
                last_block = content[-1]
                if isinstance(last_block, dict) and "text" in last_block and not str(last_block["text"]).endswith(
                    USER_TOOL_REMINDER
                ):
                    last_block["text"] += USER_TOOL_REMINDER
                    adapted = True

    # 6. Reasoning effort is passed through unchanged: DeepSeek renders it into the prompt, so changing it
    # between requests of one thread splits the prefix cache (measured: a switch re-bills nearly the whole context).

    if adapted:
        data["input"] = new_input
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    return body_bytes

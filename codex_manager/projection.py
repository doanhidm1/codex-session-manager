import json
import os
import sqlite3
import time

from .config import normalize_path


def normalize_command_execution(raw_item: dict) -> dict:
    """
    Ensures commandExecution dictionary conforms strictly to Codex Rust Serde schema:
    - command: string
    - processId: string
    - aggregatedOutput: string
    - exitCode: integer
    - durationMs: integer
    - commandActions: list of dicts
    - source: one of ('agent', 'userShell', 'unifiedExecStartup', 'unifiedExecInteraction')
    - cwd: normalized local path without 'file:///' prefix
    - strips non-schema keys (stdout, stderr, formatted_output, etc.)
    """
    item = dict(raw_item)
    raw_cmd = item.get("command")
    if isinstance(raw_cmd, list):
        import subprocess

        cmd_str = subprocess.list2cmdline(raw_cmd)
    elif isinstance(raw_cmd, str):
        cmd_str = raw_cmd
    else:
        cmd_str = str(raw_cmd or "")

    dur_raw = item.get("duration") or item.get("durationMs")
    dur_ms = None
    if isinstance(dur_raw, dict):
        dur_ms = int(dur_raw.get("secs", 0) * 1000 + dur_raw.get("nanos", 0) // 1_000_000)
    elif isinstance(dur_raw, (int, float)):
        dur_ms = int(dur_raw)

    p_cmd = item.get("parsed_cmd") or item.get("commandActions") or []
    actions = []
    if isinstance(p_cmd, list):
        for act in p_cmd:
            if isinstance(act, dict):
                act_cmd = act.get("command") or act.get("cmd") or ""
                actions.append({"type": "unknown", "command": act_cmd})

    cwd = item.get("cwd") or ""
    if isinstance(cwd, str) and cwd.startswith("file:///"):
        cwd = cwd[8:]
    cwd = os.path.normpath(cwd) if cwd else None

    src_raw = item.get("source") or "unifiedExecStartup"
    if src_raw == "unified_exec_startup":
        src_raw = "unifiedExecStartup"
    elif src_raw == "unified_exec_interaction":
        src_raw = "unifiedExecInteraction"
    elif src_raw == "user_shell":
        src_raw = "userShell"
    elif src_raw not in ("agent", "userShell", "unifiedExecStartup", "unifiedExecInteraction"):
        src_raw = "unifiedExecStartup"

    agg_out = item.get("aggregated_output") or item.get("aggregatedOutput") or item.get("stdout") or ""

    exit_code = item.get("exit_code") if "exit_code" in item else item.get("exitCode", 0)

    clean_item = {
        "type": "commandExecution",
        "id": item.get("id"),
        "pluginId": item.get("pluginId", None),
        "scriptPath": item.get("scriptPath", None),
        "command": cmd_str,
        "cwd": cwd,
        "processId": str(item.get("process_id") or item.get("processId") or ""),
        "source": src_raw,
        "status": item.get("status", "completed"),
        "commandActions": actions if actions else [{"type": "unknown", "command": cmd_str}],
        "aggregatedOutput": agg_out,
        "exitCode": exit_code,
        "durationMs": dur_ms,
    }
    return clean_item


def build_thread_projection(rollout_path, thread_id, th_db):
    """
    Scan a JSONL rollout file and populate the projection cache in thread_history_1.sqlite:
    - thread_turns
    - thread_items
    - thread_history_projection_state
    """
    if not os.path.exists(normalize_path(th_db)):
        return False
    if not os.path.exists(normalize_path(rollout_path)):
        return False

    now_ts = int(time.time())
    now_ms = int(time.time() * 1000)
    file_size = os.path.getsize(rollout_path)

    p_turns = {}
    p_turn_order = []
    p_items = []
    p_curr_tid = None
    p_max_ord = 0

    with open(rollout_path, "rb") as froll:
        p_offset = 0
        while True:
            l_start = p_offset
            raw_line = froll.readline()
            if not raw_line:
                break
            l_end = p_offset + len(raw_line)
            p_offset = l_end

            try:
                entry = json.loads(raw_line.decode("utf-8"))
            except Exception:
                continue

            ord_val = entry.get("ordinal", 0)
            if ord_val > p_max_ord:
                p_max_ord = ord_val

            e_type = entry.get("type")
            e_payload = entry.get("payload", {})
            p_type = e_payload.get("type")

            if e_type == "event_msg":
                if p_type == "task_started":
                    tid = e_payload.get("turn_id")
                    if tid:
                        p_curr_tid = tid
                        s_at = e_payload.get("started_at") or now_ts
                        if tid not in p_turns:
                            p_turn_order.append(tid)
                            p_turns[tid] = {
                                "thread_id": thread_id,
                                "turn_id": tid,
                                "rollout_ordinal": ord_val,
                                "status": "completed",
                                "error_json": None,
                                "started_at": s_at,
                                "completed_at": s_at,
                                "duration_ms": None,
                                "first_user_item_id": None,
                                "final_agent_item_id": None,
                                "rollout_byte_offset": l_start,
                                "rollout_end_ordinal": ord_val,
                                "rollout_end_byte_offset": l_end,
                            }
                elif p_type == "item_completed":
                    tid = e_payload.get("turn_id") or p_curr_tid
                    raw_item = e_payload.get("item", {})
                    itype_raw = raw_item.get("type", "")
                    iid = raw_item.get("id", "")

                    itype = "unknown"
                    if itype_raw in ("UserMessage", "userMessage"):
                        itype = "userMessage"
                        raw_content = raw_item.get("content", [])
                        clean_content = []
                        if isinstance(raw_content, list):
                            for part in raw_content:
                                if isinstance(part, dict):
                                    ptype = part.get("type")
                                    if ptype in ("local_image", "localImage"):
                                        clean_content.append(
                                            {
                                                "type": "localImage",
                                                "detail": part.get("detail", None),
                                                "path": part.get("path", ""),
                                            }
                                        )
                                    elif ptype in ("local_audio", "localAudio"):
                                        clean_content.append({"type": "localAudio", "path": part.get("path", "")})
                                    elif ptype in ("image", "input_image"):
                                        url_val = part.get("url") or part.get("image_url") or ""
                                        clean_content.append({"type": "image", "url": url_val})
                                    else:
                                        clean_content.append(part)
                                else:
                                    clean_content.append(part)
                        else:
                            clean_content = raw_content

                        clean_item = {
                            "type": "userMessage",
                            "id": iid,
                            "content": clean_content,
                            "clientId": raw_item.get("clientId") or raw_item.get("client_id") or None,
                        }
                        if tid and tid in p_turns and not p_turns[tid]["first_user_item_id"]:
                            p_turns[tid]["first_user_item_id"] = iid
                    elif itype_raw in ("AgentMessage", "agentMessage"):
                        itype = "agentMessage"
                        text_val = raw_item.get("text", "")
                        if not text_val and "content" in raw_item:
                            text_val = "".join(
                                c.get("text", "") for c in raw_item.get("content", []) if isinstance(c, dict)
                            )
                        clean_item = {
                            "type": "agentMessage",
                            "id": iid,
                            "text": text_val,
                            "phase": raw_item.get("phase", "final_answer"),
                        }
                        if tid and tid in p_turns:
                            p_turns[tid]["final_agent_item_id"] = iid
                    elif itype_raw in ("CommandExecution", "commandExecution"):
                        itype = "commandExecution"
                        clean_item = normalize_command_execution(raw_item)
                        clean_item["id"] = iid
                    elif itype_raw in ("FileChange", "fileChange"):
                        itype = "fileChange"
                        changes_raw = raw_item.get("changes")
                        changes_list = []
                        if isinstance(changes_raw, dict):
                            for fpath, fval in changes_raw.items():
                                if isinstance(fval, dict):
                                    changes_list.append(
                                        {
                                            "path": fpath,
                                            "kind": {
                                                "type": fval.get("type", "update"),
                                                "move_path": fval.get("move_path", None),
                                            },
                                            "diff": fval.get("unified_diff") or fval.get("diff", ""),
                                        }
                                    )
                        elif isinstance(changes_raw, list):
                            for ch in changes_raw:
                                if isinstance(ch, dict):
                                    path_val = ch.get("path", "")
                                    kind_val = ch.get("kind", {"type": "update", "move_path": None})
                                    if isinstance(kind_val, str):
                                        kind_val = {"type": kind_val, "move_path": None}
                                    diff_val = ch.get("diff") or ch.get("unified_diff", "")
                                    changes_list.append({"path": path_val, "kind": kind_val, "diff": diff_val})
                        clean_item = {
                            "type": "fileChange",
                            "id": iid,
                            "changes": changes_list,
                            "status": raw_item.get("status", "completed"),
                        }
                    elif itype_raw in ("McpToolCall", "mcpToolCall"):
                        itype = "mcpToolCall"
                        dur_raw = raw_item.get("duration") or raw_item.get("durationMs")
                        dur_ms = None
                        if isinstance(dur_raw, dict):
                            dur_ms = int(dur_raw.get("secs", 0) * 1000 + dur_raw.get("nanos", 0) // 1_000_000)
                        elif isinstance(dur_raw, (int, float)):
                            dur_ms = int(dur_raw)
                        clean_item = {
                            "type": "mcpToolCall",
                            "id": iid,
                            "server": raw_item.get("server", ""),
                            "tool": raw_item.get("tool", ""),
                            "arguments": raw_item.get("arguments", {}),
                            "appContext": raw_item.get("appContext", None),
                            "pluginId": raw_item.get("pluginId", None),
                            "readOnlyHint": raw_item.get("readOnlyHint", False),
                            "result": raw_item.get("result", {}),
                            "error": raw_item.get("error", None),
                            "status": raw_item.get("status", "completed"),
                            "durationMs": dur_ms,
                        }
                    elif itype_raw in ("Reasoning", "reasoning"):
                        itype = "reasoning"
                        summary = raw_item.get("summary") or raw_item.get("summary_text") or []
                        if isinstance(summary, str):
                            summary = [summary]
                        content = raw_item.get("content") or raw_item.get("raw_content") or []
                        clean_item = {"type": "reasoning", "id": iid, "summary": summary, "content": content}
                    else:
                        itype = itype_raw[0].lower() + itype_raw[1:] if itype_raw else "unknown"
                        clean_item = dict(raw_item)
                        clean_item["type"] = itype

                    if tid and tid in p_turns:
                        p_turns[tid]["rollout_end_ordinal"] = ord_val
                        p_turns[tid]["rollout_end_byte_offset"] = l_end

                    item_created_ms = e_payload.get("started_at_ms") or e_payload.get("completed_at_ms") or now_ms

                    p_items.append(
                        {
                            "thread_id": thread_id,
                            "turn_id": tid,
                            "item_id": iid,
                            "rollout_ordinal": ord_val,
                            "created_at_ms": item_created_ms,
                            "item_json": json.dumps(clean_item, ensure_ascii=False),
                            "item_type": itype,
                            "updated_at_ordinal": ord_val,
                        }
                    )
                elif p_type == "task_complete":
                    tid = e_payload.get("turn_id") or p_curr_tid
                    if tid and tid in p_turns:
                        p_turns[tid]["rollout_end_ordinal"] = ord_val
                        p_turns[tid]["rollout_end_byte_offset"] = l_end
                        if e_payload.get("completed_at"):
                            p_turns[tid]["completed_at"] = e_payload["completed_at"]
                        if e_payload.get("duration_ms"):
                            p_turns[tid]["duration_ms"] = e_payload["duration_ms"]
                        if e_payload.get("error"):
                            p_turns[tid]["status"] = "failed"
                            p_turns[tid]["error_json"] = json.dumps(e_payload["error"], ensure_ascii=False)
            elif e_type == "response_item":
                if p_type == "message" and e_payload.get("role") == "user":
                    iid = e_payload.get("id")
                    raw_c = e_payload.get("content", [])
                    clean_text = ""
                    for part in raw_c:
                        if isinstance(part, dict) and "text" in part:
                            clean_text += part["text"]
                        elif isinstance(part, str):
                            clean_text += part
                    clean_item = {
                        "type": "userMessage",
                        "id": iid,
                        "content": [{"type": "text", "text": clean_text}],
                        "clientId": None,
                    }
                    tid = p_curr_tid
                    if tid and tid in p_turns and not p_turns[tid]["first_user_item_id"]:
                        p_turns[tid]["first_user_item_id"] = iid
                    p_items.append(
                        {
                            "thread_id": thread_id,
                            "turn_id": tid,
                            "item_id": iid,
                            "rollout_ordinal": ord_val,
                            "created_at_ms": now_ms,
                            "item_json": json.dumps(clean_item, ensure_ascii=False),
                            "item_type": "userMessage",
                            "updated_at_ordinal": ord_val,
                        }
                    )

    th_conn = sqlite3.connect(normalize_path(th_db), timeout=10.0)
    th_cur = th_conn.cursor()

    th_cur.execute("DELETE FROM thread_history_projection_state WHERE thread_id = ?", (thread_id,))
    th_cur.execute("DELETE FROM thread_turns WHERE thread_id = ?", (thread_id,))
    th_cur.execute("DELETE FROM thread_items WHERE thread_id = ?", (thread_id,))
    try:
        th_cur.execute("DELETE FROM thread_realtime_items WHERE thread_id = ?", (thread_id,))
    except sqlite3.OperationalError:
        pass

    turn_cols = [
        "thread_id",
        "turn_id",
        "rollout_ordinal",
        "status",
        "error_json",
        "started_at",
        "completed_at",
        "duration_ms",
        "first_user_item_id",
        "final_agent_item_id",
        "rollout_byte_offset",
        "rollout_end_ordinal",
        "rollout_end_byte_offset",
    ]
    sql_turns = (
        f"INSERT OR REPLACE INTO thread_turns ({','.join(turn_cols)}) VALUES ({','.join(['?'] * len(turn_cols))})"
    )
    for tid in p_turn_order:
        t_data = p_turns[tid]
        th_cur.execute(sql_turns, [t_data[c] for c in turn_cols])

    item_cols = [
        "thread_id",
        "turn_id",
        "item_id",
        "rollout_ordinal",
        "created_at_ms",
        "item_json",
        "item_type",
        "updated_at_ordinal",
    ]
    sql_items = (
        f"INSERT OR REPLACE INTO thread_items ({','.join(item_cols)}) VALUES ({','.join(['?'] * len(item_cols))})"
    )
    for item in p_items:
        th_cur.execute(sql_items, [item[c] for c in item_cols])

    th_cur.execute(
        "INSERT INTO thread_history_projection_state (thread_id, next_rollout_byte_offset, next_rollout_ordinal) VALUES (?, ?, ?)",
        (thread_id, file_size, p_max_ord + 1),
    )

    th_conn.commit()
    th_conn.close()
    return True

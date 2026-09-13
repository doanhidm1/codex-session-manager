import datetime
import json
import os
import sqlite3
import time
import uuid

from .rollout import make_wire_record


def parse_iso_to_ms(iso_str):
    if not iso_str:
        return int(time.time() * 1000)
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return int(dt.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def stream_turn_wire_records(
    src_rollout_path,
    tid,
    curr_ord,
    curr_offset,
    tgt_id,
    tgt_cwd,
    tgt_model,
    tgt_prov,
    start_offset=None,
    orig_started_at=None,
    orig_completed_at=None,
    source_items_map=None,
    thread_map=None,
):
    """
    Stream full-fidelity conversational wire records directly from the source rollout file.
    Preserves:
      - Original timestamps (started_at, completed_at, event ISO timestamps)
      - Images (local_image, input_image)
      - Sequential steer user messages (no concatenation/flattening)
      - Cross-thread delegations (<codex_delegation>)
      - Scheduled task heartbeats (<heartbeat>)
    Adapts:
      - Ordinals (monotonic sequence in target rollout)
      - Thread IDs and session IDs
      - Target model and cwd
      - Converts unauthenticated synthetic tool outputs to user XML messages for DeepSeek compatibility.
    """
    if not os.path.exists(src_rollout_path):
        return None, None, None, curr_ord, curr_offset

    turn_records = []
    found = False

    with open(src_rollout_path, "rb") as f:
        # Fast path: seek to rollout_byte_offset if known
        if start_offset is not None and start_offset >= 0:
            f.seek(start_offset)
            # Verify we are on or near task_started for tid
            for _ in range(5):
                pos = f.tell()
                line = f.readline()
                if not line:
                    break
                try:
                    d = json.loads(line.decode("utf-8"))
                    p = d.get("payload", {})
                    if d.get("type") == "event_msg" and p.get("type") == "task_started" and p.get("turn_id") == tid:
                        turn_records.append(d)
                        found = True
                        break
                    if p.get("turn_id") == tid:
                        turn_records.append(d)
                        found = True
                        break
                except Exception:
                    pass

        # Fallback: scan if fast seek didn't land on tid
        if not found:
            f.seek(0)
            turn_records = []
            while True:
                line = f.readline()
                if not line:
                    break
                try:
                    d = json.loads(line.decode("utf-8"))
                    p = d.get("payload", {})
                    if d.get("type") == "event_msg" and p.get("type") == "task_started" and p.get("turn_id") == tid:
                        turn_records.append(d)
                        found = True
                        break
                except Exception:
                    continue

        if not found:
            return None, None, None, curr_ord, curr_offset

        # Collect subsequent records belonging to this turn until task_complete
        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                break
            try:
                d = json.loads(line.decode("utf-8"))
            except Exception:
                continue

            t = d.get("type")
            p = d.get("payload", {})

            # Check if this record belongs to the turn
            rec_tid = (
                p.get("turn_id")
                or p.get("internal_chat_message_metadata_passthrough", {}).get("turn_id")
            )
            if t == "event_msg" and p.get("type") == "task_started" and p.get("turn_id") != tid:
                # Next turn started, stop
                break

            turn_records.append(d)

            if t == "event_msg" and p.get("type") == "task_complete" and p.get("turn_id") == tid:
                # End of turn reached
                break

    if not turn_records:
        return None, None, None, curr_ord, curr_offset

    transformed_lines = []
    item_metas = []
    first_user_item_id = None
    final_agent_item_id = None
    started_at = orig_started_at or int(time.time())
    completed_at = orig_completed_at or started_at
    duration_ms = None
    error_json = None
    turn_start_ord = curr_ord
    turn_start_offset = curr_offset

    for rec in turn_records:
        rec_ord = curr_ord
        rec["ordinal"] = curr_ord
        curr_ord += 1

        iso_ts = rec.get("timestamp")
        rec_ts_ms = parse_iso_to_ms(iso_ts)

        rec_type = rec.get("type")
        p = rec.get("payload", {})

        if rec_type == "event_msg":
            ptype = p.get("type")
            if ptype == "task_started":
                started_at = p.get("started_at") or started_at
            elif ptype == "task_complete":
                completed_at = p.get("completed_at") or completed_at
                duration_ms = p.get("duration_ms")
                if p.get("error"):
                    error_json = json.dumps(p.get("error"), ensure_ascii=False)
            elif ptype == "item_completed":
                it = p.get("item", {})
                itype_raw = it.get("type", "")
                iid = it.get("id", "")

                is_converted_xml = False
                # DeepSeek API compatibility: synthetic tool call outputs must be standard user XML
                if tgt_prov != "openai" and itype_raw == "FunctionCallOutput":
                    iname = it.get("name")
                    out_str = it.get("output", "")
                    if iname in ("automation_update", "send_message_to_thread") or "<codex_delegation>" in out_str or "<heartbeat>" in out_str:
                        it["type"] = "UserMessage"
                        it["content"] = [{"type": "text", "text": out_str}]
                        it.pop("name", None)
                        it.pop("output", None)
                        it.pop("namespace", None)
                        itype_raw = "UserMessage"
                        is_converted_xml = True

                if itype_raw in ("UserMessage", "userMessage"):
                    if not first_user_item_id:
                        first_user_item_id = iid
                elif itype_raw in ("AgentMessage", "agentMessage"):
                    final_agent_item_id = iid

                clean_itype = itype_raw[0].lower() + itype_raw[1:] if itype_raw else "unknown"
                created_ms = p.get("started_at_ms") or p.get("completed_at_ms") or rec_ts_ms

                # Use pristine source item_json if available, unless it was converted for DeepSeek XML
                if not is_converted_xml and source_items_map and iid in source_items_map:
                    src_itype, src_ijson = source_items_map[iid]
                    final_item_json = src_ijson
                    final_item_type = src_itype
                else:
                    clean_item = dict(it)
                    clean_item["type"] = clean_itype
                    if clean_itype == "agentMessage" and "text" not in clean_item:
                        clean_item["text"] = "".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
                    elif clean_itype == "commandExecution":
                        if "process_id" in clean_item and "processId" not in clean_item:
                            clean_item["processId"] = clean_item.pop("process_id", None)
                        if "aggregated_output" in clean_item and "aggregatedOutput" not in clean_item:
                            clean_item["aggregatedOutput"] = clean_item.pop("aggregated_output", None)
                        if "exit_code" in clean_item and "exitCode" not in clean_item:
                            clean_item["exitCode"] = clean_item.pop("exit_code", None)
                        if "duration" in clean_item and "durationMs" not in clean_item:
                            clean_item["durationMs"] = clean_item.pop("duration", None)
                        if "parsed_cmd" in clean_item and "commandActions" not in clean_item:
                            clean_item["commandActions"] = clean_item.pop("parsed_cmd", None)
                    final_item_json = json.dumps(clean_item, ensure_ascii=False)
                    final_item_type = clean_itype

                if thread_map:
                    for src_t, tgt_t in thread_map.items():
                        final_item_json = final_item_json.replace(f"<source_thread_id>{src_t}</source_thread_id>", f"<source_thread_id>{tgt_t}</source_thread_id>")
                        final_item_json = final_item_json.replace(f"<source_thread_id> {src_t} </source_thread_id>", f"<source_thread_id>{tgt_t}</source_thread_id>")

                item_metas.append({
                    "thread_id": tgt_id,
                    "turn_id": tid,
                    "item_id": iid,
                    "rollout_ordinal": rec_ord,
                    "created_at_ms": created_ms,
                    "item_json": final_item_json,
                    "item_type": final_item_type,
                    "updated_at_ordinal": rec_ord,
                })

            if p.get("thread_id"):
                p["thread_id"] = tgt_id

        elif rec_type == "turn_context":
            p["cwd"] = tgt_cwd
            p["model"] = tgt_model

        elif rec_type == "response_item":
            if tgt_prov != "openai":
                ptype = p.get("type")
                if ptype == "function_call_output":
                    pname = p.get("name")
                    out_str = p.get("output", "")
                    if pname in ("automation_update", "send_message_to_thread") or "<codex_delegation>" in out_str or "<heartbeat>" in out_str:
                        p["type"] = "message"
                        p["role"] = "user"
                        p["content"] = [{"type": "input_text", "text": out_str}]
                        p.pop("name", None)
                        p.pop("output", None)
                        p.pop("namespace", None)

        elif rec_type == "token_usage_record":
            if p.get("thread_id"):
                p["thread_id"] = tgt_id
            if p.get("session_id"):
                p["session_id"] = tgt_id

        line_str = json.dumps(rec, ensure_ascii=False) + "\n"
        if thread_map:
            for src_t, tgt_t in thread_map.items():
                line_str = line_str.replace(f"<source_thread_id>{src_t}</source_thread_id>", f"<source_thread_id>{tgt_t}</source_thread_id>")
                line_str = line_str.replace(f"<source_thread_id> {src_t} </source_thread_id>", f"<source_thread_id>{tgt_t}</source_thread_id>")

        transformed_lines.append(line_str)

    turn_text = "".join(transformed_lines)
    turn_bytes = turn_text.encode("utf-8")
    turn_end_offset = turn_start_offset + len(turn_bytes)
    turn_end_ord = curr_ord - 1

    turn_meta = {
        "thread_id": tgt_id,
        "turn_id": tid,
        "rollout_ordinal": turn_start_ord,
        "status": "failed" if error_json else "completed",
        "error_json": error_json,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
        "first_user_item_id": first_user_item_id,
        "final_agent_item_id": final_agent_item_id,
        "rollout_byte_offset": turn_start_offset,
        "rollout_end_ordinal": turn_end_ord,
        "rollout_end_byte_offset": turn_end_offset,
    }

    return turn_text, turn_meta, item_metas, curr_ord, turn_end_offset


def build_turn_sync_records(tid, items, curr_ord, curr_offset, tgt_id, tgt_cwd, tgt_model, now_ts, now_ms):
    """
    Fallback synthesis when source rollout binary slice is unavailable.
    Preserves images, steer messages, and delegation/heartbeat payloads if present in item_json.
    """
    user_msgs = []
    agent_msgs = []

    for iid, itype, iord, ijson in items:
        try:
            idata = json.loads(ijson)
        except Exception:
            continue
        if itype == "userMessage":
            content = idata.get("content", [])
            utxt = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            if utxt.strip():
                user_msgs.append(utxt)
        elif itype == "agentMessage":
            atxt = idata.get("text", "") or "".join(
                c.get("text", "") for c in idata.get("content", []) if isinstance(c, dict)
            )
            if atxt.strip():
                agent_msgs.append(atxt)
        elif itype == "functionCallOutput":
            out_str = idata.get("output", "")
            if out_str.strip():
                user_msgs.append(out_str)

    if not user_msgs and not agent_msgs:
        return None, None, None, curr_ord, curr_offset

    combined_user = "\n\n".join(user_msgs) if user_msgs else "[Automated status check]"
    agent_reply = "\n\n".join(agent_msgs) if agent_msgs else ""

    turn_start_offset = curr_offset
    turn_start_ord = curr_ord
    turn_recs = []

    def make_rec(t, p):
        nonlocal curr_ord
        rec_str = make_wire_record(t, p, curr_ord)
        curr_ord += 1
        return rec_str

    turn_recs.append(
        make_rec(
            "event_msg",
            {
                "type": "task_started",
                "turn_id": tid,
                "started_at": now_ts,
                "model_context_window": 996147,
                "collaboration_mode_kind": "default",
            },
        )
    )
    turn_recs.append(
        make_rec(
            "turn_context",
            {"turn_id": tid, "root_turn_id": tid, "cwd": tgt_cwd, "model": tgt_model},
        )
    )
    turn_recs.append(
        make_rec(
            "response_item",
            {
                "type": "message",
                "id": f"msg_u_{tid[:8]}",
                "role": "user",
                "content": [{"type": "input_text", "text": combined_user}],
            },
        )
    )

    u_item_id = f"item_u_{tid[:8]}"
    turn_recs.append(
        make_rec(
            "event_msg",
            {
                "type": "item_completed",
                "thread_id": tgt_id,
                "turn_id": tid,
                "item": {
                    "type": "UserMessage",
                    "id": u_item_id,
                    "content": [{"type": "text", "text": combined_user}],
                },
            },
        )
    )

    a_item_id = str(uuid.uuid4())
    if agent_reply:
        turn_recs.append(
            make_rec(
                "event_msg",
                {
                    "type": "item_completed",
                    "thread_id": tgt_id,
                    "turn_id": tid,
                    "item": {
                        "type": "AgentMessage",
                        "id": a_item_id,
                        "content": [{"type": "Text", "text": agent_reply}],
                        "phase": "final_answer",
                    },
                },
            )
        )
        turn_recs.append(
            make_rec(
                "response_item",
                {
                    "type": "message",
                    "id": a_item_id,
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": agent_reply}],
                    "phase": "final_answer",
                },
            )
        )
        turn_recs.append(
            make_rec(
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": tid,
                    "last_agent_message": agent_reply,
                },
            )
        )

    turn_text = "\n".join(turn_recs) + "\n"
    turn_bytes = turn_text.encode("utf-8")
    turn_end_offset = turn_start_offset + len(turn_bytes)
    turn_end_ord = curr_ord - 1

    turn_meta = {
        "thread_id": tgt_id,
        "turn_id": tid,
        "rollout_ordinal": turn_start_ord,
        "status": "completed",
        "error_json": None,
        "started_at": now_ts,
        "completed_at": now_ts,
        "duration_ms": None,
        "first_user_item_id": u_item_id,
        "final_agent_item_id": a_item_id if agent_reply else None,
        "rollout_byte_offset": turn_start_offset,
        "rollout_end_ordinal": turn_end_ord,
        "rollout_end_byte_offset": turn_end_offset,
    }

    item_metas = [
        {
            "thread_id": tgt_id,
            "turn_id": tid,
            "item_id": u_item_id,
            "rollout_ordinal": turn_start_ord + 3,
            "created_at_ms": now_ms,
            "item_json": json.dumps(
                {"type": "userMessage", "id": u_item_id, "content": [{"type": "text", "text": combined_user}]}
            ),
            "item_type": "userMessage",
            "updated_at_ordinal": turn_start_ord + 3,
        }
    ]

    if agent_reply:
        item_metas.append(
            {
                "thread_id": tgt_id,
                "turn_id": tid,
                "item_id": a_item_id,
                "rollout_ordinal": turn_start_ord + 4,
                "created_at_ms": now_ms,
                "item_json": json.dumps({"type": "agentMessage", "id": a_item_id, "text": agent_reply}),
                "item_type": "agentMessage",
                "updated_at_ordinal": turn_start_ord + 4,
            }
        )

    return turn_text, turn_meta, item_metas, curr_ord, turn_end_offset


def persist_sync_metadata(paths, tgt_id, turns_meta, items_meta, curr_offset, curr_ord, now_ts, now_ms):
    """Save appended turns, items, projection, state_5 timestamps and catalog."""
    if os.path.exists(paths.th_db):
        conn_th = sqlite3.connect(paths.th_db, timeout=10.0)
        cur_th = conn_th.cursor()
        t_cols = [
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
        sql_t = f"INSERT OR REPLACE INTO thread_turns ({','.join(t_cols)}) VALUES ({','.join(['?'] * len(t_cols))})"
        for t in turns_meta:
            cur_th.execute(sql_t, [t[c] for c in t_cols])
        i_cols = [
            "thread_id",
            "turn_id",
            "item_id",
            "rollout_ordinal",
            "created_at_ms",
            "item_json",
            "item_type",
            "updated_at_ordinal",
        ]
        sql_i = f"INSERT OR REPLACE INTO thread_items ({','.join(i_cols)}) VALUES ({','.join(['?'] * len(i_cols))})"
        for i in items_meta:
            cur_th.execute(sql_i, [i[c] for c in i_cols])
        cur_th.execute(
            "UPDATE thread_history_projection_state SET next_rollout_byte_offset = ?, next_rollout_ordinal = ? WHERE thread_id = ?",
            (curr_offset, curr_ord, tgt_id),
        )
        conn_th.commit()
        conn_th.close()

    if os.path.exists(paths.state_db):
        conn_s = sqlite3.connect(paths.state_db, timeout=10.0)
        conn_s.cursor().execute(
            "UPDATE threads SET updated_at = ?, updated_at_ms = ?, recency_at = ?, recency_at_ms = ? WHERE id = ?",
            (now_ts, now_ms, now_ts, now_ms, tgt_id),
        )
        conn_s.commit()
        conn_s.close()

    if os.path.exists(paths.cat_db):
        try:
            with sqlite3.connect(paths.cat_db, timeout=5.0) as cat_conn:
                cat_conn.cursor().execute(
                    "UPDATE local_thread_catalog SET source_updated_at = ?, source_recency_at = ? WHERE thread_id = ?",
                    (now_ts, now_ts, tgt_id),
                )
                cat_conn.commit()
        except Exception:
            pass

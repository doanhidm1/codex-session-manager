import json
import uuid

from .rollout import make_wire_record


def build_turn_sync_records(tid, items, curr_ord, curr_offset, tgt_id, tgt_cwd, tgt_model, now_ts, now_ms):
    """
    Construct wire JSONL chunks and SQLite metadata dictionaries for appending
    a conversation turn from source into target thread.
    """
    user_msgs = []
    agent_msgs = []

    for iid, itype, iord, ijson in items:
        idata = json.loads(ijson)
        if itype == 'userMessage':
            utxt = "".join(c.get('text', '') for c in idata.get('content', []) if isinstance(c, dict))
            if utxt.strip():
                user_msgs.append(utxt)
        elif itype == 'agentMessage':
            atxt = idata.get('text', '') or "".join(c.get('text', '') for c in idata.get('content', []) if isinstance(c, dict))
            if atxt.strip():
                agent_msgs.append(atxt)

    if not user_msgs and not agent_msgs:
        return None, None, None, curr_ord, curr_offset

    combined_user = "\n\n".join(user_msgs) if user_msgs else "[Automated status update]"
    agent_reply = "\n\n".join(agent_msgs) if agent_msgs else ""

    turn_start_offset = curr_offset
    turn_start_ord = curr_ord
    turn_recs = []

    def make_rec(t, p):
        nonlocal curr_ord
        rec_str = make_wire_record(t, p, curr_ord)
        curr_ord += 1
        return rec_str

    turn_recs.append(make_rec("event_msg", {
        "type": "task_started",
        "turn_id": tid,
        "started_at": now_ts,
        "model_context_window": 996147,
        "collaboration_mode_kind": "default"
    }))
    turn_recs.append(make_rec("turn_context", {
        "turn_id": tid,
        "root_turn_id": tid,
        "cwd": tgt_cwd,
        "model": tgt_model
    }))
    turn_recs.append(make_rec("response_item", {
        "type": "message",
        "id": f"msg_u_{tid[:8]}",
        "role": "user",
        "content": [{"type": "input_text", "text": combined_user}]
    }))

    u_item_id = f"item_u_{tid[:8]}"
    turn_recs.append(make_rec("event_msg", {
        "type": "item_completed",
        "thread_id": tgt_id,
        "turn_id": tid,
        "item": {
            "type": "UserMessage",
            "id": u_item_id,
            "content": [{"type": "text", "text": combined_user}]
        }
    }))

    a_item_id = str(uuid.uuid4())
    if agent_reply:
        turn_recs.append(make_rec("event_msg", {
            "type": "item_completed",
            "thread_id": tgt_id,
            "turn_id": tid,
            "item": {
                "type": "AgentMessage",
                "id": a_item_id,
                "content": [{"type": "Text", "text": agent_reply}],
                "phase": "final_answer"
            }
        }))
        turn_recs.append(make_rec("response_item", {
            "type": "message",
            "id": a_item_id,
            "role": "assistant",
            "content": [{"type": "output_text", "text": agent_reply}],
            "phase": "final_answer"
        }))
        turn_recs.append(make_rec("event_msg", {
            "type": "task_complete",
            "turn_id": tid,
            "last_agent_message": agent_reply
        }))

    turn_text = "\n".join(turn_recs) + "\n"
    turn_bytes = turn_text.encode('utf-8')
    turn_end_offset = turn_start_offset + len(turn_bytes)
    turn_end_ord = curr_ord - 1

    turn_meta = {
        'thread_id': tgt_id,
        'turn_id': tid,
        'rollout_ordinal': turn_start_ord,
        'status': 'completed',
        'error_json': None,
        'started_at': now_ts,
        'completed_at': now_ts,
        'duration_ms': None,
        'first_user_item_id': u_item_id,
        'final_agent_item_id': a_item_id if agent_reply else None,
        'rollout_byte_offset': turn_start_offset,
        'rollout_end_ordinal': turn_end_ord,
        'rollout_end_byte_offset': turn_end_offset
    }

    item_metas = [{
        'thread_id': tgt_id,
        'turn_id': tid,
        'item_id': u_item_id,
        'rollout_ordinal': turn_start_ord + 3,
        'created_at_ms': now_ms,
        'item_json': json.dumps({"type": "userMessage", "id": u_item_id, "content": [{"type": "text", "text": combined_user}]}),
        'item_type': 'userMessage',
        'updated_at_ordinal': turn_start_ord + 3
    }]

    if agent_reply:
        item_metas.append({
            'thread_id': tgt_id,
            'turn_id': tid,
            'item_id': a_item_id,
            'rollout_ordinal': turn_start_ord + 4,
            'created_at_ms': now_ms,
            'item_json': json.dumps({"type": "agentMessage", "id": a_item_id, "text": agent_reply}),
            'item_type': 'agentMessage',
            'updated_at_ordinal': turn_start_ord + 4
        })

    return turn_text, turn_meta, item_metas, curr_ord, turn_end_offset


def persist_sync_metadata(paths, tgt_id, turns_meta, items_meta, curr_offset, curr_ord, now_ts, now_ms):
    """Save appended turns, items, projection, state_5 timestamps and catalog."""
    import os
    import sqlite3

    if os.path.exists(paths.th_db):
        conn_th = sqlite3.connect(paths.th_db, timeout=10.0)
        cur_th = conn_th.cursor()
        t_cols = [
            "thread_id", "turn_id", "rollout_ordinal", "status", "error_json",
            "started_at", "completed_at", "duration_ms", "first_user_item_id",
            "final_agent_item_id", "rollout_byte_offset", "rollout_end_ordinal", "rollout_end_byte_offset",
        ]
        sql_t = f"INSERT OR REPLACE INTO thread_turns ({','.join(t_cols)}) VALUES ({','.join(['?'] * len(t_cols))})"
        for t in turns_meta:
            cur_th.execute(sql_t, [t[c] for c in t_cols])
        i_cols = ["thread_id", "turn_id", "item_id", "rollout_ordinal", "created_at_ms", "item_json", "item_type", "updated_at_ordinal"]
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

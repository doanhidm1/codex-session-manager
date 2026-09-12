import json
import uuid
import time
import os
import shutil
import sqlite3
from .config import CodexPaths, normalize_path
from .db import get_connection, resolve_thread, get_paired_threads
from .rollout import make_wire_record

def sync_threads(src_arg, tgt_arg, codex_home):
    """
    Incrementally append new conversational turns from source thread to target thread.
    Non-destructive (append-only), creates pre-sync backup snapshot.
    """
    paths = CodexPaths(codex_home)
    src_row = resolve_thread(src_arg, codex_home)
    tgt_row = resolve_thread(tgt_arg, codex_home)

    if not src_row:
        print(f"ERROR: Source thread not found: {src_arg}")
        return False
    if not tgt_row:
        print(f"ERROR: Target thread not found: {tgt_arg}")
        return False

    src_id, src_name, src_title, src_prov, src_rollout = src_row
    tgt_id, tgt_name, tgt_title, tgt_prov, tgt_rollout = tgt_row

    if src_id == tgt_id:
        print("ERROR: Source and Target are the same thread!")
        return False

    src_rollout = normalize_path(src_rollout)
    tgt_rollout = normalize_path(tgt_rollout)

    if not os.path.exists(src_rollout):
        print(f"ERROR: Source rollout file does not exist: {src_rollout}")
        return False
    if not os.path.exists(tgt_rollout):
        print(f"ERROR: Target rollout file does not exist: {tgt_rollout}")
        return False

    conn_th = sqlite3.connect(paths.th_db, timeout=10.0)
    cur_th = conn_th.cursor()

    src_turns = cur_th.execute(
        "SELECT turn_id FROM thread_turns WHERE thread_id = ? ORDER BY rollout_ordinal ASC",
        (src_id,)
    ).fetchall()
    src_turn_ids = [r[0] for r in src_turns]

    tgt_turns = cur_th.execute(
        "SELECT turn_id FROM thread_turns WHERE thread_id = ? ORDER BY rollout_ordinal ASC",
        (tgt_id,)
    ).fetchall()
    tgt_turn_ids = set(r[0] for r in tgt_turns)

    new_turn_ids = [tid for tid in src_turn_ids if tid not in tgt_turn_ids]

    if not new_turn_ids:
        print(f"[i] Sessions '{src_name or src_id}' and '{tgt_name or tgt_id}' are in sync (no new turns to append).")
        conn_th.close()
        return True

    print(f"[*] Found {len(new_turn_ids)} new turn(s) from [{src_name or src_id}] to append into [{tgt_name or tgt_id}]...")

    # 1. Backup target rollout
    timestamp_str = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(paths.backup_root, f"{timestamp_str}_sync_{tgt_id}")
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(tgt_rollout, os.path.join(backup_dir, "target_rollout_before_sync.jsonl.bak"))

    # 2. Get target current file size and max ordinal
    tgt_file_size = os.path.getsize(tgt_rollout)
    tgt_max_ord = 0
    with open(tgt_rollout, "rb") as f:
        f.seek(max(0, tgt_file_size - 65536))
        lines = f.readlines()
        for l in reversed(lines):
            try:
                e = json.loads(l.decode('utf-8'))
                if 'ordinal' in e:
                    tgt_max_ord = max(tgt_max_ord, e['ordinal'])
                    break
            except Exception:
                pass

    curr_ord = tgt_max_ord + 1
    curr_offset = tgt_file_size
    now_ts = int(time.time())
    now_ms = int(time.time() * 1000)

    # 3. Read model & cwd of target
    conn_s = get_connection(paths.state_db, timeout=10.0)
    cur_s = conn_s.cursor()
    tgt_model_row = cur_s.execute("SELECT model, cwd FROM threads WHERE id = ?", (tgt_id,)).fetchone()
    tgt_model = (tgt_model_row[0] if tgt_model_row else None) or ("deepseek-flash" if tgt_prov == "deepseek" else "gpt-5.6-sol")
    tgt_cwd = normalize_path((tgt_model_row[1] if tgt_model_row else None) or os.path.expanduser("~"))

    # 4. Extract turns and format records
    appended_turns_meta = []
    appended_items_meta = []
    records_to_append = []

    for tid in new_turn_ids:
        items = cur_th.execute(
            "SELECT item_id, item_type, rollout_ordinal, item_json "
            "FROM thread_items WHERE thread_id = ? AND turn_id = ? ORDER BY rollout_ordinal ASC",
            (src_id, tid)
        ).fetchall()

        user_msgs = []
        agent_msgs = []
        for iid, itype, iord, ijson in items:
            idata = json.loads(ijson)
            if itype == 'userMessage':
                utxt = "".join(c.get('text', '') for c in idata.get('content', []) if isinstance(c, dict))
                if utxt.strip(): user_msgs.append(utxt)
            elif itype == 'agentMessage':
                atxt = idata.get('text', '') or "".join(c.get('text', '') for c in idata.get('content', []) if isinstance(c, dict))
                if atxt.strip(): agent_msgs.append(atxt)

        if not user_msgs and not agent_msgs:
            cur_th.execute(
                "INSERT OR IGNORE INTO thread_turns (thread_id, turn_id, rollout_ordinal, status, started_at, completed_at, rollout_byte_offset, rollout_end_ordinal, rollout_end_byte_offset) "
                "VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?)",
                (tgt_id, tid, curr_ord, now_ts, now_ts, curr_offset, curr_ord, curr_offset)
            )
            continue

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

        appended_turns_meta.append({
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
        })

        appended_items_meta.append({
            'thread_id': tgt_id,
            'turn_id': tid,
            'item_id': u_item_id,
            'rollout_ordinal': turn_start_ord + 3,
            'created_at_ms': now_ms,
            'item_json': json.dumps({"type":"userMessage","id":u_item_id,"content":[{"type":"text","text":combined_user}]}),
            'item_type': 'userMessage',
            'updated_at_ordinal': turn_start_ord + 3
        })

        if agent_reply:
            appended_items_meta.append({
                'thread_id': tgt_id,
                'turn_id': tid,
                'item_id': a_item_id,
                'rollout_ordinal': turn_start_ord + 4,
                'created_at_ms': now_ms,
                'item_json': json.dumps({"type":"agentMessage","id":a_item_id,"text":agent_reply}),
                'item_type': 'agentMessage',
                'updated_at_ordinal': turn_start_ord + 4
            })

        records_to_append.append(turn_text)
        curr_offset = turn_end_offset

    # 5. Append records to target rollout file
    with open(tgt_rollout, "a", encoding="utf-8") as f:
        for r_chunk in records_to_append:
            f.write(r_chunk)

    # 6. Insert metadata into thread_history_1.sqlite
    turn_cols = [
        'thread_id', 'turn_id', 'rollout_ordinal', 'status', 'error_json',
        'started_at', 'completed_at', 'duration_ms', 'first_user_item_id',
        'final_agent_item_id', 'rollout_byte_offset', 'rollout_end_ordinal',
        'rollout_end_byte_offset'
    ]
    sql_turns = f"INSERT OR REPLACE INTO thread_turns ({','.join(turn_cols)}) VALUES ({','.join(['?'] * len(turn_cols))})"
    for t_data in appended_turns_meta:
        cur_th.execute(sql_turns, [t_data[c] for c in turn_cols])

    item_cols = [
        'thread_id', 'turn_id', 'item_id', 'rollout_ordinal',
        'created_at_ms', 'item_json', 'item_type', 'updated_at_ordinal'
    ]
    sql_items = f"INSERT OR REPLACE INTO thread_items ({','.join(item_cols)}) VALUES ({','.join(['?'] * len(item_cols))})"
    for i_data in appended_items_meta:
        cur_th.execute(sql_items, [i_data[c] for c in item_cols])

    cur_th.execute(
        "UPDATE thread_history_projection_state SET next_rollout_byte_offset = ?, next_rollout_ordinal = ? WHERE thread_id = ?",
        (curr_offset, curr_ord, tgt_id)
    )
    conn_th.commit()
    conn_th.close()

    # 7. Update recency in state_5.sqlite
    cur_s.execute(
        "UPDATE threads SET updated_at = ?, updated_at_ms = ?, recency_at = ?, recency_at_ms = ? WHERE id = ?",
        (now_ts, now_ms, now_ts, now_ms, tgt_id)
    )
    conn_s.commit()
    conn_s.close()

    # 8. Update catalog if exists
    if os.path.exists(paths.cat_db):
        try:
            with sqlite3.connect(paths.cat_db, timeout=5.0) as cat_conn:
                cat_conn.cursor().execute(
                    "UPDATE local_thread_catalog SET source_updated_at = ?, source_recency_at = ? WHERE thread_id = ?",
                    (now_ts, now_ts, tgt_id)
                )
                cat_conn.commit()
        except Exception:
            pass

    print(f"[SUCCESS] Successfully appended {len(appended_turns_meta)} new turn(s) from [{src_name or src_id}] to [{tgt_name or tgt_id}]!")
    return True

def sync_all_pairs(codex_home):
    """Scan and run bidirectional sync on all registered pairs in session_manager.sqlite."""
    paths = CodexPaths(codex_home)
    from .mapping import auto_seed_existing_pairs, get_all_pairs, update_last_synced
    auto_seed_existing_pairs(codex_home)
    pairs = get_all_pairs(paths.mapping_db, active_only=True)

    if not pairs:
        print("[i] No session pairs found in mapping database to synchronize.")
        return True

    print(f"[*] Starting two-way sync for all {len(pairs)} registered session pair(s)...")
    for p in pairs:
        pair_id, name, o_id, d_id, cat, lsync, is_act = p
        print(f"\n--- Syncing pair: [{name}] (OpenAI: {o_id[:8]} <---> DeepSeek: {d_id[:8]}) ---")
        sync_threads(o_id, d_id, codex_home)
        sync_threads(d_id, o_id, codex_home)
        update_last_synced(paths.mapping_db, pair_id)

    print("\n[+] Bidirectional sync completed successfully for all session pairs!")
    return True

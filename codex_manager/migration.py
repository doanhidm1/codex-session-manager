import json
import uuid
import time
import os
import shutil
import sqlite3
from .config import CodexPaths, normalize_path
from .db import get_connection
from .rollout import extract_turns_from_sqlite, extract_turns_from_rollout, make_wire_record

def migrate_thread(source_thread_id, target_provider, codex_home):
    """
    Clone an existing session (e.g. OpenAI) into a target provider (e.g. DeepSeek).
    Creates full rollout file, populates thread projection cache, and registers in database.
    """
    paths = CodexPaths(codex_home)
    os.makedirs(paths.backup_root, exist_ok=True)

    timestamp_str = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(paths.backup_root, f"{timestamp_str}_{source_thread_id}")

    conn = get_connection(paths.state_db, timeout=15.0)
    cursor = conn.cursor()

    out_rollout = None
    new_thread_id = None

    try:
        cursor.execute("SELECT * FROM threads WHERE id = ?", (source_thread_id,))
        row = cursor.fetchone()
        if not row:
            print(f"ERROR: Thread ID '{source_thread_id}' không tồn tại trong database.")
            conn.close()
            return False

        cols = [d[0] for d in cursor.description]
        thread_data = dict(zip(cols, row))
        source_rollout = normalize_path(thread_data['rollout_path'])

        if not os.path.exists(source_rollout):
            print(f"ERROR: File rollout không tồn tại: {source_rollout}")
            conn.close()
            return False

        # 1. Create snapshot backup
        os.makedirs(backup_dir, exist_ok=True)
        backup_meta_file = os.path.join(backup_dir, "meta.json")
        backup_rollout_file = os.path.join(backup_dir, "source_rollout.jsonl.bak")
        shutil.copy2(source_rollout, backup_rollout_file)

        with open(backup_meta_file, "w", encoding="utf-8") as f:
            json.dump({
                "source_thread_id": source_thread_id,
                "thread_data": thread_data,
                "timestamp": timestamp_str,
                "target_provider": target_provider
            }, f, ensure_ascii=False, indent=2)

        # 2. Prepare new thread metadata
        new_thread_id = str(uuid.uuid4())
        now_ts = int(time.time())
        now_ms = int(time.time() * 1000)

        old_name = thread_data.get('name') or ''
        old_title = thread_data.get('title') or 'New Chat'

        if target_provider == 'deepseek':
            if old_name:
                clean_name = old_name.replace(" (ds)", "").replace("(ds)", "").strip()
                new_name = f"{clean_name} (ds)"
            else:
                new_name = "Chat (ds)"

            clean_title = old_title
            if clean_title.startswith("[DS] "): clean_title = clean_title[5:]
            if clean_title.startswith("[OAI] "): clean_title = clean_title[6:]
            new_title = f"[DS] {clean_title}"
            new_model = "deepseek-flash"
        else:
            if old_name:
                new_name = old_name.replace(" (ds)", "").replace("(ds)", "").strip()
            else:
                new_name = "Chat"

            clean_title = old_title
            if clean_title.startswith("[DS] "): clean_title = clean_title[5:]
            if clean_title.startswith("[OAI] "): clean_title = clean_title[6:]
            new_title = clean_title
            new_model = "gpt-5.6-sol"

        date_parts = time.strftime("%Y/%m/%d").split("/")
        out_dir = os.path.join(paths.sessions_dir, *date_parts)
        os.makedirs(out_dir, exist_ok=True)
        time_fn = time.strftime("%Y-%m-%dT%H-%M-%S")
        out_filename = f"rollout-{time_fn}-{new_thread_id}.jsonl"
        out_rollout = os.path.join(out_dir, out_filename)

        # 3. Read base instructions & clean CWD
        source_l0 = {}
        with open(source_rollout, 'r', encoding='utf-8') as fin:
            first_line = fin.readline()
            try: source_l0 = json.loads(first_line)
            except Exception: pass

        source_meta_payload = source_l0.get('payload', {})
        base_instr_text = source_meta_payload.get('base_instructions', {}).get('text', '')
        if not base_instr_text:
            base_instr_text = "You are Codex, an agent based on GPT-5. You and the user share one workspace, and your job is to help solve their programming and computer tasks."

        clean_cwd = normalize_path(thread_data.get('cwd') or source_meta_payload.get('cwd') or os.path.expanduser("~"))

        # 4. Extract turns
        turns = extract_turns_from_sqlite(paths.th_db, source_thread_id, paths.codex_home)
        if not turns:
            turns = extract_turns_from_rollout(source_rollout)

        ord_num = 0
        records = []

        def add_rec(t, p):
            nonlocal ord_num
            records.append(make_wire_record(t, p, ord_num))
            ord_num += 1

        add_rec("session_meta", {
            "session_id": new_thread_id,
            "id": new_thread_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cwd": clean_cwd,
            "originator": "Codex Desktop",
            "cli_version": "0.153.4",
            "source": "vscode",
            "thread_source": "user",
            "model_provider": target_provider,
            "base_instructions": {
                "text": base_instr_text,
                "provenance": {"type": "model", "model": new_model}
            },
            "history_mode": "paginated",
            "multi_agent_version": "disabled",
            "context_window": {"window_id": str(uuid.uuid4())}
        })

        for t in turns:
            tid = t['turn_id']
            if not t['user_messages']:
                continue

            add_rec("event_msg", {
                "type": "task_started",
                "turn_id": tid,
                "started_at": now_ts,
                "model_context_window": 996147,
                "collaboration_mode_kind": "default"
            })
            add_rec("turn_context", {
                "turn_id": tid,
                "root_turn_id": tid,
                "cwd": clean_cwd,
                "model": new_model
            })

            combined_user = "\n\n".join(t['user_messages'])
            add_rec("response_item", {
                "type": "message",
                "id": f"msg_u_{tid[:8]}",
                "role": "user",
                "content": [{"type": "input_text", "text": combined_user}]
            })
            add_rec("event_msg", {
                "type": "item_completed",
                "thread_id": new_thread_id,
                "turn_id": tid,
                "item": {
                    "type": "UserMessage",
                    "id": f"item_u_{tid[:8]}",
                    "content": [{"type": "text", "text": combined_user}]
                }
            })

            agent_reply = t['last_agent_message'] or ("\n\n".join(t['agent_messages']))
            if agent_reply:
                agent_item_id = str(uuid.uuid4())
                add_rec("event_msg", {
                    "type": "item_completed",
                    "thread_id": new_thread_id,
                    "turn_id": tid,
                    "item": {
                        "type": "AgentMessage",
                        "id": agent_item_id,
                        "content": [{"type": "Text", "text": agent_reply}],
                        "phase": "final_answer"
                    }
                })
                add_rec("response_item", {
                    "type": "message",
                    "id": agent_item_id,
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": agent_reply}],
                    "phase": "final_answer"
                })
                add_rec("event_msg", {
                    "type": "task_complete",
                    "turn_id": tid,
                    "last_agent_message": agent_reply
                })

        with open(out_rollout, 'w', encoding='utf-8') as fout:
            fout.write("\n".join(records) + "\n")

        # 5. Populate thread_history_1.sqlite projection state
        if os.path.exists(paths.th_db):
            try:
                file_size = os.path.getsize(out_rollout)
                p_turns = {}
                p_turn_order = []
                p_items = []
                p_curr_tid = None
                p_max_ord = 0

                with open(out_rollout, 'rb') as froll:
                    p_offset = 0
                    while True:
                        l_start = p_offset
                        raw_line = froll.readline()
                        if not raw_line: break
                        l_end = p_offset + len(raw_line)
                        p_offset = l_end

                        try: entry = json.loads(raw_line.decode('utf-8'))
                        except Exception: continue

                        ord_val = entry.get('ordinal', 0)
                        if ord_val > p_max_ord: p_max_ord = ord_val
                        e_type = entry.get('type')
                        e_payload = entry.get('payload', {})
                        p_type = e_payload.get('type')

                        if e_type == 'event_msg':
                            if p_type == 'task_started':
                                tid = e_payload.get('turn_id')
                                if tid:
                                    p_curr_tid = tid
                                    if tid not in p_turns:
                                        p_turn_order.append(tid)
                                        p_turns[tid] = {
                                            'thread_id': new_thread_id,
                                            'turn_id': tid,
                                            'rollout_ordinal': ord_val,
                                            'status': 'completed',
                                            'error_json': None,
                                            'started_at': now_ts,
                                            'completed_at': now_ts,
                                            'duration_ms': None,
                                            'first_user_item_id': None,
                                            'final_agent_item_id': None,
                                            'rollout_byte_offset': l_start,
                                            'rollout_end_ordinal': ord_val,
                                            'rollout_end_byte_offset': l_end
                                        }
                            elif p_type == 'item_completed':
                                tid = e_payload.get('turn_id') or p_curr_tid
                                raw_item = e_payload.get('item', {})
                                itype_raw = raw_item.get('type', '')
                                iid = raw_item.get('id', '')

                                itype = 'unknown'
                                if itype_raw in ('UserMessage', 'userMessage'):
                                    itype = 'userMessage'
                                    clean_item = {
                                        'type': 'userMessage',
                                        'id': iid,
                                        'content': raw_item.get('content', []),
                                        'clientId': raw_item.get('clientId', None)
                                    }
                                    if tid and tid in p_turns and not p_turns[tid]['first_user_item_id']:
                                        p_turns[tid]['first_user_item_id'] = iid
                                elif itype_raw in ('AgentMessage', 'agentMessage'):
                                    itype = 'agentMessage'
                                    text_val = raw_item.get('text', '')
                                    if not text_val and 'content' in raw_item:
                                        text_val = "".join(c.get('text', '') for c in raw_item.get('content', []) if isinstance(c, dict))
                                    clean_item = {
                                        'type': 'agentMessage',
                                        'id': iid,
                                        'text': text_val,
                                        'phase': raw_item.get('phase', 'final_answer')
                                    }
                                    if tid and tid in p_turns:
                                        p_turns[tid]['final_agent_item_id'] = iid
                                else:
                                    itype = itype_raw[0].lower() + itype_raw[1:] if itype_raw else 'unknown'
                                    clean_item = dict(raw_item)
                                    clean_item['type'] = itype

                                if tid and tid in p_turns:
                                    p_turns[tid]['rollout_end_ordinal'] = ord_val
                                    p_turns[tid]['rollout_end_byte_offset'] = l_end

                                p_items.append({
                                    'thread_id': new_thread_id,
                                    'turn_id': tid,
                                    'item_id': iid,
                                    'rollout_ordinal': ord_val,
                                    'created_at_ms': now_ms,
                                    'item_json': json.dumps(clean_item, ensure_ascii=False),
                                    'item_type': itype,
                                    'updated_at_ordinal': ord_val
                                })
                            elif p_type == 'task_complete':
                                tid = e_payload.get('turn_id') or p_curr_tid
                                if tid and tid in p_turns:
                                    p_turns[tid]['rollout_end_ordinal'] = ord_val
                                    p_turns[tid]['rollout_end_byte_offset'] = l_end

                th_conn = sqlite3.connect(paths.th_db, timeout=10.0)
                th_cur = th_conn.cursor()
                th_cur.execute("DELETE FROM thread_history_projection_state WHERE thread_id = ?", (new_thread_id,))
                th_cur.execute("DELETE FROM thread_turns WHERE thread_id = ?", (new_thread_id,))
                th_cur.execute("DELETE FROM thread_items WHERE thread_id = ?", (new_thread_id,))
                th_cur.execute("DELETE FROM thread_realtime_items WHERE thread_id = ?", (new_thread_id,))

                turn_cols = [
                    'thread_id', 'turn_id', 'rollout_ordinal', 'status', 'error_json',
                    'started_at', 'completed_at', 'duration_ms', 'first_user_item_id',
                    'final_agent_item_id', 'rollout_byte_offset', 'rollout_end_ordinal',
                    'rollout_end_byte_offset'
                ]
                sql_turns = f"INSERT INTO thread_turns ({','.join(turn_cols)}) VALUES ({','.join(['?'] * len(turn_cols))})"
                for tid in p_turn_order:
                    t_data = p_turns[tid]
                    th_cur.execute(sql_turns, [t_data[c] for c in turn_cols])

                item_cols = [
                    'thread_id', 'turn_id', 'item_id', 'rollout_ordinal',
                    'created_at_ms', 'item_json', 'item_type', 'updated_at_ordinal'
                ]
                sql_items = f"INSERT INTO thread_items ({','.join(item_cols)}) VALUES ({','.join(['?'] * len(item_cols))})"
                for item in p_items:
                    th_cur.execute(sql_items, [item[c] for c in item_cols])

                th_cur.execute(
                    "INSERT INTO thread_history_projection_state (thread_id, next_rollout_byte_offset, next_rollout_ordinal) VALUES (?, ?, ?)",
                    (new_thread_id, file_size, p_max_ord + 1)
                )

                th_conn.commit()
                th_conn.close()
            except Exception:
                pass

        # 6. Insert new thread record in state_5.sqlite
        new_thread = dict(thread_data)
        new_thread['id'] = new_thread_id
        new_thread['name'] = new_name
        new_thread['title'] = new_title
        new_thread['rollout_path'] = out_rollout
        new_thread['model_provider'] = target_provider
        new_thread['model'] = new_model
        new_thread['created_at'] = now_ts
        new_thread['updated_at'] = now_ts
        new_thread['created_at_ms'] = now_ms
        new_thread['updated_at_ms'] = now_ms
        new_thread['recency_at'] = now_ts
        new_thread['recency_at_ms'] = now_ms

        insert_cols = list(new_thread.keys())
        placeholders = ",".join(["?"] * len(insert_cols))
        sql = f"INSERT INTO threads ({','.join(insert_cols)}) VALUES ({placeholders})"
        cursor.execute(sql, [new_thread[c] for c in insert_cols])
        conn.commit()
        conn.close()

        # 7. Register in local_thread_catalog if available
        if os.path.exists(paths.cat_db):
            try:
                with sqlite3.connect(paths.cat_db, timeout=5.0) as cat_conn:
                    cat_cur = cat_conn.cursor()
                    cat_cur.execute(
                        "INSERT OR REPLACE INTO local_thread_catalog "
                        "(host_id, thread_id, display_title, source_created_at, source_updated_at, cwd, "
                        "source_kind, model_provider, thread_source, source_recency_at) "
                        "VALUES ('local', ?, ?, ?, ?, ?, 'local', ?, 'user', ?)",
                        (new_thread_id, new_name, now_ts, now_ts, clean_cwd, target_provider, now_ts)
                    )
                    cat_conn.commit()
            except Exception:
                pass

        with open(backup_meta_file, "r", encoding="utf-8") as f:
            bm = json.load(f)
        bm["created_thread_id"] = new_thread_id
        bm["created_rollout"] = out_rollout
        with open(backup_meta_file, "w", encoding="utf-8") as f:
            json.dump(bm, f, ensure_ascii=False, indent=2)

        print(f"SUCCESS:{new_thread_id}:{new_name}:{backup_dir}")
        return True

    except Exception as e:
        conn.rollback()
        conn.close()
        if out_rollout and os.path.exists(out_rollout):
            try: os.remove(out_rollout)
            except Exception: pass
        print(f"ERROR: {e}")
        return False

def rollback_thread(target_id, codex_home):
    """Undo a migrated session using backup metadata."""
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.backup_root):
        print("ERROR: Không tìm thấy thư mục backup-sessions.")
        return False

    dirs = sorted(os.listdir(paths.backup_root), reverse=True)
    if not dirs:
        print("ERROR: Không có bản backup nào.")
        return False

    selected_dir = None
    for d in dirs:
        meta_p = os.path.join(paths.backup_root, d, "meta.json")
        if os.path.exists(meta_p):
            with open(meta_p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not target_id or data.get("created_thread_id") == target_id or data.get("source_thread_id") == target_id:
                selected_dir = os.path.join(paths.backup_root, d)
                break

    if not selected_dir:
        print(f"ERROR: Không tìm thấy backup phù hợp với ID: {target_id}")
        return False

    meta_p = os.path.join(selected_dir, "meta.json")
    with open(meta_p, "r", encoding="utf-8") as f:
        meta = json.load(f)

    created_tid = meta.get("created_thread_id")
    created_rollout = normalize_path(meta.get("created_rollout"))

    conn = get_connection(paths.state_db, timeout=15.0)
    cur = conn.cursor()

    if created_tid:
        cur.execute("DELETE FROM threads WHERE id = ?", (created_tid,))
        conn.commit()
        print(f"[*] Đã xoá thread [{created_tid}] khỏi database.")

    if created_rollout and os.path.exists(created_rollout):
        try:
            os.remove(created_rollout)
            print(f"[*] Đã xoá file rollout: {created_rollout}")
        except Exception as e:
            print(f"[!] Không thể xoá file: {e}")

    conn.close()

    if created_tid and os.path.exists(paths.th_db):
        try:
            th_conn = sqlite3.connect(paths.th_db, timeout=10.0)
            th_cur = th_conn.cursor()
            th_cur.execute("DELETE FROM thread_history_projection_state WHERE thread_id = ?", (created_tid,))
            th_cur.execute("DELETE FROM thread_turns WHERE thread_id = ?", (created_tid,))
            th_cur.execute("DELETE FROM thread_items WHERE thread_id = ?", (created_tid,))
            th_cur.execute("DELETE FROM thread_realtime_items WHERE thread_id = ?", (created_tid,))
            th_conn.commit()
            th_conn.close()
        except Exception:
            pass

    if created_tid and os.path.exists(paths.cat_db):
        try:
            with sqlite3.connect(paths.cat_db, timeout=5.0) as cat_conn:
                cat_conn.cursor().execute("DELETE FROM local_thread_catalog WHERE thread_id = ?", (created_tid,))
                cat_conn.commit()
        except Exception:
            pass

    print(f"SUCCESS: Đã hoàn tác session [{created_tid}] từ backup: {selected_dir}")
    return True

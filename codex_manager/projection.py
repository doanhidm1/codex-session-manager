import json
import os
import sqlite3
import time

from .config import normalize_path


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

    with open(rollout_path, 'rb') as froll:
        p_offset = 0
        while True:
            l_start = p_offset
            raw_line = froll.readline()
            if not raw_line:
                break
            l_end = p_offset + len(raw_line)
            p_offset = l_end

            try:
                entry = json.loads(raw_line.decode('utf-8'))
            except Exception:
                continue

            ord_val = entry.get('ordinal', 0)
            if ord_val > p_max_ord:
                p_max_ord = ord_val

            e_type = entry.get('type')
            e_payload = entry.get('payload', {})
            p_type = e_payload.get('type')

            if e_type == 'event_msg':
                if p_type == 'task_started':
                    tid = e_payload.get('turn_id')
                    if tid:
                        p_curr_tid = tid
                        s_at = e_payload.get('started_at') or now_ts
                        if tid not in p_turns:
                            p_turn_order.append(tid)
                            p_turns[tid] = {
                                'thread_id': thread_id,
                                'turn_id': tid,
                                'rollout_ordinal': ord_val,
                                'status': 'completed',
                                'error_json': None,
                                'started_at': s_at,
                                'completed_at': s_at,
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

                    item_created_ms = e_payload.get('started_at_ms') or e_payload.get('completed_at_ms') or now_ms

                    p_items.append({
                        'thread_id': thread_id,
                        'turn_id': tid,
                        'item_id': iid,
                        'rollout_ordinal': ord_val,
                        'created_at_ms': item_created_ms,
                        'item_json': json.dumps(clean_item, ensure_ascii=False),
                        'item_type': itype,
                        'updated_at_ordinal': ord_val
                    })
                elif p_type == 'task_complete':
                    tid = e_payload.get('turn_id') or p_curr_tid
                    if tid and tid in p_turns:
                        p_turns[tid]['rollout_end_ordinal'] = ord_val
                        p_turns[tid]['rollout_end_byte_offset'] = l_end
                        if e_payload.get('completed_at'):
                            p_turns[tid]['completed_at'] = e_payload['completed_at']
                        if e_payload.get('duration_ms'):
                            p_turns[tid]['duration_ms'] = e_payload['duration_ms']
                        if e_payload.get('error'):
                            p_turns[tid]['status'] = 'failed'
                            p_turns[tid]['error_json'] = json.dumps(e_payload['error'], ensure_ascii=False)

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
        (thread_id, file_size, p_max_ord + 1)
    )

    th_conn.commit()
    th_conn.close()
    return True

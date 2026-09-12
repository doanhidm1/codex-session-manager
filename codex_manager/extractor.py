import json
import os
import re
import sqlite3
import time
import uuid
from collections import OrderedDict


def extract_turns_from_sqlite(th_db, source_thread_id, codex_home):
    """
    Extract structured conversational turns and messages from thread_history_1.sqlite.
    Handles userMessage, agentMessage, functionCallOutput delegations and automations.
    """
    if not os.path.exists(th_db):
        return []

    turns = []
    now_ts = int(time.time())

    try:
        conn = sqlite3.connect(th_db, timeout=5.0)
        cur = conn.cursor()

        t_rows = cur.execute("""
            SELECT turn_id, rollout_ordinal, started_at
            FROM thread_turns
            WHERE thread_id = ?
            ORDER BY rollout_ordinal
        """, (source_thread_id,)).fetchall()

        i_rows = cur.execute("""
            SELECT item_id, turn_id, item_type, rollout_ordinal, item_json
            FROM thread_items
            WHERE thread_id = ? AND item_type IN ('userMessage', 'agentMessage', 'functionCallOutput')
            ORDER BY rollout_ordinal
        """, (source_thread_id,)).fetchall()

        # Thread names map for friendly delegation attribution
        thread_names = {}
        try:
            state_db = os.path.join(codex_home, "state_5.sqlite")
            if os.path.exists(state_db):
                with sqlite3.connect(state_db, timeout=3.0) as s_conn:
                    for s_row in s_conn.cursor().execute("SELECT id, name, title FROM threads").fetchall():
                        thread_names[s_row[0]] = s_row[1] or s_row[2] or "Another task"
        except Exception:
            pass

        if i_rows:
            turn_order = []
            turns_map = OrderedDict()

            for tr in t_rows:
                tid_val = tr[0]
                turn_order.append(tid_val)
                turns_map[tid_val] = {
                    'turn_id': tid_val,
                    'started_at': tr[2] or now_ts,
                    'user_messages': [],
                    'agent_messages': [],
                    'delegations': [],
                    'automations': [],
                    'last_agent_message': ''
                }

            for ir in i_rows:
                iid, itid, itype, iord, ijson = ir
                if itid not in turns_map:
                    turn_order.append(itid)
                    turns_map[itid] = {
                        'turn_id': itid,
                        'started_at': now_ts,
                        'user_messages': [],
                        'agent_messages': [],
                        'delegations': [],
                        'automations': [],
                        'last_agent_message': ''
                    }

                idata = json.loads(ijson)
                if itype == 'userMessage':
                    utxt = "".join(c.get('text', '') for c in idata.get('content', []) if isinstance(c, dict))
                    if utxt.strip():
                        turns_map[itid]['user_messages'].append(utxt)
                elif itype == 'agentMessage':
                    atxt = idata.get('text', '') or "".join(c.get('text', '') for c in idata.get('content', []) if isinstance(c, dict))
                    if atxt.strip():
                        turns_map[itid]['agent_messages'].append(atxt)
                        turns_map[itid]['last_agent_message'] = atxt
                elif itype == 'functionCallOutput':
                    fname = idata.get('name')
                    out_str = idata.get('output', '')
                    if fname == 'send_message_to_thread' or '<codex_delegation>' in out_str:
                        m_src = re.search(r'<source_thread_id>(.*?)</source_thread_id>', out_str, re.DOTALL)
                        m_inp = re.search(r'<input>(.*?)</input>', out_str, re.DOTALL)
                        src_id = m_src.group(1).strip() if m_src else ""
                        sender_name = thread_names.get(src_id, "another task")
                        inp_text = m_inp.group(1).strip() if m_inp else out_str.strip()
                        turns_map[itid]['delegations'].append((sender_name, inp_text))
                    elif fname == 'automation_update' or '<heartbeat>' in out_str:
                        m_inst = re.search(r'<instructions>(.*?)</instructions>', out_str, re.DOTALL)
                        inst_text = m_inst.group(1).strip() if m_inst else "Automated status check"
                        turns_map[itid]['automations'].append(inst_text)

            for to_k in turn_order:
                td = turns_map[to_k]
                if not td['user_messages']:
                    if td['delegations']:
                        parts = [f"[Sent by {snd} from another task]\n{inp}" for snd, inp in td['delegations']]
                        td['user_messages'].append("\n\n".join(parts))
                    elif td['automations']:
                        td['user_messages'].append("[Automated status check]\n" + "\n\n".join(td['automations']))

                if td['user_messages'] or td['agent_messages']:
                    if not td['user_messages'] and turns:
                        if td['agent_messages']:
                            turns[-1]['agent_messages'].extend(td['agent_messages'])
                            turns[-1]['last_agent_message'] = td['last_agent_message'] or turns[-1]['last_agent_message']
                    else:
                        if not td['user_messages']:
                            td['user_messages'].append("[Workspace session initialized]")
                        turns.append(td)

        conn.close()
    except Exception:
        pass

    return turns

def extract_turns_from_rollout(source_rollout):
    """Fallback parser reading lines directly from rollout jsonl file."""
    turns = []
    curr_turn = None

    if not os.path.exists(source_rollout):
        return turns

    with open(source_rollout, 'r', encoding='utf-8') as fin:
        for line in fin:
            try:
                data = json.loads(line)
            except Exception:
                continue

            ltype = data.get('type')
            payload = data.get('payload', {})

            if ltype == 'event_msg' and payload.get('type') == 'task_started':
                curr_turn = {
                    'turn_id': payload.get('turn_id') or str(uuid.uuid4()),
                    'user_messages': [],
                    'agent_messages': [],
                    'last_agent_message': ''
                }
                turns.append(curr_turn)
            elif ltype == 'response_item':
                ptype = payload.get('type')
                if ptype == 'message':
                    role = payload.get('role')
                    txt = "".join(c.get('text', '') for c in payload.get('content', []) if isinstance(c, dict))
                    if curr_turn is None:
                        curr_turn = {
                            'turn_id': str(uuid.uuid4()),
                            'user_messages': [],
                            'agent_messages': [],
                            'last_agent_message': ''
                        }
                        turns.append(curr_turn)
                    if role == 'user' and txt.strip():
                        curr_turn['user_messages'].append(txt)
                    elif role == 'assistant' and txt.strip():
                        curr_turn['agent_messages'].append(txt)
                        curr_turn['last_agent_message'] = txt

    return turns

import json
import uuid
import time
import os
import re
import sqlite3
from collections import OrderedDict
from .config import normalize_path

def make_wire_record(rec_type, payload, ord_num):
    """Serialize a line for Codex JSONL rollout files."""
    return json.dumps({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ordinal": ord_num,
        "type": rec_type,
        "payload": payload
    }, ensure_ascii=False)

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
                elif ptype == 'function_call_output':
                    fname = payload.get('name')
                    out_str = payload.get('output', '')
                    if fname == 'send_message_to_thread' or '<codex_delegation>' in out_str:
                        m_inp = re.search(r'<input>(.*?)</input>', out_str, re.DOTALL)
                        inp_text = m_inp.group(1).strip() if m_inp else out_str.strip()
                        if curr_turn is None:
                            curr_turn = {
                                'turn_id': str(uuid.uuid4()),
                                'user_messages': [],
                                'agent_messages': [],
                                'last_agent_message': ''
                            }
                            turns.append(curr_turn)
                        curr_turn['user_messages'].append(f"[Sent from another task]\n{inp_text}")
            elif ltype == 'event_msg' and payload.get('type') == 'task_complete':
                if curr_turn and payload.get('last_agent_message'):
                    curr_turn['last_agent_message'] = payload.get('last_agent_message')

    return turns

def build_migrated_rollout_file(source_rollout, new_thread_id, new_model, target_provider, clean_cwd, turns, out_rollout_path):
    """
    Build and write a complete, valid Codex rollout JSONL file for a migrated thread.
    Faithfully inherits metadata from source_rollout Line 0 without fabricating fake prompts.
    """
    source_l0 = {}
    if os.path.exists(source_rollout):
        with open(source_rollout, 'r', encoding='utf-8') as fin:
            first_line = fin.readline()
            try:
                source_l0 = json.loads(first_line)
            except Exception:
                pass

    source_payload = source_l0.get('payload', {})

    # Inherit source metadata faithfully
    new_meta_payload = dict(source_payload)
    new_meta_payload["session_id"] = new_thread_id
    new_meta_payload["id"] = new_thread_id
    new_meta_payload["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    new_meta_payload["model_provider"] = target_provider
    if clean_cwd:
        new_meta_payload["cwd"] = clean_cwd

    # If base_instructions exists, update model provenance if present.
    # If missing or empty, DO NOT fabricate any hardcoded prompt!
    if "base_instructions" in new_meta_payload and new_meta_payload["base_instructions"]:
        bi = dict(new_meta_payload["base_instructions"])
        if "provenance" in bi and isinstance(bi["provenance"], dict):
            bi["provenance"] = dict(bi["provenance"])
            bi["provenance"]["model"] = new_model
        new_meta_payload["base_instructions"] = bi

    now_ts = int(time.time())
    ord_num = 0
    records = []

    def add_rec(t, p):
        nonlocal ord_num
        records.append(make_wire_record(t, p, ord_num))
        ord_num += 1

    add_rec("session_meta", new_meta_payload)

    for t in turns:
        tid = t['turn_id']
        if not t['user_messages']:
            continue

        started_at = t.get('started_at', now_ts)
        model_ctx = t.get('model_context_window', None)

        task_payload = {
            "type": "task_started",
            "turn_id": tid,
            "started_at": started_at,
            "collaboration_mode_kind": "default"
        }
        if model_ctx is not None:
            task_payload["model_context_window"] = model_ctx
        add_rec("event_msg", task_payload)

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

    with open(out_rollout_path, 'w', encoding='utf-8') as fout:
        fout.write("\n".join(records) + "\n")

    return out_rollout_path

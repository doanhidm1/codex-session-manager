import json
import uuid
import time
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

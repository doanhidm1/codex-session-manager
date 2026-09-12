import json
import os
import time


def make_wire_record(rec_type, payload, ord_num):
    """Serialize a single wire record for Codex JSONL rollout files."""
    return json.dumps({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ordinal": ord_num,
        "type": rec_type,
        "payload": payload
    }, ensure_ascii=False)

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

        user_combined = "\n\n".join(t['user_messages'])
        u_msg_id = f"msg_u_{tid[:8]}"
        u_item_id = f"item_u_{tid[:8]}"

        add_rec("response_item", {
            "type": "message",
            "id": u_msg_id,
            "role": "user",
            "content": [{"type": "input_text", "text": user_combined}]
        })

        add_rec("event_msg", {
            "type": "item_completed",
            "thread_id": new_thread_id,
            "turn_id": tid,
            "item": {
                "type": "UserMessage",
                "id": u_item_id,
                "content": [{"type": "text", "text": user_combined}]
            }
        })

        agent_reply = t.get('last_agent_message', '')
        if not agent_reply and t.get('agent_messages'):
            agent_reply = "\n\n".join(t['agent_messages'])

        if agent_reply:
            import uuid
            a_item_id = str(uuid.uuid4())
            add_rec("event_msg", {
                "type": "item_completed",
                "thread_id": new_thread_id,
                "turn_id": tid,
                "item": {
                    "type": "AgentMessage",
                    "id": a_item_id,
                    "content": [{"type": "Text", "text": agent_reply}],
                    "phase": "final_answer"
                }
            })

            add_rec("response_item", {
                "type": "message",
                "id": a_item_id,
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
        for r in records:
            fout.write(r + "\n")

    return True

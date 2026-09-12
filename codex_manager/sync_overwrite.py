import os
import shutil
import sqlite3
import time
import uuid

from .config import CodexPaths, normalize_path
from .db import resolve_thread
from .extractor import extract_turns_from_rollout
from .rollout import make_wire_record


def overwrite_target_from_source(src_arg, tgt_arg, codex_home, force=False):
    """
    Completely rebuild target session by converting all turns from source.
    Overwrites target rollout and replaces SQLite history, wiping any corruption.
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

    from .activity import assert_no_running_sessions

    if not assert_no_running_sessions(codex_home, force=force):
        return False

    now = time.time()
    if os.path.exists(tgt_rollout):
        bak_file = tgt_rollout + f".pre_overwrite_{int(now)}.bak"
        shutil.copy2(tgt_rollout, bak_file)
    turns = extract_turns_from_rollout(src_rollout, codex_home)
    if not turns:
        print(f"ERROR: No turns extracted from source rollout: {src_rollout}")
        return False

    # Create fresh target rollout
    now = time.time()
    date_str = time.strftime("%Y\\%m\\%d", time.localtime(now))
    target_dir = os.path.join(paths.sessions_dir, date_str)
    os.makedirs(target_dir, exist_ok=True)
    ts_str = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(now))
    new_tgt_rollout = os.path.join(target_dir, f"rollout-{ts_str}-{tgt_id}.jsonl")

    # Connect to databases
    conn_th = sqlite3.connect(paths.th_db, timeout=10.0)
    cur_th = conn_th.cursor()
    cur_th.execute("DELETE FROM thread_turns WHERE thread_id = ?", (tgt_id,))
    cur_th.execute("DELETE FROM thread_items WHERE thread_id = ?", (tgt_id,))

    curr_ord = 0
    curr_offset = 0
    parent_uuid = str(uuid.uuid4())

    with open(new_tgt_rollout, "w", encoding="utf-8", newline="\n") as out_f:
        # Write initial session meta
        meta_payload = {"id": tgt_id, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))}
        rec = make_wire_record("session_meta", meta_payload, curr_ord)
        out_f.write(rec + "\n")
        curr_offset += len((rec + "\n").encode("utf-8"))
        curr_ord += 1

        for turn in turns:
            turn_uuid = turn.get("turn_id") or str(uuid.uuid4())
            user_text = turn.get("user_prompt", "")
            turn_start_offset = curr_offset
            turn_start_ord = curr_ord

            # Task started
            ts_payload = {
                "type": "task_started",
                "turn_id": turn_uuid,
                "started_at": int(now),
                "model_context_window": 1000000,
            }
            r_str = make_wire_record("event_msg", ts_payload, curr_ord)
            out_f.write(r_str + "\n")
            curr_offset += len((r_str + "\n").encode("utf-8"))
            curr_ord += 1

            # User message
            um_id = f"user_msg_{turn_uuid[:12]}"
            um_payload = {"type": "user_message", "message": user_text, "parent_item_id": parent_uuid}
            r_str = make_wire_record("response_item", um_payload, curr_ord)
            out_f.write(r_str + "\n")
            curr_offset += len((r_str + "\n").encode("utf-8"))
            curr_ord += 1
            parent_uuid = um_id

            agent_text = turn.get("agent_response", "")
            final_item_id = um_id
            if agent_text:
                am_id = f"agent_msg_{turn_uuid[:12]}"
                am_payload = {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": agent_text}],
                    "parent_item_id": parent_uuid,
                }
                r_str = make_wire_record("response_item", am_payload, curr_ord)
                out_f.write(r_str + "\n")
                curr_offset += len((r_str + "\n").encode("utf-8"))
                curr_ord += 1
                parent_uuid = am_id
                final_item_id = am_id

            turn_end_ord = curr_ord - 1
            turn_end_offset = curr_offset

            # Project into SQLite
            cur_th.execute(
                "INSERT OR REPLACE INTO thread_turns "
                "(thread_id, turn_id, rollout_ordinal, status, error_json, started_at, completed_at, duration_ms, first_user_item_id, final_agent_item_id, rollout_byte_offset, rollout_end_ordinal, rollout_end_byte_offset) "
                "VALUES (?, ?, ?, 'completed', NULL, ?, ?, 1000, ?, ?, ?, ?, ?)",
                (
                    tgt_id,
                    turn_uuid,
                    turn_start_ord,
                    int(now),
                    int(now),
                    um_id,
                    final_item_id,
                    turn_start_offset,
                    turn_end_ord,
                    turn_end_offset,
                ),
            )

    cur_th.execute(
        "INSERT OR REPLACE INTO thread_history_projection_state (thread_id, next_rollout_byte_offset, next_rollout_ordinal) VALUES (?, ?, ?)",
        (tgt_id, curr_offset, curr_ord),
    )
    conn_th.commit()
    conn_th.close()

    # Update state_5.sqlite
    with sqlite3.connect(paths.state_db, timeout=10.0) as conn_s:
        conn_s.cursor().execute(
            "UPDATE threads SET rollout_path = ?, updated_at = ? WHERE id = ?", (new_tgt_rollout, int(now), tgt_id)
        )
        conn_s.commit()

    # Update mapping last_synced
    from .mapping import get_all_pairs, update_last_synced

    pairs = get_all_pairs(paths.mapping_db, active_only=False)
    for p in pairs:
        pid, _, o_id, d_id = p[0], p[1], p[2], p[3]
        if (src_id in (o_id, d_id)) and (tgt_id in (o_id, d_id)):
            update_last_synced(paths.mapping_db, pid)

    print(
        f"[SUCCESS] Full convert overwrite completed: [{tgt_name or tgt_id}] was cleanly rebuilt with {len(turns)} turns from [{src_name or src_id}]!"
    )
    return True


def overwrite_all_mapped_pairs(codex_home, target_provider=None, force=False):
    """Rebuild all registered session pairs using full convert overwrite from active source."""
    paths = CodexPaths(codex_home)
    from .mapping import get_all_pairs

    pairs = get_all_pairs(paths.mapping_db, active_only=True)
    if not pairs:
        print("[i] No mapped session pairs found to overwrite.")
        return True

    # Detect current provider from config.toml
    cur_prov = "openai"
    if os.path.exists(paths.config_toml):
        import re

        try:
            m = re.search(r'(?m)^model_provider\s*=\s*"([^"]+)"', open(paths.config_toml, "r", encoding="utf-8").read())
            if m:
                cur_prov = m.group(1).lower().strip()
        except Exception:
            pass

    tgt_prov = target_provider or ("deepseek" if cur_prov == "openai" else "openai")
    print(
        f"[*] Full convert overwrite for all {len(pairs)} pair(s) (Source: {cur_prov.upper()} -> Target: {tgt_prov.upper()})..."
    )
    for p in pairs:
        _, name, o_id, d_id = p[0], p[1], p[2], p[3]
        src_id = o_id if cur_prov == "openai" else d_id
        dst_id = d_id if cur_prov == "openai" else o_id
        print(f"\n--- Overwriting [{name}] ({src_id[:8]} -> {dst_id[:8]}) ---")
        overwrite_target_from_source(src_id, dst_id, codex_home, force=force)
    return True

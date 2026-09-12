import json
import os
import shutil
import sqlite3
import time

from .config import CodexPaths, normalize_path
from .db import get_connection, resolve_thread
from .sync_builder import build_turn_sync_records, persist_sync_metadata


def sync_threads(src_arg, tgt_arg, codex_home, force=False):
    """
    Incrementally append new conversational turns from source thread to target thread.
    If target is broken/deleted: requires --force to rebuild via full convert.
    """
    paths = CodexPaths(codex_home)
    src_row = resolve_thread(src_arg, codex_home)
    if not src_row:
        print(f"ERROR: Source thread not found: {src_arg}")
        return False

    src_id, src_name, src_title, src_prov, src_rollout = src_row
    tgt_row = resolve_thread(tgt_arg, codex_home)

    from .pair_health import validate_pair_for_sync

    tgt_prov_hint = "deepseek" if src_prov == "openai" else "openai"
    tgt_id_hint = tgt_row[0] if tgt_row else tgt_arg

    valid, reason = validate_pair_for_sync(src_id, tgt_id_hint, src_prov, tgt_prov_hint, codex_home, force=force)
    if not valid:
        return False
    if reason == "healed":
        return True

    tgt_id, tgt_name, tgt_title, tgt_prov, tgt_rollout = tgt_row
    if src_id == tgt_id:
        print("ERROR: Source and Target are the same thread!")
        return False

    tgt_rollout = normalize_path(tgt_rollout)
    conn_th = sqlite3.connect(paths.th_db, timeout=10.0)
    cur_th = conn_th.cursor()

    src_turns = cur_th.execute(
        "SELECT turn_id FROM thread_turns WHERE thread_id = ? ORDER BY rollout_ordinal ASC",
        (src_id,),
    ).fetchall()
    src_turn_ids = [r[0] for r in src_turns]

    tgt_turns = cur_th.execute(
        "SELECT turn_id FROM thread_turns WHERE thread_id = ? ORDER BY rollout_ordinal ASC",
        (tgt_id,),
    ).fetchall()
    tgt_turn_ids = set(r[0] for r in tgt_turns)

    last_common_idx = -1
    for i, tid in enumerate(src_turn_ids):
        if tid in tgt_turn_ids:
            last_common_idx = max(last_common_idx, i)

    if last_common_idx >= 0:
        new_turn_ids = src_turn_ids[last_common_idx + 1 :]
    else:
        new_turn_ids = [tid for tid in src_turn_ids if tid not in tgt_turn_ids]

    if not new_turn_ids:
        print(f"[i] Sessions '{src_name or src_id[:8]}' and '{tgt_name or tgt_id[:8]}' are in sync (no new turns).")
        conn_th.close()
        return True

    print(
        f"[*] Found {len(new_turn_ids)} new turn(s) from [{src_name or src_id[:8]}] to append into [{tgt_name or tgt_id[:8]}]..."
    )

    # 1. Backup target rollout before mutation
    timestamp_str = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(paths.backup_root, f"{timestamp_str}_sync_{tgt_id}")
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(tgt_rollout, os.path.join(backup_dir, "target_rollout_before_sync.jsonl.bak"))

    # 2. Get target current file size and max ordinal
    tgt_file_size = os.path.getsize(tgt_rollout)
    tgt_max_ord = 0
    with open(tgt_rollout, "rb") as f:
        f.seek(max(0, tgt_file_size - 65536))
        for line in reversed(f.readlines()):
            try:
                e = json.loads(line.decode("utf-8"))
                if "ordinal" in e:
                    tgt_max_ord = max(tgt_max_ord, e["ordinal"])
                    break
            except Exception:
                pass

    curr_ord = tgt_max_ord + 1
    curr_offset = tgt_file_size
    now_ts = int(time.time())
    now_ms = int(time.time() * 1000)

    # 3. Read target model & cwd
    conn_s = get_connection(paths.state_db, timeout=10.0)
    cur_s = conn_s.cursor()
    tgt_model_row = cur_s.execute("SELECT model, cwd FROM threads WHERE id = ?", (tgt_id,)).fetchone()
    tgt_model = (tgt_model_row[0] if tgt_model_row else None) or (
        "deepseek-flash" if tgt_prov == "deepseek" else "gpt-5.6-sol"
    )
    tgt_cwd = normalize_path((tgt_model_row[1] if tgt_model_row else None) or os.path.expanduser("~"))

    # 4. Extract turns and format records
    appended_turns_meta = []
    appended_items_meta = []
    records_to_append = []

    for tid in new_turn_ids:
        items = cur_th.execute(
            "SELECT item_id, item_type, rollout_ordinal, item_json "
            "FROM thread_items WHERE thread_id = ? AND turn_id = ? ORDER BY rollout_ordinal ASC",
            (src_id, tid),
        ).fetchall()

        turn_text, t_meta, i_metas, curr_ord, curr_offset = build_turn_sync_records(
            tid, items, curr_ord, curr_offset, tgt_id, tgt_cwd, tgt_model, now_ts, now_ms
        )

        if turn_text:
            records_to_append.append(turn_text)
            appended_turns_meta.append(t_meta)
            appended_items_meta.extend(i_metas)
        else:
            cur_th.execute(
                "INSERT OR IGNORE INTO thread_turns (thread_id, turn_id, rollout_ordinal, status, started_at, completed_at, rollout_byte_offset, rollout_end_ordinal, rollout_end_byte_offset) "
                "VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?)",
                (tgt_id, tid, curr_ord, now_ts, now_ts, curr_offset, curr_ord, curr_offset),
            )

    # 5. Append records to target rollout file
    with open(tgt_rollout, "a", encoding="utf-8") as f:
        for r_chunk in records_to_append:
            f.write(r_chunk)

    # 6. Persist metadata into thread_history_1.sqlite, state_5.sqlite, and catalog
    conn_th.close()
    conn_s.close()
    persist_sync_metadata(
        paths, tgt_id, appended_turns_meta, appended_items_meta, curr_offset, curr_ord, now_ts, now_ms
    )

    print(
        f"[SUCCESS] Successfully appended {len(appended_turns_meta)} new turn(s) from [{src_name or src_id[:8]}] to [{tgt_name or tgt_id[:8]}]!"
    )
    return True


def sync_all_pairs(codex_home, target_provider=None, force=False):
    """Scan and run bidirectional sync on all registered pairs in session_manager.sqlite."""
    paths = CodexPaths(codex_home)
    from .activity import assert_no_running_sessions

    if not assert_no_running_sessions(codex_home):
        return False

    from .discovery import discover_and_pair_unmapped_threads
    from .mapping import auto_seed_existing_pairs, get_all_pairs, update_last_synced
    from .switch import switch_provider_settings

    act_prov = target_provider
    if not act_prov:
        try:
            with sqlite3.connect(paths.mapping_db, timeout=5.0) as m_conn:
                r = m_conn.cursor().execute(
                    "SELECT value FROM manager_settings WHERE key = 'active_provider'"
                ).fetchone()
                if r:
                    act_prov = r[0]
        except Exception:
            pass
    if act_prov in ("openai", "deepseek"):
        switch_provider_settings(codex_home, act_prov)

    auto_seed_existing_pairs(codex_home)
    discover_and_pair_unmapped_threads(codex_home)
    pairs = get_all_pairs(paths.mapping_db, active_only=True)

    if not pairs:
        print("[i] No session pairs found in mapping database to synchronize.")
        return True

    print(f"[*] Starting two-way sync for all {len(pairs)} registered session pair(s)...")
    for p in pairs:
        pair_id, name, o_id, d_id, cat, lsync, is_act = p
        print(f"\n--- Syncing pair: [{name}] (OpenAI: {o_id[:8]} <---> DeepSeek: {d_id[:8]}) ---")
        sync_threads(o_id, d_id, codex_home, force=force)
        sync_threads(d_id, o_id, codex_home, force=force)
        update_last_synced(paths.mapping_db, pair_id)

    print("\n[+] Bidirectional sync completed successfully for all session pairs!")
    return True

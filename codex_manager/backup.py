import os
import json
import time
import shutil
import sqlite3
from .config import CodexPaths, normalize_path
from .db import get_connection

def create_session_backup(source_thread_id, thread_data, source_rollout, target_provider, backup_root):
    """Create a backup snapshot of a thread and its rollout file before migrating."""
    os.makedirs(backup_root, exist_ok=True)
    timestamp_str = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(backup_root, f"{timestamp_str}_{source_thread_id}")
    os.makedirs(backup_dir, exist_ok=True)

    backup_meta_file = os.path.join(backup_dir, "meta.json")
    backup_rollout_file = os.path.join(backup_dir, "source_rollout.jsonl.bak")
    shutil.copy2(source_rollout, backup_rollout_file)

    meta_content = {
        "source_thread_id": source_thread_id,
        "thread_data": thread_data,
        "timestamp": timestamp_str,
        "target_provider": target_provider
    }
    with open(backup_meta_file, "w", encoding="utf-8") as f:
        json.dump(meta_content, f, ensure_ascii=False, indent=2)

    return backup_dir, backup_meta_file

def rollback_session_backup(target_id, codex_home):
    """Roll back and undo a migrated session from backup snapshot."""
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.backup_root):
        print("ERROR: backup-sessions directory not found.")
        return False

    dirs = sorted(os.listdir(paths.backup_root), reverse=True)
    if not dirs:
        print("ERROR: No backup snapshots found.")
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
        print(f"ERROR: No matching backup found for ID: {target_id}")
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
        print(f"[*] Deleted thread [{created_tid}] from database.")

    if created_rollout and os.path.exists(created_rollout):
        try:
            os.remove(created_rollout)
            print(f"[*] Deleted rollout file: {created_rollout}")
        except Exception as e:
            print(f"[!] Could not delete file: {e}")

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

    if created_tid:
        try:
            from .mapping import remove_pair
            remove_pair(paths.mapping_db, created_tid)
        except Exception:
            pass

    print(f"SUCCESS: Successfully rolled back session [{created_tid}] from backup: {selected_dir}")
    return True

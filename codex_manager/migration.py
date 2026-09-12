import os
import time
import uuid
import json
import sqlite3
from .config import CodexPaths, normalize_path
from .db import get_connection
from .backup import create_session_backup, rollback_session_backup
from .rollout import extract_turns_from_sqlite, extract_turns_from_rollout, build_migrated_rollout_file
from .projection import build_thread_projection
from .mapping import register_pair

def migrate_thread(source_thread_id, target_provider, codex_home):
    """
    Orchestrate migration/cloning of a thread to a target provider (e.g. DeepSeek):
    1. Validate source thread and file existence.
    2. Snapshot backup of source thread and rollout.
    3. Extract conversation turns (preserving faithful structure).
    4. Write target rollout JSONL (inheriting genuine source metadata without fake prompts).
    5. Build thread projection cache in thread_history_1.sqlite.
    6. Insert new thread record in state_5.sqlite.
    7. Register in local_thread_catalog and session_manager.sqlite.
    """
    paths = CodexPaths(codex_home)
    conn = get_connection(paths.state_db, timeout=15.0)
    cursor = conn.cursor()

    out_rollout = None
    new_thread_id = None

    try:
        cursor.execute("SELECT * FROM threads WHERE id = ?", (source_thread_id,))
        row = cursor.fetchone()
        if not row:
            print(f"ERROR: Thread ID '{source_thread_id}' does not exist in database.")
            conn.close()
            return False

        cols = [d[0] for d in cursor.description]
        thread_data = dict(zip(cols, row))
        source_rollout = normalize_path(thread_data['rollout_path'])

        if not os.path.exists(source_rollout):
            print(f"ERROR: Rollout file does not exist: {source_rollout}")
            conn.close()
            return False

        # 1. Create snapshot backup
        backup_dir, backup_meta_file = create_session_backup(
            source_thread_id, thread_data, source_rollout, target_provider, paths.backup_root
        )

        # 2. Metadata for new thread
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

        clean_cwd = normalize_path(thread_data.get('cwd') or os.path.expanduser("~"))

        # 3. Extract turns
        turns = extract_turns_from_sqlite(paths.th_db, source_thread_id, paths.codex_home)
        if not turns:
            turns = extract_turns_from_rollout(source_rollout)

        # 4. Build rollout file (inheriting genuine source metadata)
        build_migrated_rollout_file(
            source_rollout=source_rollout,
            new_thread_id=new_thread_id,
            new_model=new_model,
            target_provider=target_provider,
            clean_cwd=clean_cwd,
            turns=turns,
            out_rollout_path=out_rollout
        )

        # 5. Populate projection cache in thread_history_1.sqlite
        build_thread_projection(out_rollout, new_thread_id, paths.th_db)

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

        # 8. Register in dedicated session_manager.sqlite mapping DB
        try:
            pair_name = old_name or clean_title.replace("[DS] ", "").strip()
            register_pair(paths.mapping_db, pair_name, source_thread_id, new_thread_id)
        except Exception:
            pass

        # 9. Update backup metadata
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
    """Roll back and undo a migrated session from backup snapshot."""
    return rollback_session_backup(target_id, codex_home)

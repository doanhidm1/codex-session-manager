import json
import os
import re
import time
import uuid

from .backup import create_session_backup, rollback_session_backup
from .config import CodexPaths, normalize_path
from .db import get_connection
from .extractor import extract_turns_from_rollout, extract_turns_from_sqlite
from .mapping import register_migrated_session
from .projection import build_thread_projection
from .provider import (
    SUPPORTED_PROVIDERS,
    get_last_provider_settings,
    is_supported_provider,
    warn_if_deepseek_unconfigured,
)
from .rollout import build_migrated_rollout_file


def migrate_thread(source_thread_id, target_provider, codex_home):
    """
    Safely clone an existing conversation thread into a new provider session:
    - Validates supported provider (deepseek/openai).
    - Checks if DeepSeek is configured in config.toml.
    - Preserves model settings and reasoning effort from previous selections.
    - Generates full backup snapshot before mutating database.
    - Builds wire-compliant rollout JSONL.
    - Projects byte offsets into thread_history_1.sqlite.
    - Inserts new row into state_5.sqlite and records mapping pair in session_manager.sqlite.
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.state_db):
        print(f"ERROR: Database does not exist: {paths.state_db}")
        return False

    conn = get_connection(paths.state_db, timeout=15.0)
    cursor = conn.cursor()
    out_rollout = None

    try:
        cursor.execute("SELECT * FROM threads WHERE id = ?", (source_thread_id,))
        row = cursor.fetchone()
        if not row:
            print(f"ERROR: Thread ID '{source_thread_id}' does not exist in database.")
            conn.close()
            return False

        cols = [d[0] for d in cursor.description]
        thread_data = dict(zip(cols, row))
        source_rollout = normalize_path(thread_data["rollout_path"])
        source_prov = (thread_data.get("model_provider") or "openai").lower().strip()

        if not target_provider:
            target_provider = "openai" if source_prov == "deepseek" else "deepseek"
        else:
            target_provider = target_provider.lower().strip()
            if not is_supported_provider(target_provider):
                print(
                    f"ERROR: Target provider '{target_provider}' is not supported. Currently supported: {', '.join(SUPPORTED_PROVIDERS)}."
                )
                conn.close()
                return False
            if target_provider == source_prov:
                target_provider = "openai" if source_prov == "deepseek" else "deepseek"
                print(
                    f"[*] Note: Source thread is already using [{source_prov.upper()}]. Converting to opposite provider: [{target_provider.upper()}]."
                )

        if target_provider == "deepseek":
            warn_if_deepseek_unconfigured(codex_home)

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

        old_name = thread_data.get("name") or ""
        old_title = thread_data.get("title") or "New Chat"

        target_model, target_effort = get_last_provider_settings(paths.mapping_db, target_provider)
        new_model = target_model

        clean_title = re.sub(r"^\[(DS|OAI)\]\s*", "", old_title)
        if target_provider == "deepseek":
            clean_name = (
                f"{old_name.replace(' (ds)', '').replace('(ds)', '').strip()} (ds)" if old_name else "Chat (ds)"
            )
            new_name = clean_name
            new_title = f"[DS] {clean_title}"
        else:
            clean_name = (
                old_name.replace(" (ds)", "").replace("(ds)", "").strip() if old_name else (old_title or "Chat")
            )
            new_name = clean_name
            new_title = clean_title
            if source_prov == "deepseek":
                cursor.execute(
                    "UPDATE threads SET name = ?, title = ? WHERE id = ?",
                    (f"{clean_name} (ds)", f"[DS] {clean_title}", source_thread_id),
                )

        date_parts = time.strftime("%Y/%m/%d").split("/")
        out_dir = os.path.join(paths.sessions_dir, *date_parts)
        os.makedirs(out_dir, exist_ok=True)
        time_fn = time.strftime("%Y-%m-%dT%H-%M-%S")
        out_filename = f"rollout-{time_fn}-{new_thread_id}.jsonl"
        out_rollout = os.path.join(out_dir, out_filename)
        clean_cwd = normalize_path(thread_data.get("cwd") or os.path.expanduser("~"))

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
            out_rollout_path=out_rollout,
        )

        # 5. Populate projection cache in thread_history_1.sqlite
        build_thread_projection(out_rollout, new_thread_id, paths.th_db)

        # 6. Insert new thread record in state_5.sqlite
        new_thread = dict(thread_data)
        new_thread["id"] = new_thread_id
        new_thread["name"] = new_name
        new_thread["title"] = new_title
        new_thread["rollout_path"] = out_rollout
        new_thread["model_provider"] = target_provider
        new_thread["model"] = new_model
        if target_effort:
            new_thread["reasoning_effort"] = thread_data.get("reasoning_effort") or target_effort
        new_thread["created_at"] = now_ts
        new_thread["updated_at"] = now_ts
        new_thread["created_at_ms"] = now_ms
        new_thread["updated_at_ms"] = now_ms
        new_thread["recency_at"] = now_ts
        new_thread["recency_at_ms"] = now_ms

        insert_cols = list(new_thread.keys())
        placeholders = ",".join(["?"] * len(insert_cols))
        sql = f"INSERT INTO threads ({','.join(insert_cols)}) VALUES ({placeholders})"
        cursor.execute(sql, [new_thread[c] for c in insert_cols])
        conn.commit()
        conn.close()

        # 7. Register in catalog & mapping DB
        register_migrated_session(
            paths, new_thread_id, new_name, clean_cwd, target_provider, old_name, clean_title, source_thread_id, now_ts
        )

        # 8. Update backup metadata
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
            try:
                os.remove(out_rollout)
            except Exception:
                pass
        print(f"ERROR: {e}")
        return False


def rollback_thread(target_id, codex_home):
    """Roll back and undo a migrated session from backup snapshot."""
    return rollback_session_backup(target_id, codex_home)

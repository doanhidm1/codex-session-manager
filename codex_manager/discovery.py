import os
import sqlite3
from .config import CodexPaths
from .db import get_connection

def discover_and_pair_unmapped_threads(codex_home):
    """
    Find any newly created active threads in state_5.sqlite that do not yet have
    a counterpart in session_manager.sqlite, auto-clone the twin session,
    and register the new pair.

    Ensures that when a user starts a brand new chat directly in DeepSeek or OpenAI,
    running sync or switch will seamlessly auto-pair and sync without manual intervention.
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.state_db) or not os.path.exists(paths.mapping_db):
        return []

    from .mapping import get_all_pairs
    pairs = get_all_pairs(paths.mapping_db, active_only=False)
    known_ids = set()
    for p in pairs:
        known_ids.add(p[2])  # openai_thread_id
        known_ids.add(p[3])  # deepseek_thread_id

    conn = get_connection(paths.state_db, timeout=5.0)
    cur = conn.cursor()
    unmapped = []
    try:
        rows = cur.execute(
            "SELECT id, name, title, model_provider FROM threads "
            "WHERE archived = 0 ORDER BY updated_at ASC"
        ).fetchall()
        for r in rows:
            tid, name, title, prov = r
            prov_norm = (prov or 'openai').lower().strip()
            if tid not in known_ids and prov_norm in ('openai', 'deepseek'):
                unmapped.append((tid, name, title, prov_norm))
    except Exception:
        pass
    finally:
        conn.close()

    if not unmapped:
        return []

    from .migration import migrate_thread
    new_paired = []
    for tid, name, title, prov in unmapped:
        target_prov = 'openai' if prov == 'deepseek' else 'deepseek'
        display_name = name or title or tid[:8]
        print(f"\n[*] Discovered new un-paired [{prov.upper()}] session: '{display_name}'")
        print(f"    Auto-creating [{target_prov.upper()}] counterpart and registering pair...")
        ok = migrate_thread(tid, target_prov, codex_home)
        if ok:
            new_paired.append(tid)

    return new_paired

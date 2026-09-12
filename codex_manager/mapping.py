import sqlite3
import os
import time
import uuid
from .config import CodexPaths, normalize_path

def init_mapping_db(mapping_db_path):
    """Ensure session_manager.sqlite exists and has proper schema."""
    conn = sqlite3.connect(normalize_path(mapping_db_path), timeout=10.0)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS session_pairs (
            pair_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            openai_thread_id TEXT NOT NULL UNIQUE,
            deepseek_thread_id TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            last_synced_at INTEGER,
            is_active INTEGER DEFAULT 1
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS manager_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_all_pairs(mapping_db_path, active_only=True):
    """Retrieve all registered pairs from session_manager.sqlite."""
    init_mapping_db(mapping_db_path)
    conn = sqlite3.connect(normalize_path(mapping_db_path), timeout=5.0)
    cur = conn.cursor()
    query = "SELECT pair_id, name, openai_thread_id, deepseek_thread_id, created_at, last_synced_at, is_active FROM session_pairs"
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY name ASC"
    rows = cur.execute(query).fetchall()
    conn.close()
    return rows

def register_pair(mapping_db_path, name, openai_id, ds_id):
    """Register or update an explicit pair mapping."""
    init_mapping_db(mapping_db_path)
    conn = sqlite3.connect(normalize_path(mapping_db_path), timeout=10.0)
    cur = conn.cursor()
    pair_id = str(uuid.uuid4())
    now_ts = int(time.time())
    cur.execute("""
        INSERT INTO session_pairs (pair_id, name, openai_thread_id, deepseek_thread_id, created_at, is_active)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(openai_thread_id) DO UPDATE SET
            name = excluded.name,
            deepseek_thread_id = excluded.deepseek_thread_id,
            is_active = 1
    """, (pair_id, name, openai_id, ds_id, now_ts))
    conn.commit()
    conn.close()
    return True

def remove_pair(mapping_db_path, identifier):
    """Deactivate or remove a pair by name or pair_id."""
    init_mapping_db(mapping_db_path)
    conn = sqlite3.connect(normalize_path(mapping_db_path), timeout=10.0)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM session_pairs WHERE pair_id = ? OR LOWER(name) = LOWER(?) OR openai_thread_id = ? OR deepseek_thread_id = ?",
        (identifier, identifier, identifier, identifier)
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted > 0

def update_last_synced(mapping_db_path, pair_id, timestamp=None):
    """Update last_synced_at timestamp for a pair."""
    ts = timestamp or int(time.time())
    conn = sqlite3.connect(normalize_path(mapping_db_path), timeout=5.0)
    cur = conn.cursor()
    cur.execute("UPDATE session_pairs SET last_synced_at = ? WHERE pair_id = ?", (ts, pair_id))
    conn.commit()
    conn.close()

def auto_seed_existing_pairs(codex_home):
    """
    If session_pairs table is empty, auto-detect existing (ds) twins and seed them.
    Ensures zero manual configuration needed for existing setups.
    """
    paths = CodexPaths(codex_home)
    init_mapping_db(paths.mapping_db)

    existing = get_all_pairs(paths.mapping_db, active_only=False)
    if existing:
        return existing

    if not os.path.exists(paths.state_db):
        return []

    from .db import get_paired_threads
    discovered = get_paired_threads(paths.state_db)
    for orig_t, ds_t in discovered:
        name = orig_t[1] or "Session"
        orig_id = orig_t[0]
        ds_id = ds_t[0]
        register_pair(paths.mapping_db, name, orig_id, ds_id)

    return get_all_pairs(paths.mapping_db, active_only=True)

def list_pairs_table(codex_home):
    """Print a clean table of all mapped pairs with their live status in state_5.sqlite."""
    paths = CodexPaths(codex_home)
    auto_seed_existing_pairs(codex_home)
    pairs = get_all_pairs(paths.mapping_db, active_only=False)

    if not pairs:
        print("[i] No session pairs registered in the mapping database.")
        return

    # Check live status from state_5.sqlite
    status_map = {}
    if os.path.exists(paths.state_db):
        conn = sqlite3.connect(paths.state_db, timeout=5.0)
        cur = conn.cursor()
        for r in cur.execute("SELECT id, archived FROM threads").fetchall():
            status_map[r[0]] = "Archived" if r[1] == 1 else "Active"
        conn.close()

    header = f"{'#':<3} | {'Project Name':<16} | {'OpenAI ID':<36} ({'PC Status':<8}) | {'DeepSeek ID':<36} ({'PC Status':<8}) | {'Last Synced'}"
    print(header)
    print('-' * len(header))
    for idx, p in enumerate(pairs, 1):
        pid, name, o_id, d_id, cat, lsync, is_act = p
        o_stat = status_map.get(o_id, "Unknown")
        d_stat = status_map.get(d_id, "Unknown")
        lsync_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lsync)) if lsync else "Never"
        print(f"{idx:<3} | {name:<16} | {o_id} ({o_stat:<8}) | {d_id} ({d_stat:<8}) | {lsync_str}")

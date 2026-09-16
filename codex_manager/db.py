import os
import sqlite3

from .config import normalize_path


def get_connection(db_path, timeout=10.0):
    return sqlite3.connect(normalize_path(db_path), timeout=timeout)


def list_threads(state_db, limit=15):
    """Print a clean, formatted table of recent threads."""
    if not os.path.exists(state_db):
        print(f"ERROR: Database does not exist: {state_db}")
        return

    conn = get_connection(state_db)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, model_provider, model, name, title, updated_at, archived "
        "FROM threads ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    header = (
        f"{'#':<3} | {'Provider':<8} | {'Model':<14} | {'Name':<16} | {'Status':<8} | {'Thread ID':<36} | {'Title'}"
    )
    print(header)
    print("-" * len(header))
    for idx, r in enumerate(rows, 1):
        tid, prov, mdl, name, title, ut, arch = r
        nm = (name or "").strip()[:14]
        status = "Archived" if arch == 1 else "Active"
        t_clean = (title or "Untitled").split("\n")[0][:30]
        print(
            f"{idx:<3} | {prov or 'unknown':<8} | {mdl or 'unknown':<14} | {nm:<16} | {status:<8} | {tid} | {t_clean}"
        )


def resolve_thread(identifier, codex_home):
    """
    Resolve a thread record by UUID, exact name, or partial keyword match.
    Prioritizes latest updated threads.
    """
    from .config import CodexPaths

    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.state_db):
        return None

    conn = get_connection(paths.state_db, timeout=5.0)
    cur = conn.cursor()

    # 1. Direct UUID match
    cur.execute("SELECT id, name, title, model_provider, rollout_path FROM threads WHERE id = ?", (identifier,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row

    # 2. Exact match on name (latest updated)
    cur.execute(
        "SELECT id, name, title, model_provider, rollout_path "
        "FROM threads WHERE LOWER(name) = LOWER(?) ORDER BY updated_at DESC",
        (identifier,),
    )
    rows = cur.fetchall()
    if rows:
        conn.close()
        return rows[0]

    # 3. Partial match on name
    cur.execute(
        "SELECT id, name, title, model_provider, rollout_path "
        "FROM threads WHERE LOWER(name) LIKE LOWER(?) ORDER BY updated_at DESC",
        (f"%{identifier}%",),
    )
    rows = cur.fetchall()
    if rows:
        conn.close()
        return rows[0]

    # 4. Partial match on title
    cur.execute(
        "SELECT id, name, title, model_provider, rollout_path "
        "FROM threads WHERE LOWER(title) LIKE LOWER(?) ORDER BY updated_at DESC",
        (f"%{identifier}%",),
    )
    rows = cur.fetchall()
    conn.close()
    if rows:
        return rows[0]

    return None


def get_paired_threads(state_db):
    """
    Identify and return all (original_thread, ds_twin_thread) tuples.
    Sorted by latest activity.
    """
    if not os.path.exists(state_db):
        return []

    conn = get_connection(state_db, timeout=5.0)
    cur = conn.cursor()
    rows = cur.execute("SELECT id, name, title, model_provider, archived, updated_at FROM threads").fetchall()
    conn.close()

    names_map = {}
    for r in rows:
        tid, nm, title, prov, arch, ut = r
        if nm and nm.strip():
            names_map.setdefault(nm.strip().lower(), []).append(r)

    for k in names_map:
        names_map[k].sort(key=lambda x: x[5] or 0, reverse=True)

    pairs = []
    for nm, tlist in names_map.items():
        if nm.endswith(" (ds)"):
            base_nm = nm[:-5].strip()
            if base_nm in names_map:
                latest_orig = names_map[base_nm][0]
                latest_ds = tlist[0]
                pairs.append((latest_orig, latest_ds))

    return pairs

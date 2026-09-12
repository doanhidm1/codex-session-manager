import sqlite3
import os
from .config import CodexPaths
from .db import get_connection, get_paired_threads

def focus_mode(target_mode, codex_home):
    """
    Toggle sidebar visibility between OpenAI and DeepSeek sessions.
    - 'deepseek': archives original OpenAI sessions (hides them on PC).
    - 'openai': archives (ds) sessions (hides them on PC).
    - 'all' / 'show': unarchives all sessions.
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.state_db):
        print(f"ERROR: Database không tồn tại: {paths.state_db}")
        return False

    pairs = get_paired_threads(paths.state_db)
    if not pairs:
        print("[i] Không tìm thấy cặp session nào để quản lý Focus Mode.")
        return True

    orig_ids = {p[0][0] for p in pairs}
    ds_ids = {p[1][0] for p in pairs}

    conn = get_connection(paths.state_db, timeout=10.0)
    cur = conn.cursor()

    mode = target_mode.lower().strip()
    updated = 0

    if mode == 'deepseek':
        for tid in orig_ids:
            cur.execute("UPDATE threads SET archived = 1 WHERE id = ? AND archived = 0", (tid,))
            updated += cur.rowcount
        for tid in ds_ids:
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (tid,))
            updated += cur.rowcount
    elif mode == 'openai':
        for tid in ds_ids:
            cur.execute("UPDATE threads SET archived = 1 WHERE id = ? AND archived = 0", (tid,))
            updated += cur.rowcount
        for tid in orig_ids:
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (tid,))
            updated += cur.rowcount
    elif mode in ('all', 'show'):
        for tid in orig_ids.union(ds_ids):
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (tid,))
            updated += cur.rowcount

    conn.commit()
    conn.close()

    print(f"[*] Focus mode '{mode}': Đã cập nhật trạng thái {updated} threads.")
    print(f"[*] Các cặp session được quản lý ({len(pairs)} cặp):")
    for orig_t, ds_t in pairs:
        print(f"    - [{orig_t[1] or orig_t[0]}] (Gốc OpenAI) <---> [{ds_t[1] or ds_t[0]}] (DeepSeek)")

    if mode == 'deepseek':
        print("[+] TRÊN PC: Đã ẩn (archive) các session gốc OpenAI để tránh chat nhầm!")
        print("[+] Thanh bên PC và Mobile hiện tại CHỈ THẤY các bản DeepSeek (ds).")
    elif mode == 'openai':
        print("[+] TRÊN PC: Đã mở lại các session gốc OpenAI và ẩn các bản DeepSeek (ds).")
    elif mode in ('all', 'show'):
        print("[+] TRÊN PC: Đã hiển thị đầy đủ cả 2 phiên bản.")

    return True

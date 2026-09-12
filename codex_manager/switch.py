import sqlite3
import os
from .config import CodexPaths
from .db import get_connection
from .mapping import auto_seed_existing_pairs, get_all_pairs, update_last_synced
from .sync import sync_threads

def switch_provider(target_mode, codex_home, auto_sync=True):
    """
    Switch active provider on PC and Mobile:
    1. Automatically synchronizes new conversational turns between paired sessions.
    2. Toggles sidebar visibility by updating 'archived' state in state_5.sqlite ONLY for registered pairs.
    3. Guarantees that any manually archived sessions outside the pairs are NEVER touched.
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.state_db):
        print(f"ERROR: Database không tồn tại: {paths.state_db}")
        return False

    auto_seed_existing_pairs(codex_home)
    pairs = get_all_pairs(paths.mapping_db, active_only=True)

    if not pairs:
        print("[i] Không tìm thấy cặp session nào trong mapping database.")
        return True

    mode = target_mode.lower().strip()
    if mode not in ('deepseek', 'openai', 'all', 'show'):
        print(f"ERROR: Provider không hợp lệ: '{mode}'. Vui lòng chọn 'deepseek', 'openai', hoặc 'all'.")
        return False

    # 1. Automatic Sync before switching
    if auto_sync:
        print(f"[*] Đang tự động đồng bộ tin nhắn mới trước khi chuyển sang chế độ [{mode.upper()}]...")
        for p in pairs:
            pair_id, name, o_id, d_id, cat, lsync, is_act = p
            if mode == 'deepseek':
                # Sync PC OpenAI -> DeepSeek so mobile has all latest context
                sync_threads(o_id, d_id, codex_home)
            elif mode == 'openai':
                # Sync DeepSeek (mobile) -> PC OpenAI so PC has all latest turns
                sync_threads(d_id, o_id, codex_home)
            elif mode in ('all', 'show'):
                sync_threads(o_id, d_id, codex_home)
                sync_threads(d_id, o_id, codex_home)
            update_last_synced(paths.mapping_db, pair_id)

    # 2. Update archived status in state_5.sqlite ONLY for registered IDs
    conn = get_connection(paths.state_db, timeout=10.0)
    cur = conn.cursor()
    updated = 0

    for p in pairs:
        pair_id, name, o_id, d_id, cat, lsync, is_act = p
        if mode == 'deepseek':
            # Hide OpenAI original, Show DeepSeek
            cur.execute("UPDATE threads SET archived = 1 WHERE id = ? AND archived = 0", (o_id,))
            updated += cur.rowcount
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (d_id,))
            updated += cur.rowcount
        elif mode == 'openai':
            # Hide DeepSeek, Show OpenAI original
            cur.execute("UPDATE threads SET archived = 1 WHERE id = ? AND archived = 0", (d_id,))
            updated += cur.rowcount
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (o_id,))
            updated += cur.rowcount
        elif mode in ('all', 'show'):
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (o_id,))
            updated += cur.rowcount
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (d_id,))
            updated += cur.rowcount

    conn.commit()
    conn.close()

    # 3. Save active provider in mapping settings
    try:
        m_conn = sqlite3.connect(paths.mapping_db, timeout=5.0)
        m_cur = m_conn.cursor()
        m_cur.execute(
            "INSERT OR REPLACE INTO manager_settings (key, value) VALUES ('active_provider', ?)",
            (mode,)
        )
        m_conn.commit()
        m_conn.close()
    except Exception:
        pass

    print(f"\n[+] Đã chuyển thành công sang chế độ [{mode.upper()}]!")
    print(f"[*] Quản lý chính xác {len(pairs)} cặp session từ mapping database:")
    for p in pairs:
        pair_id, name, o_id, d_id, cat, lsync, is_act = p
        print(f"    - [{name}] (OpenAI: {o_id[:8]} <---> DeepSeek: {d_id[:8]})")

    if mode == 'deepseek':
        print("[+] Trạng thái: Các session gốc OpenAI đã được ẩn an toàn. PC & Mobile CHỈ HIỆN các bản DeepSeek (ds) -> Tránh hoàn toàn việc chat nhầm!")
    elif mode == 'openai':
        print("[+] Trạng thái: Các bản DeepSeek (ds) đã được ẩn. Các session gốc OpenAI hiển thị bình thường trên PC.")
    elif mode in ('all', 'show'):
        print("[+] Trạng thái: Đang hiển thị đầy đủ tất cả các session.")

    return True

# Backward compatibility alias
def focus_mode(target_mode, codex_home):
    return switch_provider(target_mode, codex_home, auto_sync=True)

import sqlite3
import os
from .config import CodexPaths
from .db import get_connection
from .mapping import auto_seed_existing_pairs, get_all_pairs, update_last_synced
from .sync import sync_threads

from .provider import (
    is_supported_provider,
    warn_if_deepseek_unconfigured,
    switch_provider_settings,
    SUPPORTED_PROVIDERS
)

def switch_provider(target_mode, codex_home, auto_sync=True):
    """
    Switch active provider on PC and Mobile:
    1. Validates supported provider (OpenAI and DeepSeek).
    2. Warns if DeepSeek API is not yet configured in config.toml.
    3. Preserves current model settings & restores target provider's last used settings.
    4. Automatically synchronizes new conversational turns between paired sessions.
    5. Toggles sidebar visibility by updating 'archived' state in state_5.sqlite ONLY for registered pairs.
    6. Guarantees that any manually archived sessions outside the pairs are NEVER touched.
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.state_db):
        print(f"ERROR: Database does not exist: {paths.state_db}")
        return False

    mode = target_mode.lower().strip()
    if mode not in ('deepseek', 'openai', 'all', 'show'):
        print(f"ERROR: Provider '{mode}' is not supported. Currently, only 'deepseek' and 'openai' (or 'all') are supported.")
        return False

    # Check DeepSeek configuration if switching to DeepSeek
    if mode == 'deepseek':
        warn_if_deepseek_unconfigured(codex_home)

    # 0. Preserve outgoing model settings & restore incoming model settings in config.toml
    if mode in ('deepseek', 'openai'):
        restored_model, restored_effort = switch_provider_settings(codex_home, mode)
        print(f"[*] Configuration updated in config.toml: provider='{mode}', model='{restored_model}', reasoning_effort='{restored_effort}'")

    from .discovery import discover_and_pair_unmapped_threads
    auto_seed_existing_pairs(codex_home)
    discover_and_pair_unmapped_threads(codex_home)
    pairs = get_all_pairs(paths.mapping_db, active_only=True)

    if not pairs:
        print("[i] No session pairs found in mapping database.")
        return True

    # 1. Automatic Sync before switching
    if auto_sync:
        print(f"[*] Automatically syncing new messages before switching to [{mode.upper()}] mode...")
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

    print(f"\n[+] Successfully switched to [{mode.upper()}] mode!")
    print(f"[*] Managing {len(pairs)} session pair(s) from mapping database:")
    for p in pairs:
        pair_id, name, o_id, d_id, cat, lsync, is_act = p
        print(f"    - [{name}] (OpenAI: {o_id[:8]} <---> DeepSeek: {d_id[:8]})")

    if mode == 'deepseek':
        print("[+] Status: Original OpenAI sessions safely hidden on PC. PC and Mobile will ONLY display DeepSeek (ds) versions to prevent accidental chats.")
    elif mode == 'openai':
        print("[+] Status: DeepSeek (ds) sessions hidden on PC. Original OpenAI sessions are visible normally.")
    elif mode in ('all', 'show'):
        print("[+] Status: All sessions are now visible.")

    return True

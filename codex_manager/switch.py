import os
import re
import sqlite3

from .config import CodexPaths
from .db import get_connection
from .mapping import auto_seed_existing_pairs, get_all_pairs, update_last_synced
from .provider import (
    SUPPORTED_PROVIDERS,
    detect_current_provider_settings,
    get_last_provider_settings,
    save_provider_settings,
    warn_if_deepseek_unconfigured,
)
from .sync import sync_threads
from .toml_utils import update_config_toml


def switch_provider_settings(codex_home, target_provider):
    """Saves outgoing provider settings and restores target provider settings in config.toml."""
    paths = CodexPaths(codex_home)
    target_prov = target_provider.lower().strip()

    outgoing_prov = "openai"
    if os.path.exists(paths.config_toml):
        try:
            with open(paths.config_toml, "r", encoding="utf-8") as f:
                c = f.read()
            m = re.search(r'(?m)^model_provider\s*=\s*"([^"]+)"', c)
            if m:
                outgoing_prov = m.group(1).lower().strip()
        except Exception:
            pass

    if outgoing_prov in SUPPORTED_PROVIDERS and outgoing_prov != target_prov:
        out_model, out_effort = detect_current_provider_settings(codex_home, outgoing_prov)
        save_provider_settings(paths.mapping_db, outgoing_prov, out_model, out_effort)

    target_model, target_effort = get_last_provider_settings(paths.mapping_db, target_prov)
    if not target_model:
        target_model, target_effort = detect_current_provider_settings(codex_home, target_prov)

    if target_prov == "deepseek" and target_effort not in ("low", "high", "max"):
        target_effort = "high"

    updates = {
        "model_provider": target_prov,
        "model": target_model,
        "model_reasoning_effort": target_effort,
        "model_context_window": 1000000,
        "model_auto_compact_token_limit": 900000,
    }
    if target_prov == "openai":
        updates["model_catalog_json"] = None
    elif target_prov == "deepseek":
        updates["model_catalog_json"] = os.path.join(paths.codex_home, "models.json").replace("\\", "/")

    update_config_toml(codex_home, updates)
    return target_model, target_effort


def switch_provider(target_mode, codex_home, auto_sync=True, force=False):
    """
    Switch active provider on PC and Mobile:
    1. Detects and halts if any Codex session is currently running.
    2. Validates supported provider (OpenAI and DeepSeek).
    3. Preserves current model settings & restores target provider's last used settings.
    4. Automatically synchronizes new conversational turns between paired sessions.
    5. Toggles sidebar visibility by updating 'archived' state in state_5.sqlite ONLY for registered pairs.
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.state_db):
        print(f"ERROR: Database does not exist: {paths.state_db}")
        return False

    from .activity import assert_no_running_sessions

    if not force and not assert_no_running_sessions(codex_home, force=force):
        return False

    mode = target_mode.lower().strip()
    if mode not in ("deepseek", "openai", "all", "show"):
        print(
            f"ERROR: Provider '{mode}' is not supported. Currently, only 'deepseek' and 'openai' (or 'all') are supported."
        )
        return False

    # Manage DeepSeek proxy lifecycle based on target mode
    if mode == "deepseek":
        warn_if_deepseek_unconfigured(codex_home)
        from .proxy.daemon import is_proxy_running, start_proxy_daemon

        if not is_proxy_running():
            print("[*] Starting DeepSeek Reverse Proxy daemon on port 8765...")
            if start_proxy_daemon():
                print("[+] DeepSeek Reverse Proxy is RUNNING on http://127.0.0.1:8765")
            else:
                print("[-] WARNING: Failed to start DeepSeek Reverse Proxy daemon! Codex may fail to connect.")
        else:
            print("[+] DeepSeek Reverse Proxy is already RUNNING on port 8765.")

        from .proxy.reconciler import reconcile_delegation_turns

        reconciled = reconcile_delegation_turns()
        if reconciled > 0:
            print(f"[*] Reconciled {reconciled} cross-session delegation turn(s) for read_thread compatibility.")
    elif mode == "openai":
        from .proxy.daemon import is_proxy_running, stop_proxy_daemon

        if is_proxy_running():
            print("[*] Stopping DeepSeek Reverse Proxy daemon (not needed in OpenAI mode)...")
            if stop_proxy_daemon():
                print("[+] DeepSeek Reverse Proxy stopped.")
            else:
                print("[-] WARNING: Failed to gracefully stop DeepSeek Reverse Proxy daemon.")

    # 0. Preserve outgoing model settings & restore incoming model settings in config.toml
    if mode in ("deepseek", "openai"):
        restored_model, restored_effort = switch_provider_settings(codex_home, mode)
        print(
            f"[*] Configuration updated in config.toml: provider='{mode}', model='{restored_model}', reasoning_effort='{restored_effort}'"
        )
        from .reasoning import ensure_reasoning_efforts

        ensure_reasoning_efforts(codex_home, verbose=True)

    from .discovery import discover_and_pair_unmapped_threads

    auto_seed_existing_pairs(codex_home)
    outgoing = "deepseek" if mode == "openai" else ("openai" if mode == "deepseek" else None)
    discover_and_pair_unmapped_threads(codex_home, source_provider_filter=outgoing)
    pairs = get_all_pairs(paths.mapping_db, active_only=True)

    if not pairs:
        print("[i] No session pairs found in mapping database.")
        return True

    # 1. Automatic Sync before switching
    if auto_sync:
        print(f"[*] Automatically syncing new messages before switching to [{mode.upper()}] mode...")
        for p in pairs:
            pair_id, name, o_id, d_id, cat, lsync, is_act = p
            if mode == "deepseek":
                sync_threads(o_id, d_id, codex_home, force=force)
            elif mode == "openai":
                sync_threads(d_id, o_id, codex_home, force=force)
            elif mode in ("all", "show"):
                sync_threads(o_id, d_id, codex_home, force=force)
                sync_threads(d_id, o_id, codex_home, force=force)
            update_last_synced(paths.mapping_db, pair_id)

    # 1.5. Auto-split active pairs if configured threshold in config.toml is exceeded
    from .split import check_and_auto_split

    check_and_auto_split(codex_home)

    # 2. Update archived status & sync names in state_5.sqlite and local_thread_catalog
    conn = get_connection(paths.state_db, timeout=10.0)
    cur = conn.cursor()
    cat_conn = sqlite3.connect(paths.cat_db, timeout=5.0) if os.path.exists(paths.cat_db) else None

    for p in pairs:
        _, name, o_id, d_id, _, _, _ = p
        clean = re.sub(r"\s*\(ds\)$", "", name).strip()
        ds_name = f"{clean} (ds)"
        cur.execute("UPDATE threads SET name = ? WHERE id = ? AND (name IS NULL OR name != ?)", (clean, o_id, clean))
        cur.execute(
            "UPDATE threads SET name = ? WHERE id = ? AND (name IS NULL OR name != ?)", (ds_name, d_id, ds_name)
        )

        # Inspect pinned status from both pair members
        cur.execute("SELECT thread_section_id, section_position, is_pinned FROM threads WHERE id = ?", (o_id,))
        o_sec = cur.fetchone()
        cur.execute("SELECT thread_section_id, section_position, is_pinned FROM threads WHERE id = ?", (d_id,))
        d_sec = cur.fetchone()

        sec_id = (o_sec[0] if o_sec and o_sec[0] else None) or (d_sec[0] if d_sec and d_sec[0] else None)
        sec_pos = (o_sec[1] if o_sec and o_sec[1] is not None else None) or (
            d_sec[1] if d_sec and d_sec[1] is not None else None
        )
        is_pin = 1 if ((o_sec and o_sec[2]) or (d_sec and d_sec[2])) else 0

        if mode == "deepseek":
            cur.execute("UPDATE threads SET archived = 1 WHERE id = ? AND archived = 0", (o_id,))
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (d_id,))
            if sec_id:
                cur.execute(
                    "UPDATE threads SET thread_section_id = ?, section_position = ?, is_pinned = ? WHERE id = ?",
                    (sec_id, sec_pos, is_pin, d_id),
                )
                cur.execute(
                    "UPDATE threads SET thread_section_id = NULL, section_position = NULL, is_pinned = 0 WHERE id = ?",
                    (o_id,),
                )
        elif mode == "openai":
            cur.execute("UPDATE threads SET archived = 1 WHERE id = ? AND archived = 0", (d_id,))
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (o_id,))
            if sec_id:
                cur.execute(
                    "UPDATE threads SET thread_section_id = ?, section_position = ?, is_pinned = ? WHERE id = ?",
                    (sec_id, sec_pos, is_pin, o_id),
                )
                cur.execute(
                    "UPDATE threads SET thread_section_id = NULL, section_position = NULL, is_pinned = 0 WHERE id = ?",
                    (d_id,),
                )
        elif mode in ("all", "show"):
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (o_id,))
            cur.execute("UPDATE threads SET archived = 0 WHERE id = ? AND archived != 0", (d_id,))

        if cat_conn:
            try:
                cat_conn.execute("UPDATE local_thread_catalog SET display_title = ? WHERE thread_id = ?", (clean, o_id))
                cat_conn.execute(
                    "UPDATE local_thread_catalog SET display_title = ? WHERE thread_id = ?", (ds_name, d_id)
                )
            except Exception:
                pass

    # Ensure any archived threads do not linger in Pinned section
    cur.execute(
        "UPDATE threads SET thread_section_id = NULL, section_position = NULL, is_pinned = 0 WHERE archived = 1 AND thread_section_id IS NOT NULL"
    )

    conn.commit()
    conn.close()
    if cat_conn:
        try:
            cat_conn.commit()
            cat_conn.close()
        except Exception:
            pass

    # 2.5 Update automations target_thread_id if matching any pair
    # Note: Heartbeat automations must remain targeted to OpenAI threads because DeepSeek API rejects synthetic function_call_output
    automations_dir = os.path.join(codex_home, "automations")
    if os.path.isdir(automations_dir):
        for root, _, files in os.walk(automations_dir):
            for f in files:
                if f.endswith(".toml"):
                    fpath = os.path.join(root, f)
                    try:
                        with open(fpath, "r", encoding="utf-8") as af:
                            acontent = af.read()
                        new_content = acontent
                        is_heartbeat = 'kind = "heartbeat"' in acontent or "kind = 'heartbeat'" in acontent
                        for p in pairs:
                            _, _, o_id, d_id, _, _, _ = p
                            if is_heartbeat:
                                # Always route heartbeat automations to OpenAI
                                new_content = re.sub(
                                    rf'(?m)^target_thread_id\s*=\s*"{d_id}"',
                                    f'target_thread_id = "{o_id}"',
                                    new_content,
                                )
                            else:
                                if mode == "deepseek":
                                    new_content = re.sub(
                                        rf'(?m)^target_thread_id\s*=\s*"{o_id}"',
                                        f'target_thread_id = "{d_id}"',
                                        new_content,
                                    )
                                elif mode == "openai":
                                    new_content = re.sub(
                                        rf'(?m)^target_thread_id\s*=\s*"{d_id}"',
                                        f'target_thread_id = "{o_id}"',
                                        new_content,
                                    )
                        if new_content != acontent:
                            with open(fpath, "w", encoding="utf-8") as af:
                                af.write(new_content)
                    except Exception:
                        pass

    # 3. Save active provider in mapping settings
    try:
        m_conn = sqlite3.connect(paths.mapping_db, timeout=5.0)
        m_cur = m_conn.cursor()
        m_cur.execute(
            "INSERT OR REPLACE INTO manager_settings (key, value) VALUES ('active_provider', ?)",
            (mode,),
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

    if mode == "deepseek":
        print(
            "[+] Status: Original OpenAI sessions safely hidden on PC. PC and Mobile will ONLY display DeepSeek (ds) versions to prevent accidental chats."
        )
    elif mode == "openai":
        print("[+] Status: DeepSeek (ds) sessions hidden on PC. Original OpenAI sessions are visible normally.")
    elif mode in ("all", "show"):
        print("[+] Status: All sessions are now visible.")

    return True

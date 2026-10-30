import os
import re
import sqlite3
import time

from .config import CodexPaths
from .db import get_connection
from .mapping import auto_seed_existing_pairs, get_all_pairs, update_last_synced
from .provider import (
    SUPPORTED_PROVIDERS,
    detect_current_provider_settings,
    get_config_web_search,
    get_last_provider_settings,
    get_last_web_search,
    save_provider_settings,
    save_web_search,
    warn_if_deepseek_unconfigured,
)
from .sync import sync_threads
from .toml_utils import set_section_key, update_config_toml

PROXY_BASE_URL = "http://127.0.0.1:8765/"
# DeepSeek bills a cache miss on the whole context whenever a thread is reopened after the cache expired, and the
# average context sits halfway between the post-compaction size (~50k) and this limit. 400k roughly halves that cost
# while keeping the 1M window; OpenAI keeps the previous 900k.
AUTO_COMPACT_TOKEN_LIMIT = {"deepseek": 400000, "openai": 900000}
# Long-running daemons that read config.toml / thread state at start; they must be restarted after a switch
REMOTE_SERVICES = ("codex-rc.service", "codex-session-bridge.service")


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
        # web_search differs per provider (OpenAI hosted tool vs DeepSeek server-side search): persist it too
        save_web_search(paths.mapping_db, outgoing_prov, get_config_web_search(paths.config_toml))

    target_web_search = get_last_web_search(paths.mapping_db, target_prov)
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
        "model_auto_compact_token_limit": AUTO_COMPACT_TOKEN_LIMIT.get(target_prov, 900000),
        "web_search": target_web_search,
        # Left behind by the DeepSeek one-click setup script; they force API-key login and break the OpenAI/ChatGPT mode
        "forced_login_method": None,
        "preferred_auth_method": None,
    }
    if target_prov == "openai":
        updates["model_catalog_json"] = None
        updates["openai_base_url"] = None
    elif target_prov == "deepseek":
        updates["model_catalog_json"] = os.path.join(paths.codex_home, "models.json").replace("\\", "/")
        # The mobile remote client forces model_provider=openai on thread/start and ignores config.toml's provider;
        # pointing the built-in OpenAI provider at the proxy makes those threads reach DeepSeek too
        updates["openai_base_url"] = PROXY_BASE_URL

    update_config_toml(codex_home, updates)
    if target_prov == "deepseek":
        # Codex must talk to the local reverse proxy (adapter + thread-id remap), not api.deepseek.com directly
        set_section_key(codex_home, "model_providers.deepseek", "base_url", PROXY_BASE_URL)
    return target_model, target_effort


def _switch_provider_impl(target_mode, codex_home, auto_sync=True, force=False):
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

    from .repair import heal_thread_items_sources

    heal_thread_items_sources(paths.th_db)

    # 1.2 Carry turn state (interrupted turns) and queued follow-up messages to the session that becomes visible
    if mode in ("deepseek", "openai"):
        queue_db = os.path.join(paths.codex_home, "queue_1.sqlite")
        fixed = moved = 0
        for p in pairs:
            _, _, o_id, d_id, _, _, _ = p
            src, dst = (o_id, d_id) if mode == "deepseek" else (d_id, o_id)
            fixed += sync_turn_statuses(paths.th_db, src, dst)
            moved += move_queued_items(queue_db, src, dst)
        if fixed:
            print(f"[*] Restored {fixed} interrupted turn state(s) on the target sessions.")
        if moved:
            print(f"[*] Moved {moved} queued message(s) to the target sessions.")

    # 1.3 Sync goals across registered pairs
    from .goals import sync_goals

    ok_g, msg_g, _ = sync_goals(codex_home)
    if ok_g:
        print(f"[*] Thread goals synchronized: {msg_g}")

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

    # Unarchiving only flips the flag; move rollouts the desktop parked in archived_sessions back,
    # otherwise later archive/unarchive in Codex fails with "no rollout found for thread id".
    restored = restore_unarchived_rollouts(cur, paths.codex_home)
    if restored:
        print(f"[*] Moved {restored} rollout file(s) back from archived_sessions/ for visible sessions.")

    # Ensure any archived threads do not linger in Pinned section
    cur.execute(
        "UPDATE threads SET thread_section_id = NULL, section_position = NULL, is_pinned = 0 WHERE archived = 1 AND thread_section_id IS NOT NULL"
    )

    conn.commit()
    if cat_conn:
        try:
            hidden = sync_desktop_catalog(cat_conn, cur)
            if hidden:
                print(f"[*] Hid {hidden} archived/deleted session(s) from the desktop Recents catalog.")
            cat_conn.commit()
            cat_conn.close()
        except Exception as e:
            print(f"[-] WARNING: Could not update the desktop thread catalog: {e}")
    conn.close()

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


def sync_turn_statuses(th_db, src_id, dst_id):
    """Copy non-completed turn statuses (interrupted/failed) from src to the same turns of dst."""
    if not os.path.exists(th_db):
        return 0
    with sqlite3.connect(th_db, timeout=10.0) as conn:
        rows = conn.execute(
            "SELECT turn_id, status FROM thread_turns WHERE thread_id = ? AND status IN ('interrupted', 'failed')",
            (src_id,),
        ).fetchall()
        fixed = 0
        for turn_id, status in rows:
            fixed += conn.execute(
                "UPDATE thread_turns SET status = ? WHERE thread_id = ? AND turn_id = ? AND status = 'completed'",
                (status, dst_id, turn_id),
            ).rowcount
        return fixed


def move_queued_items(queue_db, src_id, dst_id):
    """Move queued follow-up messages from the session being hidden to its visible pair (appended in order)."""
    if not os.path.exists(queue_db) or src_id == dst_id:
        return 0
    with sqlite3.connect(queue_db, timeout=10.0) as conn:
        items = conn.execute(
            "SELECT id FROM queued_items WHERE thread_id = ? ORDER BY queue_order", (src_id,)
        ).fetchall()
        if not items:
            return 0
        base = conn.execute(
            "SELECT COALESCE(MAX(queue_order), -1) + 1 FROM queued_items WHERE thread_id = ?", (dst_id,)
        ).fetchone()[0]
        now_ms = int(time.time() * 1000)
        for i, (item_id,) in enumerate(items):
            conn.execute(
                "UPDATE queued_items SET thread_id = ?, queue_order = ?, updated_at_ms = ? WHERE id = ?",
                (dst_id, base + i, now_ms, item_id),
            )
        return len(items)


def sync_desktop_catalog(cat_conn, state_cur):
    """
    The desktop sidebar (Recents) reads sqlite/codex-dev.db:local_thread_catalog, which is filled incrementally
    and keeps archived and even deleted threads. Mirror state_5 into it: local Codex threads that are archived or
    gone get missing_candidate = 1 (excluded by the catalog's own indexes), visible ones get 0, and a full
    reconcile is requested so the app rebuilds the catalog on next start.
    """
    visible = {r[0] for r in state_cur.execute("SELECT id FROM threads WHERE archived = 0").fetchall()}
    rows = cat_conn.execute(
        "SELECT host_id, thread_id, missing_candidate FROM local_thread_catalog "
        "WHERE host_id = 'local' AND model_provider IS NOT NULL AND model_provider != ''"
    ).fetchall()
    hidden = 0
    for host_id, tid, missing in rows:
        want = 0 if tid in visible else 1
        if want != missing:
            cat_conn.execute(
                "UPDATE local_thread_catalog SET missing_candidate = ? WHERE host_id = ? AND thread_id = ?",
                (want, host_id, tid),
            )
            hidden += want
    try:
        cat_conn.execute("UPDATE local_thread_catalog_sync_state SET last_full_reconciled_at = 0 WHERE host_id = 'local'")
    except sqlite3.OperationalError:
        pass
    return hidden


def restore_unarchived_rollouts(cur, codex_home):
    """Move rollouts of non-archived threads out of archived_sessions/ into sessions/YYYY/MM/DD/."""
    archived_dir = os.path.join(codex_home, "archived_sessions")
    moved = 0
    rows = cur.execute("SELECT id, rollout_path FROM threads WHERE archived = 0").fetchall()
    for tid, ro_path in rows:
        if not ro_path or not os.path.isfile(ro_path):
            continue
        if os.path.normcase(os.path.dirname(os.path.abspath(ro_path))) != os.path.normcase(archived_dir):
            continue
        m = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T", os.path.basename(ro_path))
        parts = m.groups() if m else time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(ro_path))).split("-")
        dest_dir = os.path.join(codex_home, "sessions", *parts)
        dest = os.path.join(dest_dir, os.path.basename(ro_path))
        if os.path.exists(dest):
            continue
        os.makedirs(dest_dir, exist_ok=True)
        os.replace(ro_path, dest)
        cur.execute("UPDATE threads SET rollout_path = ? WHERE id = ?", (dest, tid))
        moved += 1
    return moved


def _wait_until_idle(codex_home, timeout_sec=45.0):
    from .activity import detect_running_sessions

    deadline = time.time() + timeout_sec
    while time.time() < deadline and detect_running_sessions(codex_home):
        time.sleep(2)


def _remote_daemon_running():
    from .apps import detect_codex_apps

    return any(a["kind"] == "remote-daemon" for a in detect_codex_apps())


def restart_remote_services(also_start=()):
    """
    Restart Codex remote daemons (user systemd) so mobile/remote clients pick up the new provider.
    Units in also_start (stopped before the switch) are started even if currently inactive.
    """
    import shutil
    import subprocess

    if not shutil.which("systemctl"):
        return
    for unit in REMOTE_SERVICES:
        try:
            active = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit], timeout=10
            ).returncode == 0
            if not active and unit not in also_start:
                continue
            if active:
                # stop -> wait -> start (retry): an immediate restart races with the old remote-control connection
                try:
                    subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, text=True, timeout=60)
                except subprocess.TimeoutExpired:
                    # A slow stop must not leave the unit dead: kill what is left and start it anyway
                    print(f"[-] WARNING: stopping {unit} timed out; killing it and starting again.")
                    subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", unit], capture_output=True, timeout=30)
                    subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True, timeout=30)
            r = None
            for attempt in range(3):
                time.sleep(3 + 3 * attempt)
                r = subprocess.run(["systemctl", "--user", "start", unit], capture_output=True, text=True, timeout=90)
                if r.returncode == 0:
                    break
            if r is not None and r.returncode == 0:
                print(f"[+] Restarted {unit} so remote clients use the new provider.")
            elif r is not None and "connection is errored" in (r.stderr + r.stdout) and _remote_daemon_running():
                # chatgpt.com keeps the previous server registered for a few minutes (409 "already online");
                # the daemon already runs with the new config and keeps reconnecting on its own
                subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True, timeout=30)
                print(
                    f"[!] {unit}: daemon restarted but remote control is still reconnecting "
                    "(chatgpt.com has not released the previous session yet). Reopen the mobile app in a few minutes."
                )
            else:
                print(f"[-] WARNING: Failed to restart {unit}: {(r.stderr if r else '').strip()}")
        except Exception as e:
            print(f"[-] WARNING: Could not restart {unit}: {e}")


def switch_provider(target_mode, codex_home, auto_sync=True, force=False, stop_apps=False):
    """
    Switch provider, then restart remote daemons (Codex remote-control) when the switch succeeded.
    Running Codex apps (desktop, remote daemon, CLI) are stopped first (asks unless stop_apps=True).
    """
    mode = target_mode.lower().strip()
    stopped_units = []
    if mode in ("deepseek", "openai"):
        from .apps import ensure_codex_apps_stopped

        ok_apps, stopped_units = ensure_codex_apps_stopped(stop_apps=stop_apps)
        if not ok_apps:
            if stopped_units:
                restart_remote_services(also_start=stopped_units)
            return False
        if stopped_units:
            # The stopped app-server wrote rollouts right before exiting; let the "recent write" check clear
            _wait_until_idle(codex_home)
    ok = _switch_provider_impl(target_mode, codex_home, auto_sync=auto_sync, force=force)
    if mode in ("deepseek", "openai") and (ok or stopped_units):
        restart_remote_services(also_start=stopped_units)
    return ok

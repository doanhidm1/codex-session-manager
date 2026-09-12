import os
import sqlite3

from .config import CodexPaths, normalize_path


def check_thread_health(thread_id, codex_home):
    """
    Check if thread exists in state_5.sqlite and rollout file is valid and readable.
    Returns (is_healthy, reason_str, thread_row).
    """
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.state_db):
        return False, "state_5.sqlite does not exist", None

    try:
        with sqlite3.connect(paths.state_db, timeout=5.0) as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT id, name, title, model_provider, rollout_path FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if not row:
                return False, f"Thread record does not exist in state_5.sqlite ({thread_id[:8]})", None

            tid, name, title, prov, ro_path = row
            if not ro_path:
                return False, "rollout_path in database is empty", row

            ro_norm = normalize_path(ro_path)
            if not os.path.exists(ro_norm):
                return False, f"Rollout file does not exist on disk: {ro_norm}", row

            if os.path.getsize(ro_norm) == 0:
                return False, f"Rollout file is empty (0 bytes): {ro_norm}", row

            # Check if JSON readable
            with open(ro_norm, "rb") as f:
                first_line = f.readline().decode("utf-8", errors="ignore").strip()
                if not first_line or not first_line.startswith("{"):
                    return False, "Rollout file is corrupted (cannot read valid JSON record)", row

            return True, "OK", row
    except Exception as e:
        return False, f"Error while validating thread health: {e}", None


def get_mapped_pair_info(thread_id, codex_home):
    """Query session_manager.sqlite to check if this thread is already part of a registered pair."""
    paths = CodexPaths(codex_home)
    if not os.path.exists(paths.mapping_db):
        return None
    try:
        with sqlite3.connect(paths.mapping_db, timeout=5.0) as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT pair_id, name, openai_thread_id, deepseek_thread_id, is_active FROM session_pairs "
                "WHERE openai_thread_id = ? OR deepseek_thread_id = ?",
                (thread_id, thread_id),
            ).fetchone()
            return row
    except Exception:
        return None


def heal_or_rebuild_target(src_id, old_tgt_id, tgt_prov, codex_home, pair_name=None):
    """
    Rebuild a broken/deleted target session cleanly from source using full convert.
    Updates session_pairs mapping if a new target thread was generated.
    """
    from .migration import migrate_thread
    from .sync_overwrite import overwrite_target_from_source

    paths = CodexPaths(codex_home)

    # Check if target thread record still exists in state_5.sqlite
    tgt_in_db = False
    if os.path.exists(paths.state_db):
        try:
            with sqlite3.connect(paths.state_db, timeout=5.0) as conn:
                r = conn.cursor().execute("SELECT id FROM threads WHERE id = ?", (old_tgt_id,)).fetchone()
                tgt_in_db = bool(r)
        except Exception:
            pass

    if tgt_in_db:
        # Rebuild existing thread via overwrite
        return overwrite_target_from_source(src_id, old_tgt_id, codex_home, force=True)
    else:
        # Target was deleted from DB -> clone fresh session from source
        print(
            f"[*] Target session ({old_tgt_id[:8]}) was deleted from database. Rebuilding fresh session from source..."
        )
        ok = migrate_thread(src_id, tgt_prov, codex_home)
        if not ok:
            return False
        # Find newly created thread id
        try:
            with sqlite3.connect(paths.state_db, timeout=5.0) as conn:
                cur = conn.cursor()
                new_row = cur.execute(
                    "SELECT id FROM threads WHERE model_provider = ? ORDER BY created_at DESC LIMIT 1", (tgt_prov,)
                ).fetchone()
                if new_row:
                    new_id = new_row[0]
                    with sqlite3.connect(paths.mapping_db, timeout=5.0) as m_conn:
                        m_cur = m_conn.cursor()
                        if tgt_prov == "openai":
                            m_cur.execute(
                                "UPDATE session_pairs SET openai_thread_id = ? WHERE openai_thread_id = ?",
                                (new_id, old_tgt_id),
                            )
                        else:
                            m_cur.execute(
                                "UPDATE session_pairs SET deepseek_thread_id = ? WHERE deepseek_thread_id = ?",
                                (new_id, old_tgt_id),
                            )
                        m_conn.commit()
                    print(f"[+] Successfully updated pair mapping with new target ID: {new_id[:8]}")
            return True
        except Exception as e:
            print(f"[!] Warning while updating pair mapping: {e}")
            return True


def validate_pair_for_sync(src_id, tgt_id, src_prov, tgt_prov, codex_home, force=False):
    """
    Validate target health before sync. If broken/deleted and not force, prints warning and halts.
    If force=True, cleanly rebuilds from scratch.
    """
    src_ok, src_reason, _ = check_thread_health(src_id, codex_home)
    if not src_ok:
        print(f"[!] ERROR: Source session [{src_prov.upper()}] ({src_id[:8]}) is invalid: {src_reason}")
        return False, "source_invalid"

    tgt_ok, tgt_reason, _ = check_thread_health(tgt_id, codex_home)
    if tgt_ok:
        return True, "healthy"

    # Target is broken or deleted. Check if mapped in DB
    mapped = get_mapped_pair_info(tgt_id, codex_home) or get_mapped_pair_info(src_id, codex_home)
    pair_name = mapped[1] if mapped else f"{src_id[:8]}<->{tgt_id[:8]}"

    if not force:
        print("\n" + "=" * 76)
        print(f" [!] CORRUPTED OR DELETED TARGET DETECTED: [{pair_name}]")
        print(f"     Target ID    : [{tgt_prov.upper()}] {tgt_id}")
        print(f"     Reason       : {tgt_reason}")
        print("     Standard incremental append cannot be performed.")
        print("     To bypass append and perform a clean full conversion from scratch")
        print("     (rebuilding clean target from source), please re-run with '--force'.")
        print("=" * 76 + "\n")
        return False, "target_broken_need_force"
    else:
        print(
            f"[*] [--force] Target session [{pair_name}] is corrupted/deleted. Performing full convert from scratch..."
        )
        ok = heal_or_rebuild_target(src_id, tgt_id, tgt_prov, codex_home, pair_name)
        return (True, "healed") if ok else (False, "heal_failed")

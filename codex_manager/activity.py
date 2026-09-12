import os
import sqlite3
import time

from .config import CodexPaths


def check_file_and_db_locks(codex_home):
    """
    Test if SQLite databases and rollout files can be locked exclusively.
    Returns (True, None) if clean, or (False, reason_str) if locked by Codex.
    """
    paths = CodexPaths(codex_home)
    dbs = [
        ("state_5.sqlite", paths.state_db),
        ("thread_history_1.sqlite", paths.th_db),
        ("session_manager.sqlite", paths.mapping_db),
    ]
    for name, db_p in dbs:
        if os.path.exists(db_p):
            try:
                conn = sqlite3.connect(db_p, timeout=0.3)
                conn.execute("BEGIN EXCLUSIVE")
                conn.rollback()
                conn.close()
            except sqlite3.OperationalError as e:
                return False, f"Database '{name}' is currently locked by Codex ({e})"
            except Exception:
                pass

    if os.path.exists(paths.state_db):
        try:
            with sqlite3.connect(paths.state_db, timeout=2.0) as conn_s:
                rows = conn_s.execute("SELECT id, rollout_path FROM threads WHERE archived = 0").fetchall()
                for tid, ro_path in rows:
                    if ro_path and os.path.exists(ro_path):
                        try:
                            with open(ro_path, "r+b"):
                                pass
                        except (PermissionError, OSError) as pe:
                            return False, f"Rollout file for session '{tid[:8]}' is currently locked ({pe})"
        except Exception:
            pass

    return True, None


def detect_running_sessions(codex_home, threshold_sec=60):
    """Detect if any Codex session is currently executing/generating."""
    paths = CodexPaths(codex_home)
    now = time.time()
    running_by_tid = {}

    if os.path.exists(paths.th_db) and os.path.exists(paths.state_db):
        try:
            with sqlite3.connect(paths.th_db, timeout=2.0) as conn_h:
                in_prog = (
                    conn_h.cursor()
                    .execute(
                        "SELECT thread_id, turn_id, started_at FROM thread_turns "
                        "WHERE status = 'inProgress' AND completed_at IS NULL ORDER BY started_at DESC"
                    )
                    .fetchall()
                )
                if in_prog:
                    with sqlite3.connect(paths.state_db, timeout=2.0) as conn_s:
                        cur_s = conn_s.cursor()
                        for fork_tid, turn_id, started_at in in_prog:
                            if started_at and (now - started_at > 1800):
                                continue
                            row = cur_s.execute(
                                "SELECT id, title, rollout_path, model_provider FROM threads WHERE id = ? OR rollout_path LIKE ?",
                                (fork_tid, f"%{fork_tid}%"),
                            ).fetchone()
                            if row:
                                tid, title, ro_path, prov = row
                                if tid not in running_by_tid and ro_path and os.path.exists(ro_path):
                                    diff = now - os.path.getmtime(ro_path)
                                    if diff <= threshold_sec:
                                        running_by_tid[tid] = {
                                            "thread_id": tid,
                                            "title": title or "Untitled",
                                            "provider": prov or "unknown",
                                            "turn_id": turn_id,
                                            "last_active_sec": diff,
                                            "reason": f"inProgress ({diff:.1f}s ago)",
                                        }
        except Exception:
            pass

    if os.path.exists(paths.state_db):
        try:
            with sqlite3.connect(paths.state_db, timeout=2.0) as conn_s:
                rows = (
                    conn_s.cursor()
                    .execute("SELECT id, title, rollout_path, model_provider FROM threads WHERE archived = 0")
                    .fetchall()
                )
                for tid, title, ro_path, prov in rows:
                    if tid not in running_by_tid and ro_path and os.path.exists(ro_path):
                        diff = now - os.path.getmtime(ro_path)
                        if diff <= 30.0:
                            running_by_tid[tid] = {
                                "thread_id": tid,
                                "title": title or "Untitled",
                                "provider": prov or "unknown",
                                "turn_id": None,
                                "last_active_sec": diff,
                                "reason": f"Active write ({diff:.1f}s ago)",
                            }
        except Exception:
            pass

    return list(running_by_tid.values())


def assert_no_running_sessions(codex_home, force=False, threshold_sec=60):
    """
    Ensure Codex is NOT running and files are NOT locked before allowing switch/sync.
    Note: force does NOT bypass running sessions or locked files.
    """
    lock_ok, lock_err = check_file_and_db_locks(codex_home)
    if not lock_ok:
        print("\n" + "=" * 76)
        print(" [!] REJECTED: Operation blocked because database or file is locked by Codex!")
        print(f"     Details: {lock_err}")
        print("     Please wait for Codex to finish its current task or stop the active session.")
        print("=" * 76 + "\n")
        return False

    running = detect_running_sessions(codex_home, threshold_sec=threshold_sec)
    if not running:
        return True

    print("\n" + "=" * 76)
    print(" [!] REJECTED: Active Codex session detected (inProgress / generating)!")
    for r in running:
        title_disp = r["title"].replace("\n", " ")[:50]
        print(f"     - Session : [{r['provider'].upper()}] {title_disp}")
        print(f"       ID      : {r['thread_id']}")
        if r.get("turn_id"):
            print(f"       Turn ID : {r['turn_id']}")
        print(f"       Last active: {r['last_active_sec']:.1f}s ago ({r['reason']})")
    print("\n     Codex must be completely idle (no generating turns, no file locks) before switching.")
    print("     Please wait for the turn to complete or press Stop in Codex.")
    print("=" * 76 + "\n")
    return False


def print_running_status(codex_home):
    """Format and print running sessions and lock status for diagnostics."""
    lock_ok, lock_err = check_file_and_db_locks(codex_home)
    running = detect_running_sessions(codex_home, threshold_sec=60)
    print("\n[Codex Active Running & File Lock Check]")
    if not lock_ok:
        print(f"  Lock Status : [LOCKED] {lock_err}")
    else:
        print("  Lock Status : [OK] All databases and rollout files are unlocked and accessible")

    if not running:
        print("  Turn Status : [IDLE] No active sessions running.")
    else:
        print(f"  Turn Status : [RUNNING] Detected {len(running)} active session(s):")
        for r in running:
            title_disp = r["title"].replace("\n", " ")[:55]
            print(f"  - [{r['provider'].upper()}] {title_disp} ({r['thread_id'][:8]}): {r['reason']}")

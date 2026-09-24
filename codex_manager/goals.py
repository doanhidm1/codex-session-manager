import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .mapping import get_all_pairs, normalize_path


def get_goals_db_path(codex_home: Optional[str] = None) -> str:
    """Returns the primary path to goals_1.sqlite."""
    if not codex_home:
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(codex_home, "goals_1.sqlite")


def get_secondary_goals_db_path(codex_home: Optional[str] = None) -> str:
    """Returns the secondary path ~/.codex/sqlite/goals_1.sqlite."""
    if not codex_home:
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(codex_home, "sqlite", "goals_1.sqlite")


def get_thread_goal(thread_id: str, codex_home: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch the goal record for a specific thread_id, if present."""
    db_path = get_goals_db_path(codex_home)
    if not os.path.exists(db_path):
        db_path = get_secondary_goals_db_path(codex_home)
        if not os.path.exists(db_path):
            return None

    try:
        conn = sqlite3.connect(normalize_path(db_path), timeout=5.0)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT thread_id, goal_id, objective, status, token_budget,
                       tokens_used, time_used_seconds, created_at_ms, updated_at_ms
                FROM thread_goals
                WHERE thread_id = ?
                """,
                (thread_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "thread_id": row[0],
                "goal_id": row[1],
                "objective": row[2],
                "status": row[3],
                "token_budget": row[4],
                "tokens_used": row[5],
                "time_used_seconds": row[6],
                "created_at_ms": row[7],
                "updated_at_ms": row[8],
            }
        finally:
            conn.close()
    except Exception:
        return None


def sync_goals(
    codex_home: Optional[str] = None,
    specific_pair: Optional[Tuple[str, str]] = None,
) -> Tuple[bool, str, int]:
    """
    Synchronizes thread_goals and thread_goal_continuation_deferrals between paired threads.
    If specific_pair is provided, syncs only that (source_id, target_id) pair.
    Otherwise queries all registered pairs from session_manager.sqlite.

    Returns (success: bool, message: str, synced_count: int).
    """
    pairs: List[Tuple[str, str]] = []
    if specific_pair:
        pairs = [specific_pair]
    else:
        from .config import CodexPaths

        paths = CodexPaths(codex_home)
        if os.path.exists(paths.mapping_db):
            mapping_pairs = get_all_pairs(paths.mapping_db, active_only=True)
            pairs = [(p[2], p[3]) for p in mapping_pairs if p[2] and p[3]]

    if not pairs:
        return True, "No pairs to sync goals for", 0

    target_dbs = [get_goals_db_path(codex_home)]
    sec_db = get_secondary_goals_db_path(codex_home)
    if os.path.exists(sec_db):
        target_dbs.append(sec_db)

    total_synced = 0
    for db_path in target_dbs:
        if not os.path.exists(db_path):
            continue

        try:
            conn = sqlite3.connect(normalize_path(db_path), timeout=5.0)
            try:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='thread_goals'")
                if not cur.fetchone():
                    continue

                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='thread_goal_continuation_deferrals'"
                )
                has_deferrals = bool(cur.fetchone())

                for src_id, tgt_id in pairs:
                    cur.execute(
                        """
                        SELECT thread_id, goal_id, objective, status, token_budget,
                               tokens_used, time_used_seconds, created_at_ms, updated_at_ms
                        FROM thread_goals
                        WHERE thread_id IN (?, ?)
                        """,
                        (src_id, tgt_id),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        continue

                    goals_by_id = {r[0]: r for r in rows}
                    src_goal = goals_by_id.get(src_id)
                    tgt_goal = goals_by_id.get(tgt_id)

                    chosen_donor = None
                    recipient_id = None

                    if src_goal and not tgt_goal:
                        chosen_donor = src_goal
                        recipient_id = tgt_id
                    elif tgt_goal and not src_goal:
                        chosen_donor = tgt_goal
                        recipient_id = src_id
                    elif src_goal and tgt_goal:
                        src_up = src_goal[8] or 0
                        tgt_up = tgt_goal[8] or 0
                        if src_up > tgt_up:
                            chosen_donor = src_goal
                            recipient_id = tgt_id
                        elif tgt_up > src_up:
                            chosen_donor = tgt_goal
                            recipient_id = src_id

                    if chosen_donor and recipient_id:
                        cur.execute(
                            """
                            INSERT INTO thread_goals (
                                thread_id, goal_id, objective, status, token_budget,
                                tokens_used, time_used_seconds, created_at_ms, updated_at_ms
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(thread_id) DO UPDATE SET
                                goal_id = excluded.goal_id,
                                objective = excluded.objective,
                                status = excluded.status,
                                token_budget = excluded.token_budget,
                                tokens_used = excluded.tokens_used,
                                time_used_seconds = excluded.time_used_seconds,
                                created_at_ms = excluded.created_at_ms,
                                updated_at_ms = excluded.updated_at_ms
                            """,
                            (
                                recipient_id,
                                chosen_donor[1],
                                chosen_donor[2],
                                chosen_donor[3],
                                chosen_donor[4],
                                chosen_donor[5],
                                chosen_donor[6],
                                chosen_donor[7],
                                chosen_donor[8],
                            ),
                        )
                        total_synced += 1

                        if has_deferrals:
                            cur.execute(
                                "SELECT 1 FROM thread_goal_continuation_deferrals WHERE thread_id = ?",
                                (chosen_donor[0],),
                            )
                            if cur.fetchone():
                                cur.execute(
                                    """
                                    INSERT OR IGNORE INTO thread_goal_continuation_deferrals (thread_id)
                                    VALUES (?)
                                    """,
                                    (recipient_id,),
                                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            return False, f"Error syncing goals in {db_path}: {e}", total_synced

    return True, f"Successfully synced {total_synced} goal record(s)", total_synced

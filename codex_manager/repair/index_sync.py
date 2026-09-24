"""
SQLite index and byte-offset synchronization module.
Aligns rollout_byte_offset and rollout_end_byte_offset in thread_turns,
and next_rollout_byte_offset / next_rollout_ordinal in thread_history_projection_state.
"""

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger("codex_manager.repair.index_sync")


def sync_session_offsets(
    thread_id: str,
    rollout_path: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Computes exact byte offsets for turns in rollout_path and synchronizes
    thread_turns and thread_history_projection_state in thread_history_1.sqlite.
    """
    if not os.path.exists(rollout_path):
        return {"success": False, "error": f"Rollout file not found: {rollout_path}"}

    if not db_path:
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        db_path = os.path.join(codex_home, "thread_history_1.sqlite")

    if not os.path.exists(db_path):
        return {"success": False, "error": f"Database not found: {db_path}"}

    # 1. Parse line offsets and identify turns
    line_offsets: List[int] = []
    line_lengths: List[int] = []
    turn_events: List[Dict[str, Any]] = []

    current_offset = 0
    total_lines = 0
    max_ordinal = 0

    with open(rollout_path, "rb") as f:
        for idx, line in enumerate(f):
            line_offsets.append(current_offset)
            line_lengths.append(len(line))
            current_offset += len(line)
            total_lines += 1

            try:
                data = json.loads(line.decode("utf-8"))
                ord_val = data.get("ordinal")
                if ord_val is not None and ord_val > max_ordinal:
                    max_ordinal = ord_val
                ev_type = data.get("type")
                payload = data.get("payload", {})
                p_type = payload.get("type") if isinstance(payload, dict) else None

                if ev_type == "event_msg" and p_type in ("task_started", "task_complete"):
                    turn_events.append(
                        {
                            "line_idx": idx,
                            "offset": line_offsets[idx],
                            "ordinal": ord_val if ord_val is not None else idx,
                            "event": p_type,
                            "turn_id": payload.get("turn_id"),
                        }
                    )
            except Exception:
                pass

    total_bytes = current_offset

    # 2. Pair task_started and task_complete to calculate turn byte ranges
    turn_ranges: Dict[str, Dict[str, int]] = {}
    current_turn_id = None
    current_start_offset = None
    current_start_ord = None

    for ev in turn_events:
        tid = ev["turn_id"]
        if ev["event"] == "task_started":
            current_turn_id = tid
            current_start_offset = ev["offset"]
            current_start_ord = ev["ordinal"]
        elif ev["event"] == "task_complete" and current_turn_id:
            end_line = ev["line_idx"]
            end_offset = line_offsets[end_line] + line_lengths[end_line] - 1
            turn_ranges[current_turn_id] = {
                "byte_offset": current_start_offset or 0,
                "end_byte_offset": end_offset,
                "ordinal": current_start_ord or 0,
                "end_ordinal": ev["ordinal"],
            }
            current_turn_id = None

    # 3. Update SQLite
    updated_turns = 0
    try:
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            cur = conn.cursor()

            for tid, tr in turn_ranges.items():
                cur.execute(
                    """
                    UPDATE thread_turns
                    SET rollout_byte_offset = ?,
                        rollout_end_byte_offset = ?,
                        rollout_ordinal = ?,
                        rollout_end_ordinal = ?
                    WHERE thread_id = ? AND turn_id = ?
                    """,
                    (
                        tr["byte_offset"],
                        tr["end_byte_offset"],
                        tr["ordinal"],
                        tr["end_ordinal"],
                        thread_id,
                        tid,
                    ),
                )
                updated_turns += cur.rowcount

            # Update projection state
            next_ordinal = (max_ordinal + 1) if max_ordinal > 0 else total_lines
            cur.execute(
                """
                UPDATE thread_history_projection_state
                SET next_rollout_byte_offset = ?,
                    next_rollout_ordinal = ?
                WHERE thread_id = ?
                """,
                (total_bytes, next_ordinal, thread_id),
            )
            projection_updated = cur.rowcount > 0

            conn.commit()

        logger.info(
            "Synced offsets for thread %s: %d turns updated, total bytes=%d",
            thread_id,
            updated_turns,
            total_bytes,
        )
        return {
            "success": True,
            "thread_id": thread_id,
            "updated_turns": updated_turns,
            "total_bytes": total_bytes,
            "total_lines": total_lines,
            "projection_updated": projection_updated,
        }
    except Exception as e:
        logger.error("Failed to sync offsets in SQLite: %s", e)
        return {"success": False, "error": str(e)}


def heal_thread_items_sources(db_path: Optional[str] = None) -> int:
    """
    Scans thread_items in thread_history_1.sqlite and ensures that all commandExecution
    items have valid camelCase source values ('unifiedExecStartup', 'unifiedExecInteraction',
    'userShell', 'agent') and normalized paths, preventing Codex Rust serde deserialization errors.
    Returns the count of healed items.
    """
    if not db_path:
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        db_path = os.path.join(codex_home, "thread_history_1.sqlite")

    if not os.path.exists(db_path):
        return 0

    healed = 0
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT rowid, item_id, item_json FROM thread_items WHERE item_json LIKE '%unified_exec%' OR item_json LIKE '%file:///%'"
        )
        rows = cur.fetchall()

        for rid, _iid, ijson in rows:
            try:
                data = json.loads(ijson)
                modified = False
                src = data.get("source")
                if src == "unified_exec_startup":
                    data["source"] = "unifiedExecStartup"
                    modified = True
                elif src == "unified_exec_interaction":
                    data["source"] = "unifiedExecInteraction"
                    modified = True
                elif src == "user_shell":
                    data["source"] = "userShell"
                    modified = True
                elif src and src not in ("agent", "userShell", "unifiedExecStartup", "unifiedExecInteraction"):
                    data["source"] = "unifiedExecStartup"
                    modified = True

                cwd = data.get("cwd")
                if isinstance(cwd, str) and cwd.startswith("file:///"):
                    data["cwd"] = os.path.normpath(cwd[8:])
                    modified = True

                for k in ("stdout", "stderr", "formatted_output"):
                    if k in data:
                        data.pop(k, None)
                        modified = True

                if modified:
                    new_json = json.dumps(data, ensure_ascii=False)
                    cur.execute("UPDATE thread_items SET item_json = ? WHERE rowid = ?", (new_json, rid))
                    healed += 1
            except Exception:
                pass

        if healed > 0:
            conn.commit()
            logger.info("Healed %d invalid commandExecution item(s) in %s", healed, db_path)
        conn.close()
    except Exception as e:
        logger.warning("Failed to heal thread_items in %s: %s", db_path, e)

    return healed

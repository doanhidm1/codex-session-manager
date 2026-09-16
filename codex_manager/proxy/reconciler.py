"""
SQLite thread history reconciler.
Fixes missing first_user_item_id and final_agent_item_id in thread_turns
so that Codex Desktop's `read_thread` and `wait_threads` tools return
the full turn contents instead of empty `items: []`.
"""

import json
import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger("deepseek_proxy.reconciler")


def get_thread_history_db_path(codex_home: Optional[str] = None) -> str:
    """Returns the default path to thread_history_1.sqlite."""
    if not codex_home:
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(codex_home, "thread_history_1.sqlite")


def reconcile_delegation_turns(db_path: Optional[str] = None, thread_id: Optional[str] = None) -> int:
    """
    Scans thread_turns in SQLite and populates first_user_item_id and final_agent_item_id
    for any completed turns that currently have first_user_item_id = NULL (e.g. delegated turns).

    Returns:
        The number of turn records updated.
    """
    if not db_path:
        db_path = get_thread_history_db_path()

    if not os.path.exists(db_path):
        return 0

    try:
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            cur = conn.cursor()

            # Query candidate turns
            if thread_id:
                cur.execute(
                    """
                    SELECT thread_id, turn_id
                    FROM thread_turns
                    WHERE thread_id = ? AND first_user_item_id IS NULL
                    """,
                    (thread_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT thread_id, turn_id
                    FROM thread_turns
                    WHERE first_user_item_id IS NULL
                    """
                )
            candidates = cur.fetchall()
            if not candidates:
                return 0

            updated_count = 0
            for tid, turn_id in candidates:
                # Find the first item of this turn
                cur.execute(
                    """
                    SELECT item_id
                    FROM thread_items
                    WHERE thread_id = ? AND turn_id = ?
                    ORDER BY rollout_ordinal ASC
                    LIMIT 1
                    """,
                    (tid, turn_id),
                )
                first_row = cur.fetchone()
                if not first_row:
                    continue

                first_item_id = first_row[0]

                # Ensure the turn starter item in thread_items has type 'userMessage'
                # so Codex Desktop's read_thread can deserialize and render it
                cur.execute(
                    "SELECT item_type, item_json FROM thread_items WHERE thread_id = ? AND item_id = ?",
                    (tid, first_item_id),
                )
                it_row = cur.fetchone()
                if it_row and it_row[0] != "userMessage":
                    clean_text = ""
                    try:
                        d = json.loads(it_row[1])
                        if "output" in d:
                            clean_text = d["output"]
                        elif "content" in d:
                            c = d["content"]
                            if isinstance(c, list):
                                clean_text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
                            else:
                                clean_text = str(c)
                    except Exception:
                        clean_text = "[Delegation message]"
                    if not clean_text:
                        clean_text = "[Delegation message]"
                    new_json = json.dumps(
                        {
                            "type": "userMessage",
                            "id": first_item_id,
                            "content": [{"type": "text", "text": clean_text}],
                            "clientId": None,
                        },
                        ensure_ascii=False,
                    )
                    cur.execute(
                        "UPDATE thread_items SET item_type = 'userMessage', item_json = ? WHERE thread_id = ? AND item_id = ?",
                        (new_json, tid, first_item_id),
                    )

                # Also find the final agent message of this turn if missing
                cur.execute(
                    """
                    SELECT item_id
                    FROM thread_items
                    WHERE thread_id = ? AND turn_id = ?
                      AND (item_type = 'agentMessage' OR (item_type = '' AND json_extract(item_json, '$.type') = 'agentMessage'))
                    ORDER BY rollout_ordinal DESC
                    LIMIT 1
                    """,
                    (tid, turn_id),
                )
                agent_row = cur.fetchone()
                final_agent_id = agent_row[0] if agent_row else None

                if final_agent_id:
                    cur.execute(
                        """
                        UPDATE thread_turns
                        SET first_user_item_id = ?,
                            final_agent_item_id = COALESCE(final_agent_item_id, ?)
                        WHERE thread_id = ? AND turn_id = ?
                        """,
                        (first_item_id, final_agent_id, tid, turn_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE thread_turns
                        SET first_user_item_id = ?
                        WHERE thread_id = ? AND turn_id = ?
                        """,
                        (first_item_id, tid, turn_id),
                    )
                updated_count += cur.rowcount

            conn.commit()
            if updated_count > 0:
                logger.info("Reconciled %d delegation turn(s) in SQLite.", updated_count)
            return updated_count
    except Exception as e:
        logger.warning("Failed to reconcile delegation turns: %s", e)
        return 0

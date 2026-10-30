"""
SQLite thread history reconciler.
Fixes missing first_user_item_id and final_agent_item_id in thread_turns
so that Codex Desktop's `read_thread` and `wait_threads` tools return
the full turn contents instead of empty `items: []`.
"""

import json
import logging
import os
import re
import sqlite3
from typing import Dict, Optional, Set

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
                # 1. First check if an item with type 'userMessage' already exists for this turn
                cur.execute(
                    """
                    SELECT item_id
                    FROM thread_items
                    WHERE thread_id = ? AND turn_id = ? AND item_type = 'userMessage'
                    ORDER BY rollout_ordinal ASC
                    LIMIT 1
                    """,
                    (tid, turn_id),
                )
                user_msg_row = cur.fetchone()
                if user_msg_row:
                    first_item_id = user_msg_row[0]
                else:
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
                                    clean_text = "".join(
                                        p.get("text", "") if isinstance(p, dict) else str(p) for p in c
                                    )
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


def get_automations_dir(codex_home: Optional[str] = None) -> str:
    """Returns the path to the automations directory."""
    if not codex_home:
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    user_prof = os.environ.get("USERPROFILE")
    default_dir = os.path.join(codex_home, "automations")
    if not os.path.exists(default_dir) and user_prof:
        cand = os.path.join(user_prof, ".codex", "automations")
        if os.path.exists(cand):
            return cand
    return default_dir


def reconcile_automations(
    automations_dir: Optional[str] = None,
    mapping_db_path: Optional[str] = None,
) -> int:
    """
    Scans automation.toml files in ~/.codex/automations.
    If any target_thread_id or prompt references an OpenAI thread ID with an active
    DeepSeek clone, it automatically updates it to the DeepSeek clone ID.

    Returns:
        The number of automation files updated.
    """
    if not automations_dir:
        automations_dir = get_automations_dir()

    if not os.path.isdir(automations_dir):
        return 0

    from .adapter import get_active_session_mappings

    oai_to_ds, _ = get_active_session_mappings(mapping_db_path)
    if not oai_to_ds:
        return 0

    updated_count = 0
    try:
        for root, _, files in os.walk(automations_dir):
            for file in files:
                if file == "automation.toml":
                    toml_path = os.path.join(root, file)
                    try:
                        with open(toml_path, "r", encoding="utf-8") as f:
                            content = f.read()

                        modified = False
                        for oai_id, ds_id in oai_to_ds.items():
                            if oai_id in content:
                                content = content.replace(oai_id, ds_id)
                                modified = True

                        if modified:
                            with open(toml_path, "w", encoding="utf-8") as f:
                                f.write(content)
                            updated_count += 1
                            logger.info(
                                "Reconciled automation file %s with active DeepSeek thread ID(s).",
                                toml_path,
                            )
                    except Exception as e:
                        logger.debug("Failed to reconcile automation file %s: %s", toml_path, e)
    except Exception as e:
        logger.warning("Error scanning automations directory: %s", e)

    return updated_count


def find_misdirected_automation_updates(response_tail: bytes, archived: Set[str]) -> Dict[str, str]:
    """
    Parse completed automation_update calls from a Responses stream/body and return
    {automation_id: archived_target_id} for those that point at an archived thread.
    """
    found: Dict[str, str] = {}
    text = response_tail.decode("utf-8", "replace")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if "automation_update" not in line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        items = []
        if isinstance(evt.get("item"), dict):
            items.append(evt["item"])
        resp = evt.get("response") if isinstance(evt.get("response"), dict) else evt
        items.extend(o for o in (resp.get("output") or []) if isinstance(o, dict))
        for it in items:
            if it.get("type") != "function_call" or it.get("name") != "automation_update":
                continue
            try:
                args = json.loads(it.get("arguments") or "{}")
            except Exception:
                continue
            aid, target = args.get("id"), args.get("targetThreadId")
            if aid and target in archived:
                found[aid] = target
    return found


def retarget_automation(automation_id: str, new_thread_id: str, codex_home: Optional[str] = None) -> bool:
    """Point an automation (automation.toml + desktop codex-dev.db) at new_thread_id."""
    home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    changed = False
    toml_path = os.path.join(home, "automations", automation_id, "automation.toml")
    if os.path.exists(toml_path):
        with open(toml_path, "r", encoding="utf-8") as f:
            content = f.read()
        new = re.sub(r'(?m)^target_thread_id\s*=\s*".*"$', f'target_thread_id = "{new_thread_id}"', content)
        if new != content:
            with open(toml_path, "w", encoding="utf-8") as f:
                f.write(new)
            changed = True
    db = os.path.join(home, "sqlite", "codex-dev.db")
    if os.path.exists(db):
        try:
            with sqlite3.connect(db, timeout=5.0) as conn:
                changed |= conn.execute(
                    "UPDATE automations SET target_thread_id = ? WHERE id = ? AND target_thread_id != ?",
                    (new_thread_id, automation_id, new_thread_id),
                ).rowcount > 0
        except Exception as e:
            logger.debug("Failed to retarget %s in %s: %s", automation_id, db, e)
    return changed

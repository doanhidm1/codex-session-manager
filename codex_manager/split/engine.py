import json
import os
import shutil
import sqlite3
import time
import uuid
from typing import Optional, Tuple

from ..config import CodexPaths, get_default_codex_home, normalize_path
from .scanner import scan_rollout_offsets


def split_single_thread(thread_id: str, keep_turns: int = 500, paths: Optional[CodexPaths] = None) -> Tuple[bool, str]:
    """
    Split a single thread into:
      - An archived base thread holding older turns (archived = 1)
      - An active continuation thread keeping the last `keep_turns` turns.
    Returns (success: bool, msg: str)
    """
    if not paths:
        paths = CodexPaths(get_default_codex_home())

    conn_s = sqlite3.connect(paths.state_db, timeout=10.0)
    conn_s.row_factory = sqlite3.Row
    cs = conn_s.cursor()
    row = cs.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if not row:
        conn_s.close()
        return False, f"Thread {thread_id} not found in state_5.sqlite"

    thread_meta = dict(row)
    rollout_path = normalize_path(thread_meta.get("rollout_path", ""))
    if not os.path.exists(rollout_path):
        conn_s.close()
        return False, f"Rollout file not found: {rollout_path}"

    conn_th = sqlite3.connect(paths.th_db, timeout=10.0)
    conn_th.row_factory = sqlite3.Row
    cth = conn_th.cursor()
    db_turns = cth.execute(
        "SELECT * FROM thread_turns WHERE thread_id = ? ORDER BY rollout_ordinal ASC",
        (thread_id,),
    ).fetchall()

    total_turns = len(db_turns)
    if total_turns <= keep_turns:
        conn_s.close()
        conn_th.close()
        return (
            True,
            f"Thread {thread_id[:8]} already has {total_turns} turns (<= {keep_turns}), no split needed.",
        )

    split_idx = total_turns - keep_turns
    split_turn = dict(db_turns[split_idx])
    split_turn_id = split_turn["turn_id"]
    split_ordinal = split_turn["rollout_ordinal"]

    base_id = str(uuid.uuid4())
    rollout_dir = os.path.dirname(rollout_path)
    rollout_base_name = os.path.basename(rollout_path)
    base_rollout = os.path.join(rollout_dir, f"base-{base_id}-{rollout_base_name}")

    print(f"[*] Splitting [{thread_meta.get('name') or thread_meta.get('title') or thread_id[:8]}]:")
    print(f"    - Total turns: {total_turns} -> Archive: {split_idx} turns | Active: {keep_turns} turns")
    print(f"    - Split at turn [{split_turn_id[:8]}], ordinal {split_ordinal}")

    # 1. Read source and split files
    split_byte_pos = None
    with open(rollout_path, "rb") as src, open(base_rollout, "wb") as dst_base:
        line0 = src.readline()
        meta0 = json.loads(line0.decode("utf-8"))
        if "payload" in meta0 and isinstance(meta0["payload"], dict):
            meta0["payload"]["id"] = base_id
            meta0["payload"]["session_id"] = base_id
        dst_base.write(json.dumps(meta0, ensure_ascii=False).encode("utf-8") + b"\n")

        while True:
            pos = src.tell()
            line = src.readline()
            if not line:
                break
            if f'"{split_turn_id}"'.encode("utf-8") in line and b'"task_started"' in line:
                split_byte_pos = pos
                break
            dst_base.write(line)

    if split_byte_pos is None:
        conn_s.close()
        conn_th.close()
        if os.path.exists(base_rollout):
            os.remove(base_rollout)
        return False, f"Could not find split turn start {split_turn_id} in rollout file"

    # 2. Write active continuation rollout using authentic original line 0
    temp_cont = rollout_path + ".split.tmp"
    cont_meta = json.loads(line0.decode("utf-8"))
    if "payload" in cont_meta and isinstance(cont_meta["payload"], dict):
        cont_meta["payload"]["id"] = thread_id
        cont_meta["payload"]["session_id"] = thread_id
        cont_meta["payload"]["history_mode"] = "paginated"
        cont_meta["payload"]["source"] = "vscode"
        cont_meta["payload"]["thread_source"] = "user"
        cont_meta["payload"]["originator"] = "Codex Desktop"
    clean_cont_line0 = json.dumps(cont_meta, ensure_ascii=False).encode("utf-8") + b"\n"

    with open(rollout_path, "rb") as src, open(temp_cont, "wb") as dst_cont:
        dst_cont.write(clean_cont_line0)
        src.seek(split_byte_pos)
        while True:
            line = src.readline()
            if not line:
                break
            dst_cont.write(line)

    # Backup original before replacing
    bak_path = rollout_path + f".bak_{int(time.time())}"
    shutil.copy2(rollout_path, bak_path)
    shutil.move(temp_cont, rollout_path)

    # 3. Rescan byte offsets
    base_turns = scan_rollout_offsets(base_rollout)
    cont_turns = scan_rollout_offsets(rollout_path)

    # 4. Insert base thread into state_5.sqlite
    base_row = dict(thread_meta)
    base_row["id"] = base_id
    base_title = thread_meta.get("title") or "Session"
    base_row["title"] = f"{base_title} [Archive 1-{split_idx}]"
    base_name = thread_meta.get("name") or "Archive"
    base_row["name"] = f"{base_name} [Archive]"
    base_row["archived"] = 1
    base_row["rollout_path"] = base_rollout
    base_row["is_pinned"] = 0
    base_row["section_position"] = None
    cols = list(base_row.keys())
    placeholders = ",".join("?" for _ in cols)
    cs.execute(
        f"INSERT OR REPLACE INTO threads ({','.join(cols)}) VALUES ({placeholders})",
        [base_row[c] for c in cols],
    )

    # Ensure continuation has history_mode = 'paginated' and updated created_at / first_user_message
    first_user_msg = thread_meta.get("first_user_message")
    split_user_item_id = split_turn.get("first_user_item_id")
    if split_user_item_id:
        item_row = cth.execute(
            "SELECT item_json FROM thread_items WHERE thread_id = ? AND item_id = ?",
            (thread_id, split_user_item_id),
        ).fetchone()
        if item_row:
            try:
                idata = json.loads(item_row["item_json"])
                cparts = idata.get("content", [])
                ctext = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in cparts)
                if ctext:
                    first_user_msg = ctext
            except Exception:
                pass

    cs.execute(
        """
        UPDATE threads
        SET history_mode = 'paginated',
            source = 'vscode',
            thread_source = 'user',
            originator = 'Codex Desktop',
            created_at = COALESCE(?, created_at),
            created_at_ms = COALESCE(? * 1000, created_at_ms),
            first_user_message = COALESCE(?, first_user_message),
            preview = COALESCE(?, preview)
        WHERE id = ?
    """,
        (
            split_turn.get("started_at"),
            split_turn.get("started_at"),
            first_user_msg,
            first_user_msg,
            thread_id,
        ),
    )
    conn_s.commit()
    conn_s.close()

    # 5. Update thread_history_1.sqlite
    orig_db_map = {r["turn_id"]: dict(r) for r in db_turns}

    cth.execute("DELETE FROM thread_turns WHERE thread_id = ?", (thread_id,))
    cth.execute("DELETE FROM thread_turns WHERE thread_id = ?", (base_id,))

    insert_sql = """
        INSERT INTO thread_turns (
            thread_id, turn_id, rollout_ordinal, status, error_json,
            started_at, completed_at, duration_ms, first_user_item_id,
            final_agent_item_id, rollout_byte_offset, rollout_end_ordinal, rollout_end_byte_offset
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    for t in base_turns:
        meta = orig_db_map.get(t["turn_id"], {})
        cth.execute(
            insert_sql,
            (
                base_id,
                t["turn_id"],
                t["ordinal"],
                meta.get("status", "completed"),
                meta.get("error_json"),
                meta.get("started_at"),
                meta.get("completed_at"),
                meta.get("duration_ms"),
                meta.get("first_user_item_id"),
                meta.get("final_agent_item_id"),
                t["start_offset"],
                t["end_ordinal"],
                t["end_byte_offset"],
            ),
        )

    for t in cont_turns:
        meta = orig_db_map.get(t["turn_id"], {})
        cth.execute(
            insert_sql,
            (
                thread_id,
                t["turn_id"],
                t["ordinal"],
                meta.get("status", "completed"),
                meta.get("error_json"),
                meta.get("started_at"),
                meta.get("completed_at"),
                meta.get("duration_ms"),
                meta.get("first_user_item_id"),
                meta.get("final_agent_item_id"),
                t["start_offset"],
                t["end_ordinal"],
                t["end_byte_offset"],
            ),
        )

    # Move older thread_items to base archive
    cth.execute(
        """
        UPDATE thread_items
        SET thread_id = ?
        WHERE thread_id = ?
          AND turn_id NOT IN (SELECT turn_id FROM thread_turns WHERE thread_id = ?)
    """,
        (base_id, thread_id, thread_id),
    )

    # Update thread_history_projection_state for both base and continuation
    base_file_size = os.path.getsize(base_rollout)
    cont_file_size = os.path.getsize(rollout_path)
    base_next_ord = split_ordinal
    cont_next_ord = cont_turns[-1]["end_ordinal"] + 1 if cont_turns else split_ordinal

    proj_sql = """
        INSERT INTO thread_history_projection_state (thread_id, next_rollout_byte_offset, next_rollout_ordinal)
        VALUES (?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            next_rollout_byte_offset = excluded.next_rollout_byte_offset,
            next_rollout_ordinal = excluded.next_rollout_ordinal
    """
    cth.execute(proj_sql, (base_id, base_file_size, base_next_ord))
    cth.execute(proj_sql, (thread_id, cont_file_size, cont_next_ord))

    conn_th.commit()
    conn_th.close()

    print(
        f"[+] Successfully split thread {thread_id[:8]}! Archive: {base_id[:8]} ({len(base_turns)} turns), Active: {len(cont_turns)} turns."
    )
    return (
        True,
        f"Split completed: {len(cont_turns)} turns kept, {len(base_turns)} turns archived.",
    )

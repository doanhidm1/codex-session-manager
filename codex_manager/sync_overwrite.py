import json
import os
import shutil
import sqlite3
import time

from .config import CodexPaths, normalize_path
from .db import resolve_thread


def overwrite_target_from_source(src_arg, tgt_arg, codex_home, force=False):
    """
    Completely rebuild target session with 100% full fidelity by converting all turns from source.
    Preserves:
      - Raw wire records, timestamps, local_image/input_image, separate steer messages
      - Auto-translates cross-thread delegation links (<source_thread_id>) to target provider counterparts
      - Retains history_base and pagination if present
      - Safely converts synthetic function outputs to <heartbeat> / <codex_delegation> user XML for DeepSeek
      - Guarantees valid lowerCamelCase projection schema in SQLite thread_items
    """
    paths = CodexPaths(codex_home)
    src_row = resolve_thread(src_arg, codex_home)
    tgt_row = resolve_thread(tgt_arg, codex_home)

    if not src_row:
        print(f"ERROR: Source thread not found: {src_arg}")
        return False
    if not tgt_row:
        print(f"ERROR: Target thread not found: {tgt_arg}")
        return False

    src_id, src_name, src_title, src_prov, src_rollout = src_row
    tgt_id, tgt_name, tgt_title, tgt_prov, tgt_rollout = tgt_row

    if src_id == tgt_id:
        print("ERROR: Source and Target are the same thread!")
        return False

    src_rollout = normalize_path(src_rollout)
    tgt_rollout = normalize_path(tgt_rollout)
    if not os.path.exists(src_rollout):
        print(f"ERROR: Source rollout file does not exist: {src_rollout}")
        return False

    from .activity import assert_no_running_sessions

    if not force:
        if not assert_no_running_sessions(codex_home, force=force):
            return False

    now_ts = int(time.time())
    now_ms = int(now_ts * 1000)

    # 1. Back up existing target rollout
    if os.path.exists(tgt_rollout):
        bak_file = tgt_rollout + f".pre_overwrite_{now_ts}.bak"
        try:
            shutil.copy2(tgt_rollout, bak_file)
        except Exception:
            pass

    # 2. Resolve chained thread IDs and rollout paths for source
    from .sync import get_chained_thread_ids

    src_ids = get_chained_thread_ids(src_id, src_rollout)

    found_rollouts = []
    sessions_dir = paths.sessions_dir
    for root, _, files in os.walk(sessions_dir):
        for f in files:
            if f.endswith(".jsonl") and not f.endswith(".bak"):
                for s_id in src_ids:
                    if s_id in f:
                        f_full = normalize_path(os.path.join(root, f))
                        if f_full not in found_rollouts:
                            found_rollouts.append(f_full)
    found_rollouts.sort(key=lambda p: os.path.basename(p))
    source_rollouts = found_rollouts if found_rollouts else [src_rollout]

    # 3. Build thread mapping for delegation link translation
    thread_map = {}
    if os.path.exists(paths.mapping_db):
        try:
            with sqlite3.connect(paths.mapping_db, timeout=5.0) as m_conn:
                for row in m_conn.cursor().execute(
                    "SELECT openai_thread_id, deepseek_thread_id FROM session_pairs"
                ).fetchall():
                    o_id, d_id = row[0], row[1]
                    if tgt_prov == "deepseek":
                        thread_map[o_id] = d_id
                    elif tgt_prov == "openai":
                        thread_map[d_id] = o_id
        except Exception:
            pass

    if "01a07efa-153d-7d70-958c-96eee02279f2" in thread_map:
        thread_map["01a03491-ec93-7041-a392-812d743f4e72"] = thread_map["01a07efa-153d-7d70-958c-96eee02279f2"]
    if "019ff980-dce4-7741-abe1-5236ad8bafbc" in thread_map:
        thread_map["01a08f8b-03c5-7553-8f81-249f1ac676f7"] = thread_map["019ff980-dce4-7741-abe1-5236ad8bafbc"]

    def translate_thread_ids(text):
        if not text or not thread_map:
            return text
        res = text
        for s_t, t_t in thread_map.items():
            res = res.replace(f"<source_thread_id>{s_t}</source_thread_id>", f"<source_thread_id>{t_t}</source_thread_id>")
            res = res.replace(f"<source_thread_id> {s_t} </source_thread_id>", f"<source_thread_id>{t_t}</source_thread_id>")
        return res

    # 4. Pre-fetch source items from SQLite thread_items
    conn_th = sqlite3.connect(paths.th_db, timeout=10.0)
    cur_th = conn_th.cursor()
    placeholders_s = ",".join(["?"] * len(src_ids))
    cur_th.execute(
        f"SELECT item_id, turn_id, item_type, item_json FROM thread_items WHERE thread_id IN ({placeholders_s})",
        src_ids,
    )
    source_items_cache = {}
    for iid, tid, itype, ijson in cur_th.fetchall():
        source_items_cache[iid] = (tid, itype, translate_thread_ids(ijson))

    with sqlite3.connect(paths.state_db, timeout=5.0) as conn_s:
        tgt_model_row = conn_s.cursor().execute(
            "SELECT model, cwd, history_mode FROM threads WHERE id = ?", (tgt_id,)
        ).fetchone()
    tgt_model = (tgt_model_row[0] if tgt_model_row else None) or (
        "DeepSeek-Flash High" if tgt_prov == "deepseek" else "gpt-5.6-terra"
    )
    tgt_cwd = normalize_path((tgt_model_row[1] if tgt_model_row else None) or os.path.expanduser("~"))

    # 5. Read first session_meta from primary source rollout
    first_meta = None
    with open(source_rollouts[0], "r", encoding="utf-8", errors="ignore") as sf:
        for line in sf:
            try:
                d = json.loads(line)
                if d.get("type") == "session_meta":
                    first_meta = d
                    break
            except Exception:
                continue

    if not first_meta:
        first_meta = {
            "type": "session_meta",
            "payload": {
                "id": tgt_id,
                "session_id": tgt_id,
                "model_provider": tgt_prov,
                "model": tgt_model,
                "cwd": tgt_cwd,
            },
        }
    else:
        p = first_meta.get("payload", {})
        p["session_id"] = tgt_id
        p["id"] = tgt_id
        p["model_provider"] = tgt_prov
        p["model"] = tgt_model
        p["cwd"] = tgt_cwd

    p = first_meta.get("payload", {})
    p["history_mode"] = "paginated"
    hist_base = p.get("history_base")

    base_ord = 0
    if hist_base and isinstance(hist_base, dict) and "end_ordinal_exclusive" in hist_base:
        base_ord = hist_base["end_ordinal_exclusive"]
    elif first_meta.get("ordinal", 0) > 0:
        base_ord = first_meta.get("ordinal", 0)

    first_meta["ordinal"] = base_ord

    out_file = open(tgt_rollout, "w", encoding="utf-8")
    meta_line = json.dumps(first_meta, ensure_ascii=False) + "\n"
    out_file.write(meta_line)
    curr_offset = len(meta_line.encode("utf-8"))
    curr_ord = base_ord + 1

    turns_meta = []
    items_meta = []
    seen_turn_ids = set()

    for s_rollout in source_rollouts:
        curr_tid = None
        curr_turn_start_ord = curr_ord
        curr_turn_start_offset = curr_offset
        curr_turn_started_at = now_ts
        curr_turn_completed_at = None
        curr_turn_duration_ms = None
        curr_turn_error_json = None
        curr_turn_first_user = None
        curr_turn_final_agent = None

        with open(s_rollout, "r", encoding="utf-8", errors="ignore") as sf:
            for line in sf:
                try:
                    d = json.loads(line)
                except Exception:
                    continue

                rtype = d.get("type")
                if rtype == "session_meta":
                    continue

                p = d.get("payload", {})
                d["ordinal"] = curr_ord
                rec_ord = curr_ord
                curr_ord += 1

                if rtype == "event_msg":
                    ptype = p.get("type")
                    if ptype == "task_started":
                        tid = p.get("turn_id")
                        if curr_tid and curr_tid != tid:
                            turns_meta.append({
                                "thread_id": tgt_id,
                                "turn_id": curr_tid,
                                "rollout_ordinal": curr_turn_start_ord,
                                "status": "failed" if curr_turn_error_json else "completed",
                                "error_json": curr_turn_error_json,
                                "started_at": curr_turn_started_at,
                                "completed_at": curr_turn_completed_at or curr_turn_started_at,
                                "duration_ms": curr_turn_duration_ms,
                                "first_user_item_id": curr_turn_first_user,
                                "final_agent_item_id": curr_turn_final_agent,
                                "rollout_byte_offset": curr_turn_start_offset,
                                "rollout_end_ordinal": rec_ord - 1,
                                "rollout_end_byte_offset": curr_offset,
                            })
                            curr_tid = None

                        if tid:
                            curr_tid = tid
                            seen_turn_ids.add(tid)
                            curr_turn_start_ord = rec_ord
                            curr_turn_start_offset = curr_offset
                            curr_turn_started_at = p.get("started_at") or now_ts
                            curr_turn_completed_at = None
                            curr_turn_duration_ms = None
                            curr_turn_error_json = None
                            curr_turn_first_user = None
                            curr_turn_final_agent = None
                            p["model_context_window"] = 996147

                    elif ptype == "task_complete":
                        tid = p.get("turn_id") or curr_tid
                        curr_turn_completed_at = p.get("completed_at") or now_ts
                        curr_turn_duration_ms = p.get("duration_ms")
                        if p.get("error"):
                            curr_turn_error_json = json.dumps(p["error"], ensure_ascii=False)

                    elif ptype == "item_completed":
                        it = p.get("item", {})
                        itype_raw = it.get("type", "")
                        iid = it.get("id", "")

                        is_converted_xml = False
                        if tgt_prov != "openai" and itype_raw == "FunctionCallOutput":
                            iname = it.get("name")
                            out_str = it.get("output", "")
                            if iname in ("automation_update", "send_message_to_thread") or "<codex_delegation>" in out_str or "<heartbeat>" in out_str:
                                out_str_trans = translate_thread_ids(out_str)
                                it["type"] = "UserMessage"
                                it["content"] = [{"type": "text", "text": out_str_trans}]
                                it.pop("name", None)
                                it.pop("output", None)
                                it.pop("namespace", None)
                                itype_raw = "UserMessage"
                                is_converted_xml = True

                        if itype_raw in ("UserMessage", "userMessage"):
                            if not curr_turn_first_user:
                                curr_turn_first_user = iid
                        elif itype_raw in ("AgentMessage", "agentMessage"):
                            curr_turn_final_agent = iid

                        clean_itype = itype_raw[0].lower() + itype_raw[1:] if itype_raw else "unknown"
                        created_ms = p.get("started_at_ms") or p.get("completed_at_ms") or now_ms

                        if not is_converted_xml and iid in source_items_cache:
                            _, src_itype, src_ijson = source_items_cache[iid]
                            final_item_json = src_ijson
                            final_item_type = src_itype
                        else:
                            clean_item = dict(it)
                            clean_item["type"] = clean_itype
                            if clean_itype == "agentMessage" and "text" not in clean_item:
                                clean_item["text"] = "".join(
                                    c.get("text", "") for c in it.get("content", []) if isinstance(c, dict)
                                )
                            elif clean_itype == "commandExecution":
                                if "process_id" in clean_item and "processId" not in clean_item:
                                    clean_item["processId"] = clean_item.pop("process_id", None)
                                if "aggregated_output" in clean_item and "aggregatedOutput" not in clean_item:
                                    clean_item["aggregatedOutput"] = clean_item.pop("aggregated_output", None)
                                if "exit_code" in clean_item and "exitCode" not in clean_item:
                                    clean_item["exitCode"] = clean_item.pop("exit_code", None)
                                if "duration" in clean_item and "durationMs" not in clean_item:
                                    clean_item["durationMs"] = clean_item.pop("duration", None)
                                if "parsed_cmd" in clean_item and "commandActions" not in clean_item:
                                    clean_item["commandActions"] = clean_item.pop("parsed_cmd", None)
                            final_item_json = translate_thread_ids(json.dumps(clean_item, ensure_ascii=False))
                            final_item_type = clean_itype

                        item_turn_id = p.get("turn_id") or curr_tid
                        if not item_turn_id and iid in source_items_cache:
                            item_turn_id = source_items_cache[iid][0]
                        if not item_turn_id and turns_meta:
                            item_turn_id = turns_meta[-1]["turn_id"]
                        if not item_turn_id:
                            item_turn_id = curr_tid

                        if item_turn_id:
                            items_meta.append({
                                "thread_id": tgt_id,
                                "turn_id": item_turn_id,
                                "item_id": iid,
                                "rollout_ordinal": rec_ord,
                                "created_at_ms": created_ms,
                                "item_json": final_item_json,
                                "item_type": final_item_type,
                                "updated_at_ordinal": rec_ord,
                            })

                    if p.get("thread_id"):
                        p["thread_id"] = tgt_id

                elif rtype == "turn_context":
                    p["cwd"] = tgt_cwd
                    p["model"] = tgt_model

                elif rtype == "response_item":
                    ptype = p.get("type")
                    if ptype == "function_call_output":
                        pname = p.get("name")
                        out_str = p.get("output", "")
                        if pname in ("automation_update", "send_message_to_thread") or "<codex_delegation>" in out_str or "<heartbeat>" in out_str:
                            out_str_trans = translate_thread_ids(out_str)
                            p["type"] = "message"
                            p["role"] = "user"
                            p["content"] = [{"type": "input_text", "text": out_str_trans}]
                            p.pop("name", None)
                            p.pop("output", None)
                            p.pop("namespace", None)

                elif rtype == "token_usage_record":
                    if p.get("thread_id"):
                        p["thread_id"] = tgt_id
                    if p.get("session_id"):
                        p["session_id"] = tgt_id

                line_str = translate_thread_ids(json.dumps(d, ensure_ascii=False)) + "\n"
                out_file.write(line_str)
                curr_offset += len(line_str.encode("utf-8"))

                if rtype == "event_msg" and p.get("type") == "task_complete" and curr_tid:
                    turns_meta.append({
                        "thread_id": tgt_id,
                        "turn_id": curr_tid,
                        "rollout_ordinal": curr_turn_start_ord,
                        "status": "failed" if curr_turn_error_json else "completed",
                        "error_json": curr_turn_error_json,
                        "started_at": curr_turn_started_at,
                        "completed_at": curr_turn_completed_at or curr_turn_started_at,
                        "duration_ms": curr_turn_duration_ms,
                        "first_user_item_id": curr_turn_first_user,
                        "final_agent_item_id": curr_turn_final_agent,
                        "rollout_byte_offset": curr_turn_start_offset,
                        "rollout_end_ordinal": rec_ord,
                        "rollout_end_byte_offset": curr_offset,
                    })
                    curr_tid = None

        if curr_tid:
            turns_meta.append({
                "thread_id": tgt_id,
                "turn_id": curr_tid,
                "rollout_ordinal": curr_turn_start_ord,
                "status": "failed" if curr_turn_error_json else "completed",
                "error_json": curr_turn_error_json,
                "started_at": curr_turn_started_at,
                "completed_at": curr_turn_completed_at or curr_turn_started_at,
                "duration_ms": curr_turn_duration_ms,
                "first_user_item_id": curr_turn_first_user,
                "final_agent_item_id": curr_turn_final_agent,
                "rollout_byte_offset": curr_turn_start_offset,
                "rollout_end_ordinal": curr_ord - 1,
                "rollout_end_byte_offset": curr_offset,
            })
            curr_tid = None

    out_file.close()

    # SQLite updates
    cur_th.execute("DELETE FROM thread_history_projection_state WHERE thread_id = ?", (tgt_id,))
    cur_th.execute("DELETE FROM thread_turns WHERE thread_id = ?", (tgt_id,))
    cur_th.execute("DELETE FROM thread_items WHERE thread_id = ?", (tgt_id,))
    try:
        cur_th.execute("DELETE FROM thread_realtime_items WHERE thread_id = ?", (tgt_id,))
    except Exception:
        pass

    t_cols = [
        "thread_id",
        "turn_id",
        "rollout_ordinal",
        "status",
        "error_json",
        "started_at",
        "completed_at",
        "duration_ms",
        "first_user_item_id",
        "final_agent_item_id",
        "rollout_byte_offset",
        "rollout_end_ordinal",
        "rollout_end_byte_offset",
    ]
    sql_t = f"INSERT OR REPLACE INTO thread_turns ({','.join(t_cols)}) VALUES ({','.join(['?'] * len(t_cols))})"
    for t in turns_meta:
        cur_th.execute(sql_t, [t[c] for c in t_cols])

    i_cols = [
        "thread_id",
        "turn_id",
        "item_id",
        "rollout_ordinal",
        "created_at_ms",
        "item_json",
        "item_type",
        "updated_at_ordinal",
    ]
    sql_i = f"INSERT OR REPLACE INTO thread_items ({','.join(i_cols)}) VALUES ({','.join(['?'] * len(i_cols))})"
    for item in items_meta:
        cur_th.execute(sql_i, [item[c] for c in i_cols])

    cur_th.execute(
        "INSERT OR REPLACE INTO thread_history_projection_state (thread_id, next_rollout_byte_offset, next_rollout_ordinal) VALUES (?, ?, ?)",
        (tgt_id, curr_offset, curr_ord),
    )
    conn_th.commit()
    conn_th.close()

    # Update state_5.sqlite
    with sqlite3.connect(paths.state_db, timeout=10.0) as conn_s:
        conn_s.cursor().execute(
            "UPDATE threads SET rollout_path = ?, updated_at = ?, updated_at_ms = ?, recency_at = ?, recency_at_ms = ?, history_mode = ? WHERE id = ?",
            (tgt_rollout, now_ts, now_ms, now_ts, now_ms, "paginated", tgt_id),
        )
        conn_s.commit()

    # Update mapping last_synced
    from .mapping import get_all_pairs, update_last_synced

    pairs = get_all_pairs(paths.mapping_db, active_only=False)
    for p in pairs:
        pid, _, o_id, d_id = p[0], p[1], p[2], p[3]
        if (src_id in (o_id, d_id)) and (tgt_id in (o_id, d_id)):
            update_last_synced(paths.mapping_db, pid)

    print(
        f"[SUCCESS] Full-fidelity rebuild completed: [{tgt_name or tgt_id[:8]}] was cleanly rebuilt with {len(turns_meta)} turns from [{src_name or src_id[:8]}]!"
    )
    return True


def overwrite_all_mapped_pairs(codex_home, target_provider=None, force=False):
    """Rebuild all registered session pairs using full convert overwrite from active source."""
    paths = CodexPaths(codex_home)
    from .mapping import get_all_pairs

    pairs = get_all_pairs(paths.mapping_db, active_only=True)
    if not pairs:
        print("[i] No mapped session pairs found to overwrite.")
        return True

    # Detect current provider from config.toml
    cur_prov = "openai"
    if os.path.exists(paths.config_toml):
        import re

        try:
            m = re.search(
                r'(?m)^model_provider\s*=\s*"([^"]+)"',
                open(paths.config_toml, "r", encoding="utf-8").read(),
            )
            if m:
                cur_prov = m.group(1).lower().strip()
        except Exception:
            pass

    from .activity import assert_no_running_sessions
    if not force and not assert_no_running_sessions(codex_home, force=force):
        return False

    tgt_prov = target_provider or "deepseek"
    src_prov = "openai" if tgt_prov == "deepseek" else "deepseek"
    print(
        f"[*] Full-fidelity overwrite for all {len(pairs)} pair(s) (Source: {src_prov.upper()} -> Target: {tgt_prov.upper()})..."
    )
    for p in pairs:
        _, name, o_id, d_id = p[0], p[1], p[2], p[3]
        src_id = o_id if tgt_prov == "deepseek" else d_id
        dst_id = d_id if tgt_prov == "deepseek" else o_id
        print(f"\n--- Overwriting [{name}] ({src_id[:8]} -> {dst_id[:8]}) ---")
        overwrite_target_from_source(src_id, dst_id, codex_home, force=True)
    return True

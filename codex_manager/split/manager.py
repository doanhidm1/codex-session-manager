import os
import re
import sqlite3
from typing import Optional

from ..config import CodexPaths, get_default_codex_home
from ..db import resolve_thread
from ..mapping import get_all_pairs
from .engine import split_single_thread


def split_thread(
    thread_id_or_name: str,
    keep_turns: int = 500,
    codex_home: Optional[str] = None,
    pair_aware: bool = True,
) -> bool:
    """
    Split a thread and optionally its counterpart in the pair.
    """
    if not codex_home:
        codex_home = get_default_codex_home()
    paths = CodexPaths(codex_home)

    row = resolve_thread(thread_id_or_name, codex_home)
    if not row:
        print(f"[-] Could not resolve thread '{thread_id_or_name}'")
        return False
    tid = row[0]

    # Check pair awareness
    counterpart_id = None
    if pair_aware:
        try:
            pairs = get_all_pairs(paths.mapping_db)
            for p in pairs:
                _, _, o_id, d_id, _, _, _ = p
                if tid == o_id:
                    counterpart_id = d_id
                    break
                elif tid == d_id:
                    counterpart_id = o_id
                    break
        except Exception:
            pass

    # Split target
    ok, msg = split_single_thread(tid, keep_turns=keep_turns, paths=paths)
    if not ok:
        print(f"[-] Split failed for {tid[:8]}: {msg}")
        return False
    print(f"[*] {msg}")

    # Split counterpart if exists
    if pair_aware and counterpart_id:
        print(f"[*] Found counterpart session {counterpart_id[:8]} in pair. Checking counterpart...")
        c_ok, c_msg = split_single_thread(counterpart_id, keep_turns=keep_turns, paths=paths)
        if c_ok:
            print(f"[*] Counterpart {counterpart_id[:8]}: {c_msg}")
        else:
            print(f"[-] Note: counterpart split returned: {c_msg}")

    return True


def check_and_auto_split(codex_home: Optional[str] = None) -> None:
    """
    Check if auto_split_turns is configured in config.toml or manager settings.
    If so, auto-splits any active session pairs exceeding the threshold.
    """
    if not codex_home:
        codex_home = get_default_codex_home()
    paths = CodexPaths(codex_home)

    threshold = 0
    # Read config.toml
    if os.path.exists(paths.config_toml):
        try:
            try:
                import tomllib

                with open(paths.config_toml, "rb") as f:
                    cfg = tomllib.load(f)
                threshold = cfg.get("sessions", {}).get("auto_split_turns", 0) or cfg.get("auto_split_turns", 0)
            except Exception:
                import toml

                cfg = toml.load(paths.config_toml)
                threshold = cfg.get("sessions", {}).get("auto_split_turns", 0) or cfg.get("auto_split_turns", 0)
        except Exception:
            pass
        if not threshold:
            try:
                with open(paths.config_toml, "r", encoding="utf-8") as f:
                    txt = f.read()
                m = re.search(r"auto_split_turns\s*=\s*(\d+)", txt)
                if m:
                    threshold = int(m.group(1))
            except Exception:
                pass

    if threshold <= 0:
        return

    conn_th = sqlite3.connect(paths.th_db, timeout=5.0)
    cth = conn_th.cursor()

    pairs = get_all_pairs(paths.mapping_db)
    for p in pairs:
        _pair_id, name, o_id, d_id, _cat, _lsync, _is_act = p
        for tid in (o_id, d_id):
            if not tid:
                continue
            cnt = cth.execute("SELECT count(*) FROM thread_turns WHERE thread_id = ?", (tid,)).fetchone()[0]
            if cnt > threshold:
                print(f"[*] Auto-split triggered for [{name}] ({tid[:8]} has {cnt} turns > {threshold})")
                split_single_thread(tid, keep_turns=threshold, paths=paths)

    conn_th.close()

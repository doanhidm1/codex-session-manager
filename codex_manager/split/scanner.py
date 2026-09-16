import json
from typing import Any, Dict, List


def scan_rollout_offsets(fpath: str) -> List[Dict[str, Any]]:
    """Scan a rollout file in binary mode and compute exact byte offsets for all turns."""
    turns: List[Dict[str, Any]] = []
    with open(fpath, "rb") as f:
        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                break
            end_pos = f.tell()
            try:
                obj = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if obj.get("type") == "event_msg":
                p = obj.get("payload", {})
                if isinstance(p, dict) and p.get("type") == "task_started":
                    t_id = p.get("turn_id")
                    ord_val = obj.get("ordinal")
                    turns.append(
                        {
                            "turn_id": t_id,
                            "ordinal": ord_val,
                            "start_offset": pos,
                            "start_end_offset": end_pos,
                            "last_ordinal": ord_val,
                            "last_offset": end_pos,
                        }
                    )
                elif turns:
                    turns[-1]["last_ordinal"] = obj.get("ordinal", turns[-1]["last_ordinal"])
                    turns[-1]["last_offset"] = end_pos

    for i in range(len(turns)):
        t = turns[i]
        if i + 1 < len(turns):
            t["end_byte_offset"] = turns[i + 1]["start_offset"]
            t["end_ordinal"] = turns[i + 1]["ordinal"] - 1
        else:
            t["end_byte_offset"] = t["last_offset"]
            t["end_ordinal"] = t["last_ordinal"]

    return turns

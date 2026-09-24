"""
Rollout file audit and repair module.
Detects and repairs truncated UTF-8 sequences, incomplete JSON lines,
and syntax corruptions in Codex rollout .jsonl files.
"""

import json
import logging
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("codex_manager.repair.rollout")

TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def sanitize_tool_name(name: Optional[str]) -> str:
    """
    Sanitizes tool/function call names to conform to OpenAI/DeepSeek API constraints:
    Expected pattern: ^[a-zA-Z0-9_-]+$
    Replaces any invalid characters (e.g. colons '::', spaces, dots) with underscores '_'.
    """
    if not name:
        return "unnamed_tool"
    s = name.replace("::", "__")
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", s)
    return cleaned or "unnamed_tool"


def audit_rollout_file(file_path: str) -> Dict[str, Any]:
    """
    Audits a rollout JSONL file line-by-line.
    Returns audit result containing status, line counts, and details of any broken lines.
    """
    if not os.path.exists(file_path):
        return {"exists": False, "valid": False, "error": f"File not found: {file_path}"}

    broken_lines: List[Dict[str, Any]] = []
    total_lines = 0

    with open(file_path, "rb") as f:
        for idx, raw_line in enumerate(f):
            total_lines += 1
            # Check 1: UTF-8 decoding
            try:
                text_line = raw_line.decode("utf-8")
            except UnicodeDecodeError as ue:
                broken_lines.append(
                    {
                        "line_index": idx,
                        "error_type": "UnicodeDecodeError",
                        "details": str(ue),
                        "raw_sample": raw_line[:120].hex(),
                    }
                )
                continue

            # Check 2: JSON parsing
            stripped = text_line.strip()
            if not stripped:
                continue

            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as je:
                broken_lines.append(
                    {
                        "line_index": idx,
                        "error_type": "JSONDecodeError",
                        "details": str(je),
                        "text_sample": stripped[:120],
                    }
                )
                continue

            # Check 3: Tool name validity (OpenAI schema requirement: ^[a-zA-Z0-9_-]+$)
            if isinstance(data, dict) and data.get("type") == "response_item":
                payload = data.get("payload")
                if isinstance(payload, dict):
                    ptype = payload.get("type")
                    if ptype in ("function_call", "custom_tool_call"):
                        pname = payload.get("name")
                        if pname and not TOOL_NAME_PATTERN.match(pname):
                            broken_lines.append(
                                {
                                    "line_index": idx,
                                    "error_type": "InvalidToolName",
                                    "details": f"Tool name {pname!r} does not match ^[a-zA-Z0-9_-]+$",
                                    "text_sample": stripped[:120],
                                    "invalid_name": pname,
                                }
                            )
                    elif ptype in ("function_call_output", "custom_tool_call_output"):
                        pname = payload.get("name")
                        if pname and not TOOL_NAME_PATTERN.match(pname):
                            broken_lines.append(
                                {
                                    "line_index": idx,
                                    "error_type": "InvalidToolName",
                                    "details": f"Tool output name {pname!r} does not match ^[a-zA-Z0-9_-]+$",
                                    "text_sample": stripped[:120],
                                    "invalid_name": pname,
                                }
                            )

    return {
        "exists": True,
        "valid": len(broken_lines) == 0,
        "file_path": file_path,
        "total_lines": total_lines,
        "broken_count": len(broken_lines),
        "broken_lines": broken_lines,
    }


def repair_rollout_file(file_path: str, backup: bool = True) -> Tuple[bool, str, List[int]]:
    """
    Repairs broken lines in a rollout file.
    Creates a backup copy if backup=True.
    Returns (success, message, repaired_line_indices).
    """
    audit = audit_rollout_file(file_path)
    if not audit["exists"]:
        return False, audit["error"], []

    if audit["valid"]:
        return True, "Rollout file is already 100% valid. No repairs needed.", []

    broken_indices = {b["line_index"] for b in audit["broken_lines"]}
    logger.info("Found %d broken lines in %s to repair: %s", len(broken_indices), file_path, broken_indices)

    if backup:
        backup_path = f"{file_path}.bak_repair_{int(time.time())}"
        shutil.copy2(file_path, backup_path)
        logger.info("Backup created at: %s", backup_path)

    repaired_lines: List[bytes] = []
    fixed_indices: List[int] = []

    with open(file_path, "rb") as f:
        raw_lines = f.readlines()

    for idx, raw_line in enumerate(raw_lines):
        if idx not in broken_indices:
            repaired_lines.append(raw_line)
            continue

        # Check if line has InvalidToolName
        invalid_tool_entry = next(
            (b for b in audit["broken_lines"] if b["line_index"] == idx and b.get("error_type") == "InvalidToolName"),
            None,
        )
        if invalid_tool_entry:
            text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            try:
                data = json.loads(text)
                payload = data.get("payload", {})
                if isinstance(payload, dict):
                    if "name" in payload and payload["name"]:
                        payload["name"] = sanitize_tool_name(payload["name"])
                    meta = payload.get("internal_chat_message_metadata_passthrough")
                    if isinstance(meta, dict) and "executed_tool_calls" in meta:
                        for etc in meta["executed_tool_calls"]:
                            if isinstance(etc, dict) and "name" in etc:
                                etc["name"] = sanitize_tool_name(etc["name"])
                fixed_text = json.dumps(data, ensure_ascii=False)
                repaired_lines.append((fixed_text + "\r\n").encode("utf-8"))
                fixed_indices.append(idx)
                logger.info("Sanitized tool name at line %d.", idx)
                continue
            except Exception as e:
                logger.warning("Failed to sanitize tool name at line %d: %s", idx, e)

        # Attempt safe repair of truncated line
        # 1. Decode with ignore or replace to inspect
        text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")

        # Case A: Truncated JSON object (e.g. truncated task_complete or event_msg)
        fixed_text = None
        if text.startswith("{"):
            # Try to complete incomplete string literals or close braces
            # Remove replacement character if at the very end
            candidate = text.rstrip("\ufffd")

            # Check if inside an unclosed string
            quote_count = candidate.count('"') - candidate.count('\\"')
            if quote_count % 2 != 0:
                candidate += '"'

            # Close open braces
            open_braces = candidate.count("{") - candidate.count("}")
            if open_braces > 0:
                candidate += "}" * open_braces

            try:
                json.loads(candidate)
                fixed_text = candidate
            except Exception:
                # If cannot be salvaged simply, check if it's a known event pattern
                if '"type": "event_msg"' in text and '"task_complete"' in text:
                    # Construct minimal valid task_complete line
                    fixed_text = json.dumps(
                        {
                            "timestamp": "2026-09-12T00:00:00Z",
                            "ordinal": idx,
                            "type": "event_msg",
                            "payload": {"type": "task_complete", "error": None},
                        },
                        ensure_ascii=False,
                    )

        if fixed_text is not None:
            repaired_lines.append((fixed_text + "\r\n").encode("utf-8"))
            fixed_indices.append(idx)
            logger.info("Repaired line %d successfully.", idx)
        else:
            logger.warning("Could not automatically repair line %d; preserving as comment.", idx)
            repaired_lines.append(raw_line)

    # Write repaired file
    with open(file_path, "wb") as f:
        f.writelines(repaired_lines)

    # Re-verify
    recheck = audit_rollout_file(file_path)
    if recheck["valid"]:
        return True, f"Successfully repaired {len(fixed_indices)} line(s). File is now valid.", fixed_indices
    else:
        return (
            False,
            f"Repaired {len(fixed_indices)} line(s), but {recheck['broken_count']} issue(s) remain.",
            fixed_indices,
        )

import gc
import json
import os
import sqlite3
import sys
import tempfile
import unittest

# Ensure parent directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_manager.repair.index_sync import sync_session_offsets
from codex_manager.repair.rollout_repair import audit_rollout_file, repair_rollout_file


class TestRolloutRepair(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        gc.collect()
        self.temp_dir.cleanup()

    def test_audit_clean_file(self):
        fpath = os.path.join(self.temp_dir.name, "clean.jsonl")
        records = [
            {"type": "session_meta", "payload": {"id": "test"}},
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "t1"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "t1"},
            },
        ]
        with open(fpath, "wb") as f:
            for r in records:
                f.write(json.dumps(r).encode("utf-8") + b"\n")

        audit = audit_rollout_file(fpath)
        self.assertTrue(audit["valid"])
        self.assertEqual(audit["total_lines"], 3)
        self.assertEqual(len(audit["broken_lines"]), 0)

    def test_repair_corrupted_line(self):
        fpath = os.path.join(self.temp_dir.name, "corrupt.jsonl")
        # Write 2 valid lines, then a truncated line with broken UTF-8, then a valid line
        valid1 = json.dumps({"type": "session_meta"}).encode("utf-8") + b"\n"
        valid2 = (
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "t1"},
                }
            ).encode("utf-8")
            + b"\n"
        )
        corrupted = b'{"type": "response_item", "content": "test truncated \xe1\n'  # truncated mid-byte
        valid3 = (
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "t1"},
                }
            ).encode("utf-8")
            + b"\n"
        )

        with open(fpath, "wb") as f:
            f.write(valid1)
            f.write(valid2)
            f.write(corrupted)
            f.write(valid3)

        # Audit should flag the corruption
        audit = audit_rollout_file(fpath)
        self.assertFalse(audit["valid"])
        self.assertEqual(len(audit["broken_lines"]), 1)
        self.assertEqual(audit["broken_lines"][0]["line_index"], 2)

        # Repair file
        ok, msg, fixed_lines = repair_rollout_file(fpath, backup=False)
        self.assertTrue(ok, msg)
        self.assertEqual(len(fixed_lines), 1)

        # Re-audit should now be completely clean
        post_audit = audit_rollout_file(fpath)
        self.assertTrue(post_audit["valid"])
        self.assertEqual(len(post_audit["broken_lines"]), 0)


class TestIndexSync(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.temp_dir.name, "thread_history_1.sqlite")
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE thread_turns (
                    thread_id TEXT NOT NULL,
                    turn_id TEXT PRIMARY KEY,
                    rollout_ordinal INTEGER,
                    status TEXT,
                    error_json TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_ms INTEGER,
                    first_user_item_id TEXT,
                    final_agent_item_id TEXT,
                    rollout_byte_offset INTEGER,
                    rollout_end_ordinal INTEGER,
                    rollout_end_byte_offset INTEGER
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE thread_history_projection_state (
                    thread_id TEXT PRIMARY KEY,
                    next_rollout_byte_offset INTEGER,
                    next_rollout_ordinal INTEGER
                )
                """
            )
            conn.commit()

    def tearDown(self):
        gc.collect()
        self.temp_dir.cleanup()

    def test_sync_session_offsets(self):
        tid = "thread-xyz"
        fpath = os.path.join(self.temp_dir.name, "rollout.jsonl")

        with open(fpath, "wb") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": tid}}).encode("utf-8") + b"\n")
            f.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "ordinal": 1,
                        "payload": {"type": "task_started", "turn_id": "turn-1"},
                    }
                ).encode("utf-8")
                + b"\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "ordinal": 2,
                        "payload": {"content": "hello"},
                    }
                ).encode("utf-8")
                + b"\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "ordinal": 3,
                        "payload": {"type": "task_complete", "turn_id": "turn-1"},
                    }
                ).encode("utf-8")
                + b"\n"
            )

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO thread_turns (thread_id, turn_id, rollout_ordinal, rollout_byte_offset) VALUES (?, 'turn-1', 0, 0)",
                (tid,),
            )
            cur.execute(
                "INSERT INTO thread_history_projection_state (thread_id, next_rollout_byte_offset, next_rollout_ordinal) VALUES (?, 0, 0)",
                (tid,),
            )
            conn.commit()

        res = sync_session_offsets(tid, fpath, self.db_path)
        self.assertTrue(res["success"])
        self.assertEqual(res["updated_turns"], 1)
        self.assertGreater(res["total_bytes"], 0)

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT rollout_byte_offset, rollout_end_byte_offset, rollout_ordinal, rollout_end_ordinal FROM thread_turns WHERE turn_id = 'turn-1'"
            ).fetchone()
            self.assertGreater(row[0], 0)  # task_started offset is > 0
            self.assertGreater(row[1], row[0])
            self.assertEqual(row[2], 1)
            self.assertEqual(row[3], 3)


if __name__ == "__main__":
    unittest.main()

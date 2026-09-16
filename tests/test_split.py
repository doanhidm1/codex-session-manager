import json
import os
import sqlite3
import sys
import tempfile
import unittest

# Ensure parent directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_manager.config import CodexPaths
from codex_manager.split.engine import split_single_thread
from codex_manager.split.scanner import scan_rollout_offsets


class TestSplitModule(unittest.TestCase):
    def setUp(self):
        import gc

        gc.collect()
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = self.temp_dir.name
        self.paths = CodexPaths(self.home)

        # Create state_5.sqlite
        with sqlite3.connect(self.paths.state_db) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    name TEXT,
                    archived INTEGER DEFAULT 0,
                    rollout_path TEXT,
                    created_at TEXT,
                    created_at_ms INTEGER,
                    updated_at TEXT,
                    originator TEXT,
                    cli_version TEXT,
                    cwd TEXT,
                    model_provider TEXT,
                    model TEXT,
                    thread_source TEXT,
                    history_mode TEXT,
                    first_user_message TEXT,
                    preview TEXT,
                    is_pinned INTEGER DEFAULT 0,
                    section_position TEXT
                )
                """
            )
            conn.commit()

        # Create thread_history_1.sqlite
        with sqlite3.connect(self.paths.th_db) as conn:
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
                CREATE TABLE thread_items (
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    item_id TEXT PRIMARY KEY,
                    rollout_ordinal INTEGER,
                    created_at_ms INTEGER,
                    item_json TEXT,
                    item_type TEXT,
                    updated_at_ordinal INTEGER
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
        import gc

        gc.collect()
        self.temp_dir.cleanup()

    def test_scan_rollout_offsets(self):
        fpath = os.path.join(self.home, "sample_rollout.jsonl")
        with open(fpath, "wb") as f:
            f.write(json.dumps({"type": "session_meta", "ordinal": 0}).encode("utf-8") + b"\n")
            f.write(
                json.dumps(
                    {"type": "event_msg", "ordinal": 1, "payload": {"type": "task_started", "turn_id": "turn-1"}}
                ).encode("utf-8")
                + b"\n"
            )
            f.write(
                json.dumps(
                    {"type": "event_msg", "ordinal": 2, "payload": {"type": "task_complete", "turn_id": "turn-1"}}
                ).encode("utf-8")
                + b"\n"
            )
            f.write(
                json.dumps(
                    {"type": "event_msg", "ordinal": 3, "payload": {"type": "task_started", "turn_id": "turn-2"}}
                ).encode("utf-8")
                + b"\n"
            )
            f.write(
                json.dumps(
                    {"type": "event_msg", "ordinal": 4, "payload": {"type": "task_complete", "turn_id": "turn-2"}}
                ).encode("utf-8")
                + b"\n"
            )

        turns = scan_rollout_offsets(fpath)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["turn_id"], "turn-1")
        self.assertEqual(turns[1]["turn_id"], "turn-2")
        self.assertLess(turns[0]["start_offset"], turns[1]["start_offset"])

    def test_split_single_thread(self):
        tid = "thread-to-split"
        rollout_path = os.path.join(self.home, "thread.jsonl")

        # Create rollout with 4 turns
        with open(rollout_path, "wb") as f:
            f.write(json.dumps({"type": "session_meta", "ordinal": 0, "payload": {"id": tid}}).encode("utf-8") + b"\n")
            for i in range(1, 5):
                t_id = f"turn-{i}"
                f.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "ordinal": i * 2 - 1,
                            "payload": {"type": "task_started", "turn_id": t_id},
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
                f.write(
                    json.dumps(
                        {"type": "event_msg", "ordinal": i * 2, "payload": {"type": "task_complete", "turn_id": t_id}}
                    ).encode("utf-8")
                    + b"\n"
                )

        # Insert metadata into SQLite
        with sqlite3.connect(self.paths.state_db) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO threads (id, title, name, rollout_path, created_at, originator, cli_version, cwd, model_provider)
                VALUES (?, 'My Session', 'My Session', ?, '2026-09-01T00:00:00Z', 'Codex', '1.0', 'C:/cwd', 'openai')
                """,
                (tid, rollout_path),
            )
            conn.commit()

        with sqlite3.connect(self.paths.th_db) as conn:
            cur = conn.cursor()
            for i in range(1, 5):
                t_id = f"turn-{i}"
                cur.execute(
                    """
                    INSERT INTO thread_turns (thread_id, turn_id, rollout_ordinal, status)
                    VALUES (?, ?, ?, 'completed')
                    """,
                    (tid, t_id, i * 2 - 1),
                )
            conn.commit()

        # Split: keep 2 turns, archive 2 turns
        ok, msg = split_single_thread(tid, keep_turns=2, paths=self.paths)
        self.assertTrue(ok, msg)

        # Verify state_db now has 2 threads: original active + new archived
        with sqlite3.connect(self.paths.state_db) as conn:
            cur = conn.cursor()
            rows = cur.execute("SELECT id, archived, title FROM threads").fetchall()
            self.assertEqual(len(rows), 2)
            active_row = [r for r in rows if r[0] == tid][0]
            archive_row = [r for r in rows if r[0] != tid][0]
            self.assertEqual(active_row[1], 0)
            self.assertEqual(archive_row[1], 1)
            self.assertIn("Archive", archive_row[2])

        # Verify turns in th_db
        with sqlite3.connect(self.paths.th_db) as conn:
            cur = conn.cursor()
            active_turns = cur.execute("SELECT turn_id FROM thread_turns WHERE thread_id = ?", (tid,)).fetchall()
            archive_turns = cur.execute(
                "SELECT turn_id FROM thread_turns WHERE thread_id = ?", (archive_row[0],)
            ).fetchall()
            self.assertEqual(len(active_turns), 2)
            self.assertEqual(len(archive_turns), 2)
            self.assertEqual([t[0] for t in active_turns], ["turn-3", "turn-4"])
            self.assertEqual([t[0] for t in archive_turns], ["turn-1", "turn-2"])


if __name__ == "__main__":
    unittest.main()

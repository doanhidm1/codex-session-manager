import os
import sqlite3
import tempfile
import unittest

from codex_manager.goals import (
    get_goals_db_path,
    get_thread_goal,
    sync_goals,
)
from codex_manager.mapping import init_mapping_db, register_pair


def exec_sql(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur.fetchall()
    finally:
        conn.close()


class TestGoalsSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.codex_home = self.temp_dir.name
        self.goals_db = get_goals_db_path(self.codex_home)
        self.mapping_db = os.path.join(self.codex_home, "session_manager.sqlite")

        # Initialize mapping DB and register a test pair
        init_mapping_db(self.mapping_db)
        register_pair(
            self.mapping_db,
            "test_pair",
            "openai_thread_12345",
            "deepseek_thread_67890",
        )

        # Initialize goals_1.sqlite with schema
        exec_sql(
            self.goals_db,
            """
            CREATE TABLE thread_goals (
                thread_id TEXT PRIMARY KEY NOT NULL,
                goal_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'active', 'paused', 'blocked', 'usage_limited', 'budget_limited', 'complete'
                )),
                token_budget INTEGER,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                time_used_seconds INTEGER NOT NULL DEFAULT 0,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )
            """,
        )
        exec_sql(
            self.goals_db,
            """
            CREATE TABLE thread_goal_continuation_deferrals (
                thread_id TEXT PRIMARY KEY NOT NULL REFERENCES thread_goals(thread_id) ON DELETE CASCADE
            )
            """,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sync_goals_from_deepseek_to_openai(self):
        exec_sql(
            self.goals_db,
            """
            INSERT INTO thread_goals (thread_id, goal_id, objective, status, token_budget, tokens_used, time_used_seconds, created_at_ms, updated_at_ms)
            VALUES ('deepseek_thread_67890', 'goal_abc', 'tiếp tục theo plan', 'blocked', 50000, 1200, 300, 1000, 2000)
            """,
        )

        ok, msg, count = sync_goals(self.codex_home)
        self.assertTrue(ok)
        self.assertEqual(count, 1)

        oa_goal = get_thread_goal("openai_thread_12345", self.codex_home)
        self.assertIsNotNone(oa_goal)
        self.assertEqual(oa_goal["objective"], "tiếp tục theo plan")
        self.assertEqual(oa_goal["status"], "blocked")
        self.assertEqual(oa_goal["time_used_seconds"], 300)

    def test_sync_goals_from_openai_to_deepseek(self):
        exec_sql(
            self.goals_db,
            """
            INSERT INTO thread_goals (thread_id, goal_id, objective, status, token_budget, tokens_used, time_used_seconds, created_at_ms, updated_at_ms)
            VALUES ('openai_thread_12345', 'goal_xyz', 'refactor tests', 'active', 100000, 500, 120, 3000, 4000)
            """,
        )

        ok, msg, count = sync_goals(self.codex_home)
        self.assertTrue(ok)
        self.assertEqual(count, 1)

        ds_goal = get_thread_goal("deepseek_thread_67890", self.codex_home)
        self.assertIsNotNone(ds_goal)
        self.assertEqual(ds_goal["objective"], "refactor tests")
        self.assertEqual(ds_goal["status"], "active")

    def test_sync_goals_newer_updated_at_wins(self):
        exec_sql(
            self.goals_db,
            """
            INSERT INTO thread_goals (thread_id, goal_id, objective, status, token_budget, tokens_used, time_used_seconds, created_at_ms, updated_at_ms)
            VALUES ('openai_thread_12345', 'goal_1', 'old objective', 'active', 10000, 100, 50, 1000, 2000),
                   ('deepseek_thread_67890', 'goal_1', 'new objective', 'complete', 10000, 800, 450, 1000, 5000)
            """,
        )

        ok, msg, count = sync_goals(self.codex_home)
        self.assertTrue(ok)
        self.assertEqual(count, 1)

        oa_goal = get_thread_goal("openai_thread_12345", self.codex_home)
        self.assertEqual(oa_goal["objective"], "new objective")
        self.assertEqual(oa_goal["status"], "complete")
        self.assertEqual(oa_goal["updated_at_ms"], 5000)

    def test_sync_goals_deferrals(self):
        exec_sql(
            self.goals_db,
            """
            INSERT INTO thread_goals (thread_id, goal_id, objective, status, token_budget, tokens_used, time_used_seconds, created_at_ms, updated_at_ms)
            VALUES ('deepseek_thread_67890', 'goal_def', 'deferral test', 'active', 10000, 100, 50, 1000, 2000)
            """,
        )
        exec_sql(
            self.goals_db,
            """
            INSERT INTO thread_goal_continuation_deferrals (thread_id)
            VALUES ('deepseek_thread_67890')
            """,
        )

        ok, msg, count = sync_goals(self.codex_home)
        self.assertTrue(ok)

        rows = exec_sql(
            self.goals_db,
            "SELECT 1 FROM thread_goal_continuation_deferrals WHERE thread_id = 'openai_thread_12345'",
        )
        self.assertTrue(len(rows) > 0)

    def test_missing_goals_db_handled_gracefully(self):
        empty_dir = tempfile.TemporaryDirectory()
        try:
            ok, msg, count = sync_goals(empty_dir.name)
            self.assertTrue(ok)
            self.assertEqual(count, 0)
        finally:
            empty_dir.cleanup()


if __name__ == "__main__":
    unittest.main()

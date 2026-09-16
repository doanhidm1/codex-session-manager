import gc
import json
import os
import sqlite3
import sys
import tempfile
import unittest

# Ensure parent directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_manager.proxy.adapter import TOOL_DIRECTIVE, USER_TOOL_REMINDER, adapt_responses_body
from codex_manager.proxy.daemon import get_proxy_status
from codex_manager.proxy.reconciler import reconcile_delegation_turns


class TestProxyAdapter(unittest.TestCase):
    def test_tool_directive_injected(self):
        body = {
            "model": "deepseek-chat",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "base dev prompt"}],
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello world!"}]},
            ],
            "stream": True,
        }
        raw = json.dumps(body).encode("utf-8")
        adapted_raw = adapt_responses_body(raw)
        adapted_body = json.loads(adapted_raw.decode("utf-8"))

        # Developer message contains TOOL_DIRECTIVE
        dev_msg = adapted_body["input"][0]
        self.assertEqual(dev_msg["role"], "developer")
        self.assertEqual(len(dev_msg["content"]), 2)
        self.assertEqual(dev_msg["content"][1]["text"], TOOL_DIRECTIVE)

        # User message contains reminder
        user_msg = adapted_body["input"][1]
        self.assertEqual(user_msg["role"], "user")
        self.assertIn(USER_TOOL_REMINDER, user_msg["content"][0]["text"])

    def test_historical_exec_neutralized(self):
        body = {
            "model": "deepseek-chat",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "exec",
                    "arguments": '{"input": "const r = await tools.exec_command({cmd: \\"hostname\\"});"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "name": "exec",
                    "output": "host1",
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Run next"}],
                },
            ],
        }
        raw = json.dumps(body).encode("utf-8")
        adapted_raw = adapt_responses_body(raw)
        adapted_body = json.loads(adapted_raw.decode("utf-8"))

        # Both function_call and function_call_output should be renamed to legacy_exec
        fc = [item for item in adapted_body["input"] if item.get("type") == "function_call"][0]
        fco = [item for item in adapted_body["input"] if item.get("type") == "function_call_output"][0]
        self.assertEqual(fc["name"], "legacy_exec")
        self.assertEqual(fco["name"], "legacy_exec")

    def test_exec_tool_filtered(self):
        body = {
            "model": "deepseek-chat",
            "tools": [
                {"type": "function", "name": "exec_command", "description": "Execute shell command"},
                {"type": "function", "name": "exec", "description": "Execute JavaScript"},
            ],
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Check"}]},
            ],
        }
        raw = json.dumps(body).encode("utf-8")
        adapted_raw = adapt_responses_body(raw)
        adapted_body = json.loads(adapted_raw.decode("utf-8"))

        tool_names = [t.get("name") for t in adapted_body["tools"]]
        self.assertIn("exec_command", tool_names)
        self.assertNotIn("exec", tool_names)

    def test_delegation_payload_converted_to_user_message(self):
        # DeepSeek Responses API receives unattached function_call_output without preceding function_call
        body = {
            "model": "deepseek-chat",
            "input": [
                {
                    "type": "function_call_output",
                    "output": "<codex_delegation sender='alpha'>Task instruction</codex_delegation>",
                },
            ],
            "stream": False,
        }
        raw = json.dumps(body).encode("utf-8")
        adapted_raw = adapt_responses_body(raw)
        self.assertNotEqual(raw, adapted_raw)

        adapted_body = json.loads(adapted_raw.decode("utf-8"))
        user_msgs = [item for item in adapted_body["input"] if item.get("role") == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertIn("Task instruction", user_msgs[0]["content"][0]["text"])

    def test_attached_function_call_output_preserved(self):
        # If preceding function_call has call_id, it is preserved
        body = {
            "model": "deepseek-chat",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "shell_execute",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "command success",
                },
            ],
        }
        raw = json.dumps(body).encode("utf-8")
        adapted_raw = adapt_responses_body(raw)
        adapted_body = json.loads(adapted_raw.decode("utf-8"))

        fco = [item for item in adapted_body["input"] if item.get("type") == "function_call_output"][0]
        self.assertEqual(fco["call_id"], "call_123")
        self.assertEqual(fco["output"], "command success")


class TestProxyReconciler(unittest.TestCase):
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
            conn.commit()

    def tearDown(self):
        gc.collect()
        self.temp_dir.cleanup()

    def test_reconcile_delegation_turns(self):
        # Insert a delegation turn where first_user_item_id is NULL
        tid = "thread-123"
        turn_id = "turn-456"
        fco_item_id = "fco_abc789"
        agent_item_id = "msg_agent_999"

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO thread_turns (thread_id, turn_id, rollout_ordinal, first_user_item_id, final_agent_item_id)
                VALUES (?, ?, 10, NULL, ?)
                """,
                (tid, turn_id, agent_item_id),
            )
            cur.execute(
                """
                INSERT INTO thread_items (thread_id, turn_id, item_id, rollout_ordinal, item_type, item_json)
                VALUES (?, ?, ?, 10, 'function_call_output', '{"type":"function_call_output","output":"<codex_delegation>hi</codex_delegation>"}')
                """,
                (tid, turn_id, fco_item_id),
            )
            cur.execute(
                """
                INSERT INTO thread_items (thread_id, turn_id, item_id, rollout_ordinal, item_type, item_json)
                VALUES (?, ?, ?, 11, 'agentMessage', '{"type":"agentMessage","content":[{"type":"text","text":"response"}]}')
                """,
                (tid, turn_id, agent_item_id),
            )
            conn.commit()

        # Run reconciler
        reconciled_count = reconcile_delegation_turns(self.db_path)
        self.assertEqual(reconciled_count, 1)

        # Verify first_user_item_id was updated to fco_item_id
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT first_user_item_id, final_agent_item_id FROM thread_turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            self.assertEqual(row[0], fco_item_id)
            self.assertEqual(row[1], agent_item_id)


class TestProxyDaemon(unittest.TestCase):
    def test_status_structure(self):
        status = get_proxy_status()
        self.assertIn("running", status)
        self.assertIn("port", status)
        self.assertIn("upstream", status)
        self.assertEqual(status["port"], 8765)


if __name__ == "__main__":
    unittest.main()

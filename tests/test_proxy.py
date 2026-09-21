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
        self.assertIn(TOOL_DIRECTIVE, dev_msg["content"][1]["text"])

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

    def test_thread_id_remapped_and_registry_injected(self):
        # Create a temp mapping db
        gc.collect()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            mapping_db = os.path.join(tmpdir, "session_manager.sqlite")
            with sqlite3.connect(mapping_db) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE session_pairs (
                        pair_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        openai_thread_id TEXT NOT NULL UNIQUE,
                        deepseek_thread_id TEXT NOT NULL UNIQUE,
                        created_at INTEGER NOT NULL,
                        is_active INTEGER DEFAULT 1
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO session_pairs (pair_id, name, openai_thread_id, deepseek_thread_id, created_at, is_active)
                    VALUES
                        ('p1', 'SaaS', '019f675a-28f8-73a3-a5a7-4d3f83560eef', '6f98dfb5-c214-4b40-b653-d5b6b888909c', 100, 1),
                        ('p2', 'Veo3', '01a07efa-153d-7d70-958c-96eee02279f2', 'dfc26a6f-8de5-449f-aaab-2a0f8578df3b', 100, 1)
                    """
                )
                conn.commit()

            body = {
                "model": "deepseek-chat",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Please check thread 01a07efa-153d-7d70-958c-96eee02279f2 and set self 019f675a-28f8-73a3-a5a7-4d3f83560eef",
                            }
                        ],
                    },
                ],
            }
            raw = json.dumps(body).encode("utf-8")
            adapted_raw = adapt_responses_body(raw, mapping_db_path=mapping_db)
            adapted_body = json.loads(adapted_raw.decode("utf-8"))

            # Verify historical IDs in user message were replaced
            user_text = adapted_body["input"][1]["content"][0]["text"]
            self.assertNotIn("01a07efa-153d-7d70-958c-96eee02279f2", user_text)
            self.assertIn("dfc26a6f-8de5-449f-aaab-2a0f8578df3b", user_text)
            self.assertNotIn("019f675a-28f8-73a3-a5a7-4d3f83560eef", user_text)
            self.assertIn("6f98dfb5-c214-4b40-b653-d5b6b888909c", user_text)

            # Verify developer directive contains active thread registry
            dev_msg = adapted_body["input"][0]
            dev_text = dev_msg["content"][0]["text"]
            self.assertIn("[ACTIVE DEEPSEEK THREAD REGISTRY]", dev_text)
            self.assertIn("SaaS (ds): 6f98dfb5-c214-4b40-b653-d5b6b888909c", dev_text)
            self.assertIn("Veo3 (ds): dfc26a6f-8de5-449f-aaab-2a0f8578df3b", dev_text)
            gc.collect()


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

    def test_reconcile_automations(self):
        from codex_manager.proxy.reconciler import reconcile_automations

        mapping_db = os.path.join(self.temp_dir.name, "session_manager.sqlite")
        with sqlite3.connect(mapping_db) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE session_pairs (
                    pair_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    openai_thread_id TEXT NOT NULL UNIQUE,
                    deepseek_thread_id TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    is_active INTEGER DEFAULT 1
                )
                """
            )
            cur.execute(
                """
                INSERT INTO session_pairs (pair_id, name, openai_thread_id, deepseek_thread_id, created_at, is_active)
                VALUES ('p1', 'SaaS', '019f675a-28f8-73a3-a5a7-4d3f83560eef', '6f98dfb5-c214-4b40-b653-d5b6b888909c', 100, 1)
                """
            )
            conn.commit()

        auto_dir = os.path.join(self.temp_dir.name, "automations", "my-heartbeat")
        os.makedirs(auto_dir, exist_ok=True)
        toml_file = os.path.join(auto_dir, "automation.toml")
        with open(toml_file, "w", encoding="utf-8") as f:
            f.write(
                'version = 1\nkind = "heartbeat"\ntarget_thread_id = "019f675a-28f8-73a3-a5a7-4d3f83560eef"\nprompt = "check"\n'
            )

        updated = reconcile_automations(
            automations_dir=os.path.join(self.temp_dir.name, "automations"),
            mapping_db_path=mapping_db,
        )
        self.assertEqual(updated, 1)

        with open(toml_file, "r", encoding="utf-8") as f:
            new_content = f.read()
        self.assertIn('target_thread_id = "6f98dfb5-c214-4b40-b653-d5b6b888909c"', new_content)
        self.assertNotIn("019f675a-28f8-73a3-a5a7-4d3f83560eef", new_content)


class TestProxyStreaming(unittest.TestCase):
    def test_sliding_window_replaces_boundary_split_uuid(self):
        oai = b"019f675a-28f8-73a3-a5a7-4d3f83560eef"
        ds = b"6f98dfb5-c214-4b40-b653-d5b6b888909c"
        replacements = [(oai, ds)]

        # Chunk 1 cuts UUID in half
        chunk1 = b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\\"targetThreadId\\":\\"019f675a-28f8'
        chunk2 = b'-73a3-a5a7-4d3f83560eef\\"}"}}]}}]}\n\n'

        chunks = [chunk1, chunk2]

        def stream_sim():
            buffer = b""
            overlap = 35
            for c in chunks:
                if replacements:
                    buffer += c
                    for oai_b, ds_b in replacements:
                        buffer = buffer.replace(oai_b, ds_b)
                    if len(buffer) > overlap:
                        to_yield = buffer[:-overlap]
                        buffer = buffer[-overlap:]
                        yield to_yield
            if buffer:
                for oai_b, ds_b in replacements:
                    buffer = buffer.replace(oai_b, ds_b)
                yield buffer

        result = b"".join(stream_sim())
        self.assertNotIn(oai, result)
        self.assertIn(ds, result)
        self.assertEqual(len(result), len(chunk1) + len(chunk2))

    def test_delegation_tool_adaptation_in_history(self):
        body = {
            "model": "deepseek-chat",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_grok_1",
                    "name": "send_message_to_thread",
                    "namespace": "codex_app",
                    "arguments": '{"threadId": "test-id", "prompt": "hi"}',
                },
                {
                    "type": "function_call",
                    "call_id": "call_grok_2",
                    "name": "mcp__codex_app__send_message_to_thread",
                    "arguments": '{"threadId": "test-id", "prompt": "hi"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_grok_1",
                    "name": "send_message_to_thread",
                    "namespace": "codex_app",
                    "output": "ok",
                },
            ],
        }
        raw = json.dumps(body).encode("utf-8")
        adapted_raw = adapt_responses_body(raw)
        adapted_body = json.loads(adapted_raw.decode("utf-8"))

        # Note: index 0 is the injected developer message with TOOL_DIRECTIVE
        fc1 = adapted_body["input"][1]
        self.assertEqual(fc1["namespace"], "mcp__codex_app")
        self.assertEqual(fc1["name"], "send_message_to_thread")

        fc2 = adapted_body["input"][2]
        self.assertEqual(fc2["namespace"], "mcp__codex_app")
        self.assertEqual(fc2["name"], "send_message_to_thread")

        fco1 = adapted_body["input"][3]
        self.assertEqual(fco1["namespace"], "mcp__codex_app")

    def test_delegation_stream_replacements(self):
        from codex_manager.proxy.server import DELEGATION_STREAM_REPLACEMENTS

        raw_chunk = b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"mcp__codex_app__send_message_to_thread","namespace":"codex_app"}}]}}]}\n\n'
        processed = raw_chunk
        for pat, repl in DELEGATION_STREAM_REPLACEMENTS:
            processed = processed.replace(pat, repl)

        self.assertNotIn(b'"namespace":"codex_app"', processed)
        self.assertIn(b'"namespace":"mcp__codex_app"', processed)
        self.assertNotIn(b'"name":"mcp__codex_app__send_message_to_thread"', processed)
        self.assertIn(b'"name":"send_message_to_thread"', processed)


class TestProxyDaemon(unittest.TestCase):
    def test_status_structure(self):
        status = get_proxy_status()
        self.assertIn("running", status)
        self.assertIn("port", status)
        self.assertIn("upstream", status)
        self.assertEqual(status["port"], 8765)


if __name__ == "__main__":
    unittest.main()

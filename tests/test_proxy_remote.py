import gzip
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_manager.proxy import server
from codex_manager.switch import PROXY_BASE_URL, switch_provider_settings


class TestRequestBodyDecoding(unittest.TestCase):
    BODY = json.dumps({"model": "deepseek-flash", "input": "hi"}).encode()

    def test_zstd_body_decoded(self):
        import zstandard

        self.assertEqual(server._decode_request_body(zstandard.ZstdCompressor().compress(self.BODY), "zstd"), self.BODY)

    def test_gzip_body_decoded(self):
        self.assertEqual(server._decode_request_body(gzip.compress(self.BODY), "gzip"), self.BODY)

    def test_plain_body_untouched(self):
        self.assertEqual(server._decode_request_body(self.BODY, ""), self.BODY)
        self.assertEqual(server._decode_request_body(self.BODY, "identity"), self.BODY)


class TestFlatMcpToolNames(unittest.TestCase):
    NS = {"mcp__linear", "mcp__codex_app", "mcp__codex"}

    def test_flat_name_split(self):
        buf = b'data: {"item":{"type":"function_call","name":"mcp__linear__get_issue","arguments":"{}"}}'
        self.assertEqual(
            server.split_flat_mcp_tool_names(buf, self.NS),
            b'data: {"item":{"type":"function_call","name":"get_issue","namespace":"mcp__linear","arguments":"{}"}}',
        )

    def test_spaced_json_and_longest_namespace(self):
        buf = b'{"name": "mcp__codex_app__read_thread"}'
        self.assertEqual(
            server.split_flat_mcp_tool_names(buf, self.NS),
            b'{"name": "read_thread", "namespace": "mcp__codex_app"}',
        )

    def test_unknown_or_already_split_untouched(self):
        for buf in (
            b'{"name":"mcp__unknown__tool"}',
            b'{"name":"get_issue","namespace":"mcp__linear"}',
            b'{"name":"mcp__linear"}',
            b'{"arguments":"{\\"name\\":\\"mcp__linear__get_issue\\"}"}',
        ):
            self.assertEqual(server.split_flat_mcp_tool_names(buf, self.NS), buf)

    def test_namespaces_from_request(self):
        body = json.dumps(
            {"tools": [{"type": "namespace", "name": "mcp__linear", "tools": []}, {"type": "function", "name": "exec_command"}]}
        ).encode()
        self.assertEqual(server._mcp_namespaces(body), {"mcp__linear"})


class TestToolOutputAdjacency(unittest.TestCase):
    def test_hook_message_moved_after_output(self):
        from codex_manager.proxy.adapter import keep_tool_outputs_adjacent

        call = {"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch"}
        hook = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "hook"}]}
        out = {"type": "custom_tool_call_output", "call_id": "c1", "output": "ok"}
        user = {"type": "message", "role": "user", "content": []}
        self.assertEqual(keep_tool_outputs_adjacent([call, hook, out, user]), [call, out, hook, user])

    def test_ordered_or_unanswered_untouched(self):
        from codex_manager.proxy.adapter import keep_tool_outputs_adjacent

        a = {"type": "function_call", "call_id": "a"}
        a_out = {"type": "function_call_output", "call_id": "a"}
        msg = {"type": "message", "role": "user"}
        ordered = [a, a_out, msg]
        self.assertIs(keep_tool_outputs_adjacent(ordered), ordered)
        unanswered = [{"type": "function_call", "call_id": "x"}, msg]
        self.assertIs(keep_tool_outputs_adjacent(unanswered), unanswered)


class TestPromptCacheStability(unittest.TestCase):
    @staticmethod
    def _adapt(items):
        from codex_manager.proxy.adapter import adapt_responses_body

        with tempfile.TemporaryDirectory() as d:
            body = json.dumps({"model": "deepseek-flash", "input": items}).encode()
            return json.loads(adapt_responses_body(body, mapping_db_path=os.path.join(d, "none.sqlite")))["input"]

    def test_earlier_items_unchanged_when_a_new_user_message_arrives(self):
        def user(t):
            return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}

        call = {"type": "function_call", "call_id": "c", "name": "exec_command", "arguments": "{}"}
        out = {"type": "function_call_output", "call_id": "c", "output": "ok"}
        first = self._adapt([user("a"), call, out])
        second = self._adapt([user("a"), call, out, user("b")])
        self.assertEqual(second[: len(first)], first)

    def test_images_replaced_with_text(self):
        from codex_manager.proxy.adapter import IMAGE_PLACEHOLDER

        img = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        call = {"type": "function_call", "call_id": "v", "name": "view_image", "arguments": "{}"}
        out = {"type": "function_call_output", "call_id": "v", "output": [img]}
        msg = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "see"}, dict(img)]}
        res = self._adapt([msg, call, out])
        self.assertNotIn("base64", json.dumps(res))
        self.assertIn(IMAGE_PLACEHOLDER, json.dumps(res))


class TestRequestLog(unittest.TestCase):
    def test_divergence_and_usage(self):
        from codex_manager.proxy import reqlog

        def body(texts, tools=None):
            items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]} for t in texts]
            return json.dumps({"model": "m", "prompt_cache_key": "conv-1", "input": items, "tools": tools or []}).encode()

        tools = [{"type": "namespace", "name": "mcp__chrome_devtools", "tools": [{"type": "function", "name": "click"}]}]
        first = reqlog.describe_request(body(["a", "b"], tools), {})
        self.assertIsNone(first["diverge"])
        self.assertIn("mcp__chrome_devtools", first["tools_bytes"])
        same_prefix = reqlog.describe_request(body(["a", "b", "c"], tools), {})
        self.assertIsNone(same_prefix["diverge"])
        changed = reqlog.describe_request(body(["a", "B", "c"], tools), {})
        self.assertEqual(changed["diverge"]["item_index"], 1)
        tail = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":10,"input_tokens_details":{"cached_tokens":8},"output_tokens":3}}}\n\n'
        self.assertEqual(reqlog.extract_usage(tail)["input_tokens_details"]["cached_tokens"], 8)


class TestToolStepEffort(unittest.TestCase):
    def test_tool_step_uses_low_effort(self):
        from codex_manager.proxy.adapter import adapt_responses_body

        items = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {"type": "function_call", "call_id": "c", "name": "exec_command", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c", "output": "ok"},
        ]
        with tempfile.TemporaryDirectory() as d:
            body = json.dumps({"model": "deepseek-flash", "input": items, "reasoning": {"effort": "max"}}).encode()
            out = json.loads(adapt_responses_body(body, mapping_db_path=os.path.join(d, "none.sqlite")))
        self.assertEqual(out["reasoning"]["effort"], "low")


class TestDeepSeekKey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = self.tmp.name
        server._key_cache.update(mtime=None, key=None)

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.old_home
        server._key_cache.update(mtime=None, key=None)
        self.tmp.cleanup()

    def test_key_read_from_provider_section(self):
        with open(os.path.join(self.tmp.name, "config.toml"), "w") as f:
            f.write('model_provider = "deepseek"\n\n[model_providers.deepseek]\nexperimental_bearer_token = "sk-ds"\n')
        self.assertEqual(server._deepseek_api_key(), "sk-ds")


class TestSwitchOpenAIBaseUrl(unittest.TestCase):
    def test_openai_base_url_toggled(self):
        with tempfile.TemporaryDirectory() as home:
            cfg = os.path.join(home, "config.toml")
            with open(cfg, "w") as f:
                f.write('model = "gpt-x"\nmodel_provider = "openai"\n\n[model_providers.deepseek]\nname = "deepseek"\n')
            switch_provider_settings(home, "deepseek")
            with open(cfg) as f:
                self.assertIn(f'openai_base_url = "{PROXY_BASE_URL}"', f.read())
            switch_provider_settings(home, "openai")
            with open(cfg) as f:
                self.assertNotIn("openai_base_url", f.read())


if __name__ == "__main__":
    unittest.main()

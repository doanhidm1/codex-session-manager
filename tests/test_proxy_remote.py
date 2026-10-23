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

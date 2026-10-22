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

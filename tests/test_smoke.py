import unittest
import os
import sys

# Ensure parent directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_manager.config import get_default_codex_home, CodexPaths
from codex_manager.db import get_connection
from codex_manager.rollout import make_wire_record
from codex_manager.mapping import get_all_pairs, auto_seed_existing_pairs
from codex_manager.switch import switch_provider

from codex_manager.provider import (
    is_supported_provider,
    assert_supported_provider,
    check_deepseek_config,
    save_provider_settings,
    get_last_provider_settings,
    update_config_toml
)

class TestCodexManagerSmoke(unittest.TestCase):
    def test_config_resolution(self):
        home = get_default_codex_home()
        self.assertTrue(bool(home))
        paths = CodexPaths(home)
        self.assertTrue(paths.state_db.endswith("state_5.sqlite"))
        self.assertTrue(paths.mapping_db.endswith("session_manager.sqlite"))
        self.assertTrue(paths.config_toml.endswith("config.toml"))

    def test_rollout_serialization(self):
        rec_str = make_wire_record("test_type", {"key": "value"}, 1)
        self.assertIn('"type": "test_type"', rec_str)
        self.assertIn('"ordinal": 1', rec_str)

    def test_database_connection(self):
        home = get_default_codex_home()
        paths = CodexPaths(home)
        if os.path.exists(paths.state_db):
            conn = get_connection(paths.state_db)
            cur = conn.cursor()
            cnt = cur.execute("SELECT count(*) FROM threads").fetchone()[0]
            conn.close()
            self.assertGreaterEqual(cnt, 0)

    def test_mapping_database(self):
        home = get_default_codex_home()
        paths = CodexPaths(home)
        auto_seed_existing_pairs(home)
        pairs = get_all_pairs(paths.mapping_db)
        self.assertIsInstance(pairs, list)

    def test_provider_support(self):
        self.assertTrue(is_supported_provider("deepseek"))
        self.assertTrue(is_supported_provider("openai"))
        self.assertFalse(is_supported_provider("anthropic"))
        self.assertFalse(is_supported_provider("gemini"))
        self.assertFalse(is_supported_provider("ollama"))
        with self.assertRaises(ValueError):
            assert_supported_provider("unsupported_provider")

    def test_provider_settings_persistence(self):
        home = get_default_codex_home()
        paths = CodexPaths(home)
        save_provider_settings(paths.mapping_db, "openai", "gpt-5.6-luna", "max")
        m, e = get_last_provider_settings(paths.mapping_db, "openai")
        self.assertEqual(m, "gpt-5.6-luna")
        self.assertEqual(e, "max")
        # Restore test settings
        save_provider_settings(paths.mapping_db, "openai", "gpt-5.6-terra", "high")

    def test_pair_registration(self):
        home = get_default_codex_home()
        paths = CodexPaths(home)
        from codex_manager.mapping import register_pair, remove_pair
        register_pair(paths.mapping_db, "UnitTestPair", "openai-fake-1", "deepseek-fake-1")
        pairs = get_all_pairs(paths.mapping_db, active_only=False)
        found = [p for p in pairs if p[1] == "UnitTestPair"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][2], "openai-fake-1")
        self.assertEqual(found[0][3], "deepseek-fake-1")
        remove_pair(paths.mapping_db, "UnitTestPair")

if __name__ == '__main__':
    unittest.main()

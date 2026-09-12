import os
import sys
import unittest

# Ensure parent directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_manager.config import CodexPaths, get_default_codex_home
from codex_manager.db import get_connection
from codex_manager.mapping import auto_seed_existing_pairs, get_all_pairs
from codex_manager.provider import (
    assert_supported_provider,
    get_last_provider_settings,
    is_supported_provider,
    save_provider_settings,
)
from codex_manager.rollout import make_wire_record


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

    def test_active_session_and_lock_detection(self):
        home = get_default_codex_home()
        from codex_manager.activity import assert_no_running_sessions, check_file_and_db_locks, detect_running_sessions

        is_clean, reason = check_file_and_db_locks(home)
        self.assertTrue(is_clean, f"Lock check failed: {reason}")
        running = detect_running_sessions(home, threshold_sec=60)
        self.assertIsInstance(running, list)
        self.assertTrue(assert_no_running_sessions(home))

    def test_pair_health_and_force_rebuild(self):
        home = get_default_codex_home()
        from codex_manager.pair_health import check_thread_health, validate_pair_for_sync

        # Non-existent thread should report unhealthy
        ok, reason, _ = check_thread_health("non-existent-thread-id", home)
        self.assertFalse(ok)
        self.assertIn("does not exist", reason)

        # Validating broken target without force should fail and indicate need for force
        # Use a registered pair ID with a broken fake target
        from codex_manager.mapping import get_all_pairs

        paths = CodexPaths(home)
        pairs = get_all_pairs(paths.mapping_db)
        if pairs:
            valid_src = pairs[0][2]
            valid_res, status = validate_pair_for_sync(
                valid_src, "fake-broken-target", "openai", "deepseek", home, force=False
            )
            self.assertFalse(valid_res)
            self.assertEqual(status, "target_broken_need_force")

    def test_sync_overwrite_import(self):
        from codex_manager.sync_overwrite import overwrite_all_mapped_pairs, overwrite_target_from_source

        self.assertTrue(callable(overwrite_target_from_source))
        self.assertTrue(callable(overwrite_all_mapped_pairs))


if __name__ == "__main__":
    unittest.main()

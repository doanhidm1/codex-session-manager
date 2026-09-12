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

class TestCodexManagerSmoke(unittest.TestCase):
    def test_config_resolution(self):
        home = get_default_codex_home()
        self.assertTrue(bool(home))
        paths = CodexPaths(home)
        self.assertTrue(paths.state_db.endswith("state_5.sqlite"))
        self.assertTrue(paths.mapping_db.endswith("session_manager.sqlite"))

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
        self.assertGreaterEqual(len(pairs), 1)

if __name__ == '__main__':
    unittest.main()

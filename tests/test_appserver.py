import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_manager import switch as switch_mod
from codex_manager.appserver import set_threads_archived
from codex_manager.config import CodexPaths
from codex_manager.provider import (
    get_last_provider_settings,
    model_matches_provider,
    save_provider_settings,
)

FAKE_APP_SERVER = """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    params = msg.get("params") or {}
    if "bad" in str(params.get("threadId", "")):
        out = {"id": msg["id"], "error": {"code": -1, "message": "no rollout found"}}
    else:
        out = {"id": msg["id"], "result": {}}
    print(json.dumps(out), flush=True)
"""


def make_codex_home(tmp, with_binary=True):
    home = os.path.join(tmp, ".codex")
    os.makedirs(home, exist_ok=True)
    if with_binary:
        bin_dir = os.path.join(home, "packages", "app-server-daemon", "current", "bin")
        os.makedirs(bin_dir)
        path = os.path.join(bin_dir, "codex")
        with open(path, "w", encoding="utf-8") as f:
            f.write(FAKE_APP_SERVER)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return home


class TestSetThreadsArchived(unittest.TestCase):
    def test_returns_only_successful_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_codex_home(tmp)
            done = set_threads_archived(home, archive_ids=["a1", "bad1"], unarchive_ids=["u1", "bad2"], timeout=15)
            self.assertEqual(done, {"a1", "u1"})

    def test_nothing_to_do_returns_empty_without_spawning(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_codex_home(tmp, with_binary=False)
            with mock.patch("codex_manager.appserver.subprocess.Popen") as popen:
                self.assertEqual(set_threads_archived(home), set())
                popen.assert_not_called()

    def test_missing_binary_is_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_codex_home(tmp, with_binary=False)
            with mock.patch("codex_manager.appserver.shutil.which", return_value=None):
                self.assertEqual(set_threads_archived(home, archive_ids=["a1"]), set())


class TestProviderModelGuard(unittest.TestCase):
    def test_model_matches_provider(self):
        self.assertTrue(model_matches_provider("deepseek", "deepseek-flash"))
        self.assertFalse(model_matches_provider("deepseek", "gpt-6.1-sol"))
        self.assertTrue(model_matches_provider("openai", "gpt-6.1-sol"))
        self.assertFalse(model_matches_provider("openai", "deepseek-flash"))
        self.assertFalse(model_matches_provider("deepseek", ""))
        self.assertFalse(model_matches_provider("openai", None))

    def test_foreign_model_is_never_saved_or_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "session_manager.sqlite")
            save_provider_settings(db, "deepseek", "gpt-6.1-sol", "low")
            model, _ = get_last_provider_settings(db, "deepseek")
            self.assertEqual(model, "deepseek-flash")  # default, the foreign name was not stored

            # A value that was poisoned in the DB earlier must not be restored either
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO manager_settings (key, value) VALUES ('last_deepseek_model', 'gpt-6.1-sol')"
                )
            model, _ = get_last_provider_settings(db, "deepseek")
            self.assertEqual(model, "deepseek-flash")

            save_provider_settings(db, "deepseek", "deepseek-v4-pro", "high")
            self.assertEqual(get_last_provider_settings(db, "deepseek")[0], "deepseek-v4-pro")


class TestCatalogPaths(unittest.TestCase):
    def test_cat_dbs_lists_live_first_then_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_codex_home(tmp, with_binary=False)
            sqlite_dir = os.path.join(home, "sqlite")
            os.makedirs(sqlite_dir)
            for name in ("codex.db", "codex-dev.db"):
                open(os.path.join(sqlite_dir, name), "w").close()
            paths = CodexPaths(home)
            self.assertEqual([os.path.basename(p) for p in paths.cat_dbs], ["codex.db", "codex-dev.db"])
            self.assertTrue(paths.cat_db.endswith("codex.db"))

    def test_falls_back_to_dev_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_codex_home(tmp, with_binary=False)
            sqlite_dir = os.path.join(home, "sqlite")
            os.makedirs(sqlite_dir)
            open(os.path.join(sqlite_dir, "codex-dev.db"), "w").close()
            paths = CodexPaths(home)
            self.assertEqual([os.path.basename(p) for p in paths.cat_dbs], ["codex-dev.db"])
            self.assertTrue(paths.cat_db.endswith("codex-dev.db"))


class TestApplyAppServerArchive(unittest.TestCase):
    OAI, DS = "oai-1", "ds-1"

    def _make_state(self, tmp, oai_row, ds_row):
        home = make_codex_home(tmp, with_binary=False)
        paths = CodexPaths(home)
        with sqlite3.connect(paths.state_db) as conn:
            conn.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER, archived_at INTEGER, rollout_path TEXT)"
            )
            for tid, (archived, path) in ((self.OAI, oai_row), (self.DS, ds_row)):
                conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (tid, archived, 5 if archived else None, path))
        return home, paths

    def _pairs(self):
        return [(1, "Pair", self.OAI, self.DS, "", 0, 1)]

    def _flags(self, paths):
        with sqlite3.connect(paths.state_db) as conn:
            return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, archived, archived_at FROM threads")}

    def test_deepseek_mode_archives_openai_and_unarchives_ds(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = os.path.join("x", "archived_sessions", "r.jsonl")
            home, paths = self._make_state(tmp, (0, "/s/o.jsonl"), (1, arch))
            with mock.patch("codex_manager.appserver.set_threads_archived", return_value={self.OAI, self.DS}) as rpc:
                switch_mod._apply_app_server_archive(paths, self._pairs(), "deepseek", home)
            rpc.assert_called_once_with(home, archive_ids=[self.OAI], unarchive_ids=[self.DS])

    def test_legacy_flag_only_archive_is_reset_before_rpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            # DS twin only has the old flag (rollout still in sessions/): must be archived properly in openai mode
            home, paths = self._make_state(tmp, (1, "/s/o.jsonl"), (1, "/s/d.jsonl"))
            with mock.patch("codex_manager.appserver.set_threads_archived", return_value={self.DS}) as rpc:
                switch_mod._apply_app_server_archive(paths, self._pairs(), "openai", home)
            rpc.assert_called_once_with(home, archive_ids=[self.DS], unarchive_ids=[])
            flags = self._flags(paths)
            self.assertEqual(flags[self.DS], (0, None))  # reset so thread/archive can move the rollout
            self.assertEqual(flags[self.OAI], (0, None))  # flag-only archive of the wanted-visible twin is cleared

    def test_already_correct_state_makes_no_rpc_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = os.path.join("x", "archived_sessions", "r.jsonl")
            home, paths = self._make_state(tmp, (0, "/s/o.jsonl"), (1, arch))
            with mock.patch("codex_manager.appserver.set_threads_archived") as rpc:
                switch_mod._apply_app_server_archive(paths, self._pairs(), "openai", home)
            rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()

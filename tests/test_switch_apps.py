import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_manager import apps
from codex_manager.switch import restore_unarchived_rollouts, sync_desktop_catalog


class TestClassify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(apps._classify(["/usr/lib/chatgpt/ChatGPT", "--codex-browser-background-networking-disabled"]), "desktop")
        self.assertIsNone(apps._classify(["/usr/lib/chatgpt/ChatGPT", "--type=renderer"]))
        self.assertIsNone(apps._classify(["/usr/lib/chatgpt/ChatGPT --type=zygote --no-zygote-sandbox"]))
        self.assertIsNone(apps._classify(["/usr/lib/chatgpt/resources/codex", "app-server"]))
        daemon = ["/home/u/.codex/packages/app-server-daemon/releases/x/bin/codex", "app-server", "--remote-control"]
        self.assertEqual(apps._classify(daemon), "remote-daemon")
        self.assertIsNone(apps._classify(daemon[:2] + ["daemon", "pid-update-loop"]))
        self.assertEqual(apps._classify(["/home/u/.local/bin/codex"]), "cli")
        self.assertEqual(apps._classify(["/home/u/.local/bin/codex", "exec", "hi"]), "cli")
        self.assertIsNone(apps._classify(["/home/u/.local/bin/codex", "remote-control", "start"]))
        self.assertIsNone(apps._classify(["/usr/bin/python3", "codex_migrator.py"]))


class TestEnsureStopped(unittest.TestCase):
    RUNNING = [{"pid": 4242, "kind": "desktop", "cmd": "/usr/lib/chatgpt/ChatGPT"}]

    def test_nothing_running(self):
        with mock.patch.object(apps, "detect_codex_apps", return_value=[]):
            self.assertEqual(apps.ensure_codex_apps_stopped(), (True, []))

    def test_rejects_when_non_interactive(self):
        with mock.patch.object(apps, "detect_codex_apps", return_value=self.RUNNING), mock.patch.object(
            apps.sys.stdin, "isatty", return_value=False
        ), mock.patch.object(apps, "stop_codex_apps") as stop:
            self.assertEqual(apps.ensure_codex_apps_stopped(), (False, []))
            stop.assert_not_called()

    def test_stop_apps_flag_stops_without_asking(self):
        with mock.patch.object(apps, "detect_codex_apps", side_effect=[self.RUNNING, []]), mock.patch.object(
            apps, "stop_codex_apps", return_value=["codex-rc.service"]
        ) as stop:
            self.assertEqual(apps.ensure_codex_apps_stopped(stop_apps=True), (True, ["codex-rc.service"]))
            stop.assert_called_once_with(self.RUNNING)


class TestRestoreUnarchivedRollouts(unittest.TestCase):
    def test_moves_visible_thread_out_of_archive(self):
        with tempfile.TemporaryDirectory() as home:
            arch = os.path.join(home, "archived_sessions")
            os.makedirs(arch)
            name = "rollout-2026-10-22T13-32-38-abc.jsonl"
            visible, hidden = os.path.join(arch, name), os.path.join(arch, "rollout-2026-10-21T01-00-00-def.jsonl")
            for p in (visible, hidden):
                open(p, "w").close()
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            conn.executemany("INSERT INTO threads VALUES (?, ?, ?)", [("a", visible, 0), ("d", hidden, 1)])
            self.assertEqual(restore_unarchived_rollouts(conn.cursor(), home), 1)
            dest = os.path.join(home, "sessions", "2026", "10", "22", name)
            self.assertTrue(os.path.isfile(dest))
            self.assertEqual(conn.execute("SELECT rollout_path FROM threads WHERE id='a'").fetchone()[0], dest)
            self.assertTrue(os.path.isfile(hidden))


class TestSyncDesktopCatalog(unittest.TestCase):
    def test_hides_archived_and_deleted_keeps_chatgpt_rows(self):
        state = sqlite3.connect(":memory:")
        state.execute("CREATE TABLE threads (id TEXT, archived INTEGER)")
        state.executemany("INSERT INTO threads VALUES (?, ?)", [("vis", 0), ("arch", 1)])
        cat = sqlite3.connect(":memory:")
        cat.execute(
            "CREATE TABLE local_thread_catalog (host_id TEXT, thread_id TEXT, model_provider TEXT, missing_candidate INTEGER)"
        )
        cat.execute("CREATE TABLE local_thread_catalog_sync_state (host_id TEXT, last_full_reconciled_at INTEGER)")
        cat.execute("INSERT INTO local_thread_catalog_sync_state VALUES ('local', 123)")
        cat.executemany(
            "INSERT INTO local_thread_catalog VALUES ('local', ?, ?, ?)",
            [("vis", "openai", 1), ("arch", "deepseek", 0), ("gone", "openai", 0), ("chatgpt", None, 0)],
        )
        self.assertEqual(sync_desktop_catalog(cat, state.cursor()), 2)
        flags = dict(cat.execute("SELECT thread_id, missing_candidate FROM local_thread_catalog").fetchall())
        self.assertEqual(flags, {"vis": 0, "arch": 1, "gone": 1, "chatgpt": 0})
        self.assertEqual(cat.execute("SELECT last_full_reconciled_at FROM local_thread_catalog_sync_state").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

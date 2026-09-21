import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_manager.reasoning import (
    ensure_deepseek_models_catalog,
    ensure_global_state_reasoning_efforts,
    ensure_reasoning_efforts,
    get_reasoning_status,
)


class TestReasoningEfforts(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_reasoning_")
        self.global_state_path = os.path.join(self.test_dir, ".codex-global-state.json")
        self.models_path = os.path.join(self.test_dir, "models.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_repairs_unticked_max_in_global_state(self):
        # User unticked max in UI -> list contains low, medium, high but NOT max
        initial_data = {
            "electron-persisted-atom-state": {
                "enabled-reasoning-efforts": ["low", "medium", "high", "xhigh"],
            }
        }
        with open(self.global_state_path, "w", encoding="utf-8") as f:
            json.dump(initial_data, f)

        ok, msg = ensure_global_state_reasoning_efforts(self.test_dir)
        self.assertTrue(ok)

        with open(self.global_state_path, "r", encoding="utf-8") as f:
            saved = json.load(f)

        efforts = saved["electron-persisted-atom-state"]["enabled-reasoning-efforts"]
        self.assertIn("max", efforts)
        self.assertIn("high", efforts)
        self.assertIn("low", efforts)

    def test_initializes_efforts_when_not_set(self):
        initial_data = {"electron-persisted-atom-state": {}}
        with open(self.global_state_path, "w", encoding="utf-8") as f:
            json.dump(initial_data, f)

        ok, msg = ensure_global_state_reasoning_efforts(self.test_dir)
        self.assertTrue(ok)

        with open(self.global_state_path, "r", encoding="utf-8") as f:
            saved = json.load(f)

        efforts = saved["electron-persisted-atom-state"]["enabled-reasoning-efforts"]
        self.assertIn("max", efforts)
        self.assertIn("high", efforts)
        self.assertIn("low", efforts)

    def test_ensures_deepseek_models_catalog(self):
        # Model has incorrect levels (e.g. medium or missing max)
        initial_models = {
            "models": [
                {
                    "slug": "deepseek-flash",
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "medium"},
                        {"effort": "high"},
                    ],
                }
            ]
        }
        with open(self.models_path, "w", encoding="utf-8") as f:
            json.dump(initial_models, f)

        ok, msg = ensure_deepseek_models_catalog(self.test_dir)
        self.assertTrue(ok)

        with open(self.models_path, "r", encoding="utf-8") as f:
            saved = json.load(f)

        model = saved["models"][0]
        self.assertEqual(model["default_reasoning_level"], "high")
        efforts = [lvl["effort"] for lvl in model["supported_reasoning_levels"]]
        self.assertEqual(efforts, ["low", "high", "max"])
        self.assertNotIn("medium", efforts)

    def test_composite_ensure_and_status(self):
        initial_global = {"electron-persisted-atom-state": {"enabled-reasoning-efforts": ["low", "high"]}}
        with open(self.global_state_path, "w", encoding="utf-8") as f:
            json.dump(initial_global, f)

        initial_models = {
            "models": [
                {
                    "slug": "deepseek-flash",
                    "default_reasoning_level": "high",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "high"},
                        {"effort": "max"},
                    ],
                }
            ]
        }
        with open(self.models_path, "w", encoding="utf-8") as f:
            json.dump(initial_models, f)

        self.assertTrue(ensure_reasoning_efforts(self.test_dir, verbose=False))
        stat = get_reasoning_status(self.test_dir)
        self.assertTrue(stat["global_ok"])
        self.assertTrue(stat["catalog_ok"])
        self.assertIn("max", stat["global_efforts"])


if __name__ == "__main__":
    unittest.main()

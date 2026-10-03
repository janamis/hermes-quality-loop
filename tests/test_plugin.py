from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "quality_loop_plugin_tested",
    PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
assert spec and spec.loader
plugin: Any = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


class PluginRegistrationTests(unittest.TestCase):
    def test_desktop_ui_uses_live_profile_and_model_pickers(self):
        source = (PLUGIN_ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")

        self.assertIn("host.request('profiles.list'", source)
        self.assertIn("host.profileRoutes()", source)
        self.assertIn("host.requestProfile(route, 'model.options', { include_unconfigured: true })", source)
        self.assertIn("host.request('model.options', { include_unconfigured: true })", source)
        self.assertIn("/model-catalog?profile=", source)
        self.assertIn("OFFLINE_MODEL_FALLBACKS", source)
        self.assertNotIn("refetchInterval: 10000", source)
        self.assertIn("models available from", source)
        self.assertIn("pluginOs.pickOpenPath", source)
        self.assertIn("directories: true", source)
        self.assertIn("Browse…", source)
        self.assertNotIn("explicit_only: true", source)
        self.assertIn("SelectTrigger", source)
        self.assertIn("SelectItem", source)
        self.assertIn("onCheckedChange: setPublishOnSuccess", source)
        self.assertNotIn("useState('qwen3.5:122b-a10b')", source)
        self.assertNotIn("useState('glm-4.7-flash:q4_K_M')", source)

    def test_dispatch_tick_only_enqueues_reconcile(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow_reconcile(*, board=None):
            calls.append(board)
            started.set()
            release.wait(2)

        class Context:
            profile_name = "test-profile"

            def register_hook(self, name, handler):
                self.hook_name = name
                self.handler = handler

            def register_command(self, *args, **kwargs):
                return None

        context = Context()
        with mock.patch.object(plugin.controller, "reconcile_all", side_effect=slow_reconcile):
            plugin._WORKER = None
            plugin.register(context)
            started_at = time.monotonic()
            context.handler(board="default", dry_run=False)
            elapsed = time.monotonic() - started_at
            self.assertLess(elapsed, 0.1)
            self.assertTrue(started.wait(1))
            context.handler(board="default", dry_run=False)
            release.set()
            worker = plugin._WORKER
            assert worker is not None
            worker._queue.join()

        self.assertEqual(context.hook_name, "on_kanban_dispatch_tick")
        self.assertEqual(calls, ["default"])

    def test_dashboard_api_loads_prefixed_controller_without_sys_path_mutation(self):
        before = list(sys.path)
        sys.modules.pop("hermes_quality_loop_controller", None)
        api_spec = importlib.util.spec_from_file_location(
            "quality_loop_dashboard_api_tested",
            PLUGIN_ROOT / "dashboard" / "plugin_api.py",
        )
        assert api_spec and api_spec.loader
        api = importlib.util.module_from_spec(api_spec)
        api_spec.loader.exec_module(api)

        self.assertEqual(sys.path, before)
        self.assertEqual(api.controller.__name__, "hermes_quality_loop_controller")
        self.assertIs(sys.modules["hermes_quality_loop_controller"], api.controller)

    def test_dashboard_api_reads_cached_profile_model_catalog(self):
        api_spec = importlib.util.spec_from_file_location(
            "quality_loop_dashboard_api_catalog_tested",
            PLUGIN_ROOT / "dashboard" / "plugin_api.py",
        )
        assert api_spec and api_spec.loader
        api = importlib.util.module_from_spec(api_spec)
        api_spec.loader.exec_module(api)

        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            cache = Path(directory) / "provider_models_cache.json"
            cache.write_text(json.dumps({"openai-codex": {"models": ["gpt-a", "gpt-b", "gpt-a"]}}), encoding="utf-8")
            with mock.patch.object(api, "resolve_profile_env", return_value=directory):
                result = api.model_catalog("chatgpt", "openai-codex")

        self.assertEqual(result["models"], ["gpt-a", "gpt-b"])

    def test_dashboard_api_resolves_custom_provider_cache_key(self):
        api_spec = importlib.util.spec_from_file_location(
            "quality_loop_dashboard_api_custom_catalog_tested",
            PLUGIN_ROOT / "dashboard" / "plugin_api.py",
        )
        assert api_spec and api_spec.loader
        api = importlib.util.module_from_spec(api_spec)
        api_spec.loader.exec_module(api)

        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            profile_home = Path(directory)
            (profile_home / "config.yaml").write_text(
                "model:\n  provider: custom:litellm\n  base_url: http://litellm.example:4000/v1\n",
                encoding="utf-8",
            )
            (profile_home / "provider_models_cache.json").write_text(
                json.dumps({"custom:http://litellm.example:4000#fingerprint": {"models": ["model-a", "model-b"]}}),
                encoding="utf-8",
            )
            with mock.patch.object(api, "resolve_profile_env", return_value=directory):
                result = api.model_catalog("lab", "custom:litellm")

        self.assertEqual(result["models"], ["model-a", "model-b"])


if __name__ == "__main__":
    unittest.main()
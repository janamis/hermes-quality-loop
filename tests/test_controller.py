from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = PLUGIN_ROOT / "quality_loop_controller.py"

# Hermes source checkouts expose some compatibility modules from the package's
# repository root. Discover that root from the installed package rather than a
# machine-specific path so the test suite works in any checkout.
import hermes_cli  # noqa: E402

HERMES_PACKAGE_ROOT = Path(hermes_cli.__file__).resolve().parent.parent
if str(HERMES_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_PACKAGE_ROOT))

spec = importlib.util.spec_from_file_location("quality_loop_controller_tested", CONTROLLER_PATH)
controller = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(controller)

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_db_connect as kbc  # noqa: E402


class QualityLoopControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._env = {
            name: os.environ.get(name)
            for name in (
                "HERMES_HOME",
                "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_TASK",
                "HERMES_DELEGATED_CHILD_CONTEXT",
            )
        }
        os.environ["HERMES_HOME"] = self.temp.name
        for name in self._env:
            if name != "HERMES_HOME":
                os.environ.pop(name, None)
        self.workspace = Path(self.temp.name) / "repo"
        self.workspace.mkdir()
        (self.workspace / "tests").mkdir()
        for relative_path in (
            "app.py",
            "server.js",
            "package.json",
            "package-lock.json",
            "tests/quadtree-slicing.test.js",
        ):
            (self.workspace / relative_path).write_text(
                f"baseline for {relative_path}\n", encoding="utf-8"
            )
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.name", "Quality Loop Test"],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.workspace), "config", "user.email",
                "quality-loop@example.invalid",
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.workspace), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-q", "-m", "baseline"],
            check=True,
        )
        self._real_kanban_connect = kbc.connect
        self._patchers = [
            mock.patch.object(
                controller,
                "get_default_hermes_root",
                side_effect=lambda: (
                    Path(os.environ["HERMES_HOME"]).parent.parent
                    if Path(os.environ["HERMES_HOME"]).parent.name == "profiles"
                    else Path(os.environ["HERMES_HOME"])
                ),
            ),
            mock.patch.object(
                kbc,
                "connect",
                side_effect=lambda db_path=None, *, board=None: self._real_kanban_connect(
                    Path(self.temp.name) / "kanban.db"
                ),
            ),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    def create_campaign(self, **overrides):
        data = {
            "name": "Test Loop",
            "board": "default",
            "workspace": str(self.workspace),
            "assignee": "test-profile",
            "examiner_model": "example-examiner",
            "executor_model": "example-executor",
            "validator_model": "example-validator",
            "build_command": "true",
            "test_command": "true",
            "gate_timeout_seconds": 30,
            "max_rounds": 4,
            "max_repairs": 2,
        }
        data.update(overrides)
        return controller.create_campaign(data)

    def publish_to_local_test_remote(self, campaign, remote):
        """Exercise publication while bypassing only the production local-remote ban."""
        with mock.patch.object(
            controller, "_safe_publication_remote_url", return_value=str(remote)
        ):
            return controller._publish_success(campaign)

    def test_examine_card_is_narrow_and_bounded_to_twenty_minutes(self):
        campaign = self.create_campaign(target_average=9.0)
        conn = kbc.connect(board=campaign["board"])
        try:
            task = kb.get_task(conn, campaign["active_task_id"])
        finally:
            conn.close()
        self.assertIsNotNone(task)
        assert task is not None
        body = task.body or ""
        self.assertIn("single highest-priority defect", body)
        self.assertIn("Do not run dependency installation", body)
        self.assertIn("or the full build or test suite", body)
        self.assertNotIn("improvement_items", body)
        self.assertNotIn("execution_slices", body)
        self.assertNotIn("Set `kanban_complete.quality_loop` to exactly:", body)
        self.assertEqual(task.max_runtime_seconds, 1200)

    def test_default_campaign_uses_complete_prompt_profile(self):
        campaign = self.create_campaign()
        self.assertEqual(campaign["prompt_profile"], "complete")
        body = controller._task_body(campaign, "examine")
        self.assertIn("The controller computes the arithmetic average", body)

    def test_simple_prompt_profile_keeps_short_local_model_prompts(self):
        campaign = self.create_campaign(prompt_profile="simple")
        self.assertEqual(campaign["prompt_profile"], "simple")
        for stage in ("examine", "scope_validate", "plan", "execute", "validate"):
            body = controller._task_body(campaign, stage)
            # The machine-parsed trusted markers must survive the short profile.
            self.assertIn("TRUSTED_QUALITY_LOOP_CARD", body, stage)
            self.assertIn("QUALITY_LOOP_JSON: ", body, stage)
            # Local-model guidance stays short: no multi-paragraph exclusion essays.
            self.assertLess(len(body), 2200, f"{stage} prompt too long for local models")
            self.assertNotIn("Do only these four things", body, stage)
            self.assertNotIn("READ-ONLY EXAMINATION: inspect the CURRENT project", body, stage)

    def test_simple_profile_examine_prompt_names_all_five_categories(self):
        campaign = self.create_campaign(prompt_profile="simple")
        body = controller._task_body(campaign, "examine")
        for category in controller.RANKING_CATEGORIES:
            self.assertIn(category, body)
        self.assertIn("one", body.lower())
        self.assertIn("defect", body.lower())

    def test_prompt_profile_rejects_unknown_values(self):
        with self.assertRaisesRegex(ValueError, "prompt_profile"):
            self.create_campaign(prompt_profile="verbose")

    def test_simple_profile_retains_validator_gate_lines_and_fallback_wording(self):
        campaign = self.create_campaign(prompt_profile="simple")
        body = controller._task_body(campaign, "validate")
        self.assertIn("- build: `true`", body)
        self.assertIn("- test: `true`", body)
        self.assertIn("Verdict PASS or FAIL", body)

    def test_simple_profile_targets_and_publish_lines_survive(self):
        campaign = self.create_campaign(
            prompt_profile="simple", target_average=9.0,
            publish_on_success=True, publish_branch="rank-loop",
        )
        body = controller._task_body(campaign, "examine")
        self.assertIn("TARGET AVERAGE: 9/10", body)
        # A fresh campaign has no previous average; the line appears once round 2 begins.
        self.assertNotIn("PREVIOUS COMPUTED AVERAGE", body)
        c = dict(campaign, last_average=7.5)
        body = controller._task_body(c, "examine")
        self.assertIn("TARGET AVERAGE: 9/10", body)
        self.assertIn("PREVIOUS COMPUTED AVERAGE: 7.5/10", body)

    def test_proposal_moves_through_scope_and_plan_before_execute(self):
        campaign = self.create_campaign()
        campaign = self.complete(
            campaign,
            {
                "schema": controller.SCHEMA,
                "role": "examine",
                "verdict": "proposal",
                "score_breakdown": {
                    name: 7.0 for name in controller.RANKING_CATEGORIES
                },
                "score_rationale": "One boundary defect dominates the current score.",
                "selected_defect": {
                    "title": "Return the tested value",
                    "description": "The application seam returns the wrong value.",
                    "evidence": ["app.py contains the incorrect return path"],
                    "proposed_outcome": "The application seam returns the tested value.",
                },
            },
            auto_scope=False,
        )
        self.assertEqual(campaign["stage"], "scope_validate")

        scoped = {
            "title": "Return the tested value",
            "component": "application module",
            "behavior": "the application seam returns the tested value",
            "boundary": "application function return value",
            "implementation_prompt": "Change only the application return path.",
            "acceptance_criteria": ["configured gates pass"],
            "verification_commands": ["true"],
            "relevant_files": ["app.py"],
            "excluded_scope": ["all unrelated modules"],
            "risks": [],
        }
        campaign = self.complete(
            campaign,
            {
                "schema": controller.SCHEMA,
                "role": "scope_validate",
                "verdict": "pass",
                "scoped_improvement": scoped,
                "findings": [],
            },
            auto_scope=False,
        )
        self.assertEqual(campaign["stage"], "plan")

        campaign = self.complete(
            campaign,
            {
                "schema": controller.SCHEMA,
                "role": "plan",
                "decomposition_required": False,
                "rationale": "The item already changes one component and one boundary.",
            },
            auto_scope=False,
        )
        self.assertEqual(campaign["stage"], "execute")
        self.assertEqual(campaign["slice_count"], 1)
        self.assertEqual(campaign["selected_improvement"]["title"], scoped["title"])

    def test_pause_stop_and_resume_reject_nonresumable_terminal_states(self):
        c = self.create_campaign()
        for state in ("succeeded", "needs_review", "max_rounds"):
            for action in ("pause", "stop", "resume"):
                with self.subTest(state=state, action=action):
                    controller._update(c["id"], state=state, message="terminal fixture")
                    with self.assertRaisesRegex(ValueError, state):
                        controller.set_campaign_state(c["id"], action)
                    self.assertEqual(controller.get_campaign(c["id"])["state"], state)

    def test_pause_and_stop_are_idempotent_only_in_their_own_state(self):
        paused = self.create_campaign()
        paused = controller.set_campaign_state(paused["id"], "pause")
        self.assertEqual(controller.set_campaign_state(paused["id"], "pause")["state"], "paused")

        stopped = controller.set_campaign_state(paused["id"], "stop")
        self.assertEqual(controller.set_campaign_state(stopped["id"], "stop")["state"], "stopped")
        with self.assertRaisesRegex(ValueError, "stopped"):
            controller.set_campaign_state(stopped["id"], "pause")

    def test_pause_stop_and_resume_reject_while_reconcile_holds_process_lock(self):
        c = self.create_campaign()
        for action in ("pause", "stop", "resume"):
            with self.subTest(action=action):
                if action == "resume":
                    controller._update(c["id"], state="paused")
                else:
                    controller._update(c["id"], state="running")
                entered = threading.Event()
                release = threading.Event()

                def blocked_reconcile():
                    with mock.patch.object(
                        controller,
                        "_reconcile_campaign_in_process",
                        side_effect=lambda campaign_id: (entered.set(), release.wait(5))[0],
                    ):
                        controller.reconcile_campaign(c["id"])

                thread = threading.Thread(target=blocked_reconcile)
                thread.start()
                self.assertTrue(entered.wait(5))
                try:
                    with self.assertRaisesRegex(ValueError, "busy"):
                        controller.set_campaign_state(c["id"], action)
                finally:
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())

    def test_resume_holds_process_lock_through_reconcile_without_recursion(self):
        c = self.create_campaign()
        controller.set_campaign_state(c["id"], "pause")
        entered = threading.Event()
        release = threading.Event()

        original = controller._reconcile_campaign_in_process

        def blocked_resume_reconcile(campaign_id):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(campaign_id)

        with mock.patch.object(
            controller, "_reconcile_campaign_in_process", side_effect=blocked_resume_reconcile
        ):
            thread = threading.Thread(
                target=controller.set_campaign_state, args=(c["id"], "resume")
            )
            thread.start()
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(ValueError, "busy"):
                    controller.set_campaign_state(c["id"], "stop")
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(controller.get_campaign(c["id"])["state"], "running")

    def _complete_active_without_reconcile(self, campaign, payload):
        conn = kbc.connect(board=campaign["board"])
        try:
            self.assertTrue(
                kb.complete_task(
                    conn,
                    campaign["active_task_id"],
                    summary="completed while campaign inactive",
                    metadata={"quality_loop": payload},
                    fire_lifecycle_hook=False,
                )
            )
            return kb.latest_run(conn, campaign["active_task_id"])
        finally:
            conn.close()

    def test_paused_and_stopped_completed_runs_resume_and_reconcile_exactly_once(self):
        for action in ("pause", "stop"):
            with self.subTest(action=action):
                c = self.create_campaign()
                c = controller.set_campaign_state(c["id"], action)
                self._complete_active_without_reconcile(c, self.proposal())
                conn = kbc.connect(board=c["board"])
                try:
                    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                finally:
                    conn.close()

                resumed = controller.set_campaign_state(c["id"], "resume")
                self.assertEqual(resumed["state"], "running")
                self.assertEqual(resumed["stage"], "scope_validate")
                first_child = resumed["active_task_id"]
                conn = kbc.connect(board=c["board"])
                try:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before + 1
                    )
                finally:
                    conn.close()

                repeated = controller.reconcile_campaign(c["id"])
                self.assertEqual(repeated["active_task_id"], first_child)
                with self.assertRaisesRegex(ValueError, "already running"):
                    controller.set_campaign_state(c["id"], "resume")
                conn = kbc.connect(board=c["board"])
                try:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before + 1
                    )
                finally:
                    conn.close()
                controller._update(c["id"], state="stopped")

    def test_resume_rejects_already_processed_completed_run_without_replay(self):
        for action in ("pause", "stop"):
            with self.subTest(action=action):
                c = self.create_campaign()
                try:
                    c = controller.set_campaign_state(c["id"], action)
                    run = self._complete_active_without_reconcile(c, self.proposal())
                    self.assertIsNotNone(run)
                    controller._update(
                        c["id"],
                        processed_run_id=run.id,
                        last_publish_result=json.dumps({"ok": True, "commit": "deadbeef"}),
                    )

                    with mock.patch.object(controller, "_publish_success") as publish:
                        with self.assertRaisesRegex(ValueError, "already processed"):
                            controller.set_campaign_state(c["id"], "resume")
                    publish.assert_not_called()
                    expected_state = "paused" if action == "pause" else "stopped"
                    self.assertEqual(controller.get_campaign(c["id"])["state"], expected_state)
                finally:
                    controller._update(c["id"], state="stopped")

    def _assert_direct_examine_rejected(self, payload, *, ranked=False):
        c = self.create_campaign(target_average=9.0 if ranked else None)
        result = self.complete(c, payload, auto_scope=False)
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result["active_task_id"], c["active_task_id"])
        conn = kbc.connect(board=c["board"])
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        finally:
            conn.close()

    def test_direct_completion_rejects_unknown_improvement_item_key_without_child(self):
        payload = self.proposal()
        payload["selected_defect"]["unknown_item_key"] = "forbidden"
        self._assert_direct_examine_rejected(payload)

    def test_direct_completion_rejects_unknown_execution_slice_key_without_child(self):
        payload = self.proposal()
        payload["execution_slices"] = []
        self._assert_direct_examine_rejected(payload)

    def test_direct_completion_rejects_nested_execution_slices_without_child(self):
        payload = self.proposal()
        payload["selected_defect"]["execution_slices"] = []
        self._assert_direct_examine_rejected(payload)

    def test_direct_completion_rejects_execution_slice_priority_without_child(self):
        payload = self.proposal()
        payload["selected_defect"]["priority"] = 1
        self._assert_direct_examine_rejected(payload)

    def test_direct_completion_rejects_boolean_score_without_child(self):
        payload = self.proposal(scores=7.5)
        payload["score_breakdown"][controller.RANKING_CATEGORIES[0]] = True
        self._assert_direct_examine_rejected(payload, ranked=True)

    def test_direct_completion_rejects_string_score_without_coercion_or_child(self):
        payload = self.proposal(scores=7.5)
        payload["score_breakdown"][controller.RANKING_CATEGORIES[0]] = "7.5"
        self._assert_direct_examine_rejected(payload, ranked=True)

    def test_profile_and_models_are_required_without_private_defaults(self):
        required = ("assignee", "examiner_model", "executor_model", "validator_model")
        for field in required:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "required"):
                data = {
                    "name": "Portable Loop",
                    "board": "default",
                    "workspace": str(self.workspace),
                    "assignee": "test-profile",
                    "examiner_model": "example-examiner",
                    "executor_model": "example-executor",
                    "validator_model": "example-validator",
                    "test_command": "true",
                }
                data[field] = ""
                controller.create_campaign(data)

    def test_discovery_configures_commands_and_repairs_before_examination(self):
        (self.workspace / "package.json").write_text(
            json.dumps({"scripts": {"test": "node tests/quadtree-slicing.test.js"}}) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.workspace), "add", "package.json"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-q", "-m", "valid node manifest"],
            check=True,
        )
        c = self.create_campaign(build_command="", test_command="", max_repairs=5)
        self.assertEqual(c["stage"], "discover")
        self.assertEqual(c["build_command"], "")
        self.assertEqual(c["test_command"], "")
        conn = kbc.connect(board=c["board"])
        try:
            discovery = kb.get_task(conn, c["active_task_id"])
            self.assertIsNotNone(discovery)
            assert discovery is not None
            self.assertIn("ROLE: DISCOVER", discovery.body or "")
            self.assertIn("TRUSTED_DISCOVERY_CONTRACT:", discovery.body or "")
            self.assertEqual(discovery.skills, [])
        finally:
            conn.close()
        contract = json.loads(c["discovery_contract"])
        payload = {
            "schema": controller.SCHEMA,
            "role": "discover",
            "verdict": "configured",
            "project_kind": contract["project_kind"],
            "languages": contract["languages"],
            "manifests": contract["manifests"],
            "build_command": contract["build_candidates"][0],
            "test_command": contract["test_candidates"][0],
            "max_repairs": 1,
            "runtime_evidence": contract["runtime_evidence"],
        }
        result = self.complete(c, payload, auto_scope=False)
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["stage"], "examine")
        self.assertEqual(result["build_command"], payload["build_command"])
        self.assertEqual(result["test_command"], payload["test_command"])
        self.assertEqual(result["max_repairs"], 1)
        conn = kbc.connect(board=result["board"])
        try:
            examine = kb.get_task(conn, result["active_task_id"])
            self.assertIsNotNone(examine)
            assert examine is not None
            self.assertEqual(kb.parent_ids(conn, examine.id), [discovery.id])
            self.assertIn(payload["build_command"], examine.body or "")
            self.assertIn(payload["test_command"], examine.body or "")
        finally:
            conn.close()

    def test_discovery_rejects_untrusted_repair_count_without_child(self):
        (self.workspace / "package.json").write_text(
            json.dumps({"scripts": {"test": "node tests/quadtree-slicing.test.js"}}) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.workspace), "add", "package.json"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-q", "-m", "valid node manifest"],
            check=True,
        )
        c = self.create_campaign(build_command="", test_command="")
        contract = json.loads(c["discovery_contract"])
        payload = {
            "schema": controller.SCHEMA,
            "role": "discover",
            "verdict": "configured",
            "project_kind": contract["project_kind"],
            "languages": contract["languages"],
            "manifests": contract["manifests"],
            "build_command": contract["build_candidates"][0],
            "test_command": contract["test_candidates"][0],
            "max_repairs": 99,
            "runtime_evidence": contract["runtime_evidence"],
        }
        result = self.complete(c, payload, auto_scope=False)
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result["active_task_id"], c["active_task_id"])
        conn = kbc.connect(board=c["board"])
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        finally:
            conn.close()

    def test_discovery_rejects_ignored_untracked_manifest(self):
        (self.workspace / "package.json").write_text(
            json.dumps({"scripts": {"test": "node tests/quadtree-slicing.test.js"}}) + "\n",
            encoding="utf-8",
        )
        (self.workspace / ".gitignore").write_text("package.json\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.workspace), "rm", "--cached", "package.json"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(["git", "-C", str(self.workspace), "add", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-q", "-m", "ignore manifest"],
            check=True,
        )
        with self.assertRaisesRegex(ValueError, "tracked package.json"):
            self.create_campaign(build_command="", test_command="")

    def test_discovery_rejects_manifest_mutation_during_authenticated_read(self):
        (self.workspace / "package.json").write_text(
            json.dumps({"scripts": {"test": "node tests/quadtree-slicing.test.js"}}) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.workspace), "add", "package.json"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-q", "-m", "valid node manifest"],
            check=True,
        )
        root_fd = controller._open_workspace_root(self.workspace)
        original = controller._raw_worktree_entry
        mutated = False

        def mutate_after_auth(root, path, object_format, *, root_fd=None):
            nonlocal mutated
            value = original(root, path, object_format, root_fd=root_fd)
            if path == "package.json" and not mutated:
                mutated = True
                (self.workspace / "package.json").write_text(
                    json.dumps({"scripts": {"test": "attacker command"}}) + "\n",
                    encoding="utf-8",
                )
            return value

        try:
            with controller._pinned_git_context(self.workspace, root_fd):
                with mock.patch.object(
                    controller, "_raw_worktree_entry", side_effect=mutate_after_auth
                ):
                    with self.assertRaisesRegex(ValueError, "changed during authenticated read"):
                        controller._discover_project_contract(self.workspace, root_fd)
        finally:
            os.close(root_fd)

    def test_discovery_persists_configuration_before_examination_card_creation(self):
        (self.workspace / "package.json").write_text(
            json.dumps({"scripts": {"test": "node tests/quadtree-slicing.test.js"}}) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.workspace), "add", "package.json"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-q", "-m", "valid node manifest"],
            check=True,
        )
        c = self.create_campaign(build_command="", test_command="", max_repairs=5)
        contract = json.loads(c["discovery_contract"])
        payload = {
            "schema": controller.SCHEMA,
            "role": "discover",
            "verdict": "configured",
            "project_kind": contract["project_kind"],
            "languages": contract["languages"],
            "manifests": contract["manifests"],
            "build_command": contract["build_candidates"][0],
            "test_command": contract["test_candidates"][0],
            "max_repairs": 1,
            "runtime_evidence": contract["runtime_evidence"],
        }
        self._complete_active_without_reconcile(c, payload)

        def fail_after_asserting_persistence(selected, stage, parents, **_kwargs):
            persisted = controller.get_campaign(c["id"])
            self.assertEqual(stage, "examine")
            self.assertEqual(parents, [c["active_task_id"]])
            self.assertEqual(persisted["build_command"], payload["build_command"])
            self.assertEqual(persisted["test_command"], payload["test_command"])
            self.assertEqual(persisted["max_repairs"], 1)
            raise RuntimeError("forced examination creation failure")

        with mock.patch.object(controller, "_create_task", side_effect=fail_after_asserting_persistence):
            with self.assertRaisesRegex(RuntimeError, "forced examination"):
                controller.reconcile_campaign(c["id"])
        persisted = controller.get_campaign(c["id"])
        self.assertEqual(persisted["stage"], "discover")
        self.assertEqual(persisted["max_repairs"], 1)
        self.assertIsNone(persisted["processed_run_id"])

    def test_unsliced_improvement_over_two_files_is_rejected(self):
        scoped = self.scoped_item(
            "Three-file change",
            "Change three files together.",
            "configured gates pass",
        )
        scoped["relevant_files"] = ["app.py", "server.js", "package.json"]
        campaign = self.create_campaign()
        campaign = self.complete(campaign, self.proposal(item=scoped), auto_scope=False)
        campaign = self.complete(
            campaign,
            {
                "schema": controller.SCHEMA,
                "role": "scope_validate",
                "verdict": "pass",
                "scoped_improvement": scoped,
                "findings": [],
            },
            auto_scope=False,
        )
        campaign = self.complete(
            campaign,
            {
                "schema": controller.SCHEMA,
                "role": "plan",
                "decomposition_required": False,
                "rationale": "No decomposition requested.",
            },
            auto_scope=False,
        )
        self.assertEqual(campaign["state"], "needs_review")
        self.assertEqual(campaign["stage"], "plan")

    def test_sliced_parent_must_exactly_equal_slice_file_union(self):
        payload = self.sliced_proposal()
        parent = dict(self._auto_scoped_item)
        parent["relevant_files"] = [*parent["relevant_files"], "app.py"]
        campaign = self.create_campaign()
        campaign = self.complete(campaign, payload, auto_scope=False)
        campaign = self.complete(
            campaign,
            {
                "schema": controller.SCHEMA,
                "role": "scope_validate",
                "verdict": "pass",
                "scoped_improvement": parent,
                "findings": [],
            },
            auto_scope=False,
        )
        campaign = self.complete(
            campaign,
            {
                "schema": controller.SCHEMA,
                "role": "plan",
                "decomposition_required": True,
                "rationale": "Three serial changes are required.",
                "execution_slices": self._auto_plan_slices,
            },
            auto_scope=False,
        )
        self.assertEqual(campaign["state"], "needs_review")
        self.assertEqual(campaign["stage"], "plan")

    def complete(self, campaign, payload=None, summary="done", *, auto_scope=True):
        if (
            isinstance(payload, dict)
            and payload == {"schema": controller.SCHEMA, "role": "execute"}
        ):
            conn = kbc.connect(board=campaign["board"])
            try:
                task = kb.get_task(conn, campaign["active_task_id"])
            finally:
                conn.close()
            contract = controller._execution_contract_from_body(task.body or "")
            self.assertIsNotNone(contract)
            changed_file = contract["allowed_files"][0]
            target = self.workspace / changed_file
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(f"execution {campaign['active_task_id']}\n")
            payload = {
                "schema": controller.SCHEMA,
                "role": "execute",
                "changed_files": [changed_file],
                "verification": [
                    {"command": command, "exit_code": 0}
                    for command in contract["commands"]
                ],
                "residual_risk": [],
            }
        conn = kbc.connect(board=campaign["board"])
        try:
            ok = kb.complete_task(
                conn,
                campaign["active_task_id"],
                summary=summary,
                metadata={"quality_loop": payload} if payload is not None else {},
                fire_lifecycle_hook=False,
            )
            self.assertTrue(ok)
        finally:
            conn.close()
        result = controller.reconcile_campaign(campaign["id"])
        if auto_scope and result["state"] == "running" and result["stage"] == "scope_validate":
            if int(result.get("repair_no") or 0):
                # Repairs route back through scope_validate against the active slice.
                selected = result.get("selected_improvement")
                if not isinstance(selected, dict):
                    raise AssertionError("repair scope_validate has no selected improvement")
                scoped = controller._slice_item(
                    selected, int(result.get("slice_index") or 0)
                )
                if scoped is None:
                    scoped = dict(selected)
            else:
                selected = result.get("selected_improvement")
                if isinstance(selected, dict):
                    chain = controller._execution_slices(selected)
                    if len(chain) > 1:
                        # Mid-chain slice scope validation: echo the current slice.
                        scoped = controller._slice_item(
                            selected, int(result.get("slice_index") or 0)
                        )
                        if scoped is None:
                            raise AssertionError("mid-chain slice index is invalid")
                    else:
                        scoped = dict(selected)
                else:
                    scoped = dict(getattr(self, "_auto_scoped_item", self.scoped_item(
                        "Tested application change",
                        "Implement only the tested application change.",
                        "configured gates pass",
                    )))
            result = self.complete(
                result,
                {
                    "schema": controller.SCHEMA,
                    "role": "scope_validate",
                    "verdict": "pass",
                    "scoped_improvement": scoped,
                    "findings": [],
                },
                auto_scope=False,
            )
            if result["state"] == "running" and result["stage"] == "plan":
                slices = getattr(self, "_auto_plan_slices", None)
                plan = {
                    "schema": controller.SCHEMA,
                    "role": "plan",
                    "decomposition_required": bool(slices),
                    "rationale": (
                        "The bounded item needs serial slices."
                        if slices else
                        "The bounded item is already executable as one slice."
                    ),
                }
                if slices:
                    plan["execution_slices"] = slices
                result = self.complete(result, plan, auto_scope=False)
            return result
        return result

    def proposal(self, *, scores=None, commands=None, item=None):
        commands = commands or ["true"]
        scoped = item or self.scoped_item(
            "Tested application change",
            "Implement only the tested application change.",
            "configured gates pass",
            commands=commands,
        )
        self._auto_scoped_item = dict(scoped)
        self._auto_plan_slices = None
        if scores is None:
            scores = 7.5
        if isinstance(scores, (int, float)):
            scores = {name: float(scores) for name in controller.RANKING_CATEGORIES}
        return {
            "schema": controller.SCHEMA,
            "role": "examine",
            "verdict": "proposal",
            "score_breakdown": scores,
            "score_rationale": "Deterministic test scores",
            "selected_defect": {
                "title": scoped["title"],
                "description": scoped["implementation_prompt"],
                "evidence": [f"{scoped['relevant_files'][0]} demonstrates the defect"],
                "proposed_outcome": scoped["behavior"],
            },
        }

    def scoped_item(
        self,
        title,
        implementation_prompt,
        acceptance,
        *,
        priority=1,
        relevant_file="app.py",
        commands=None,
    ):
        return {
            "title": title,
            "component": "application module",
            "behavior": title.lower(),
            "boundary": "application function return value",
            "implementation_prompt": implementation_prompt,
            "acceptance_criteria": [acceptance],
            "verification_commands": commands or ["true"],
            "relevant_files": [relevant_file],
            "excluded_scope": ["all unrelated modules"],
            "risks": [],
        }

    def sliced_proposal(self):
        parent = {
            "title": "Production-backed quadtree tests",
            "component": "quadtree module",
            "behavior": "tests execute the production quadtree",
            "boundary": "quadtree module exports",
            "implementation_prompt": "Replace mock-only tests with production-backed coverage.",
            "acceptance_criteria": ["All three slices work together", "full suite passes"],
            "verification_commands": ["true"],
            "relevant_files": ["server.js", "tests/quadtree-slicing.test.js", "package.json"],
            "excluded_scope": ["unrelated application behavior"],
            "risks": [],
        }
        slices = [
            {
                "title": "Export the production seam",
                "component": "quadtree module",
                "behavior": "server exports are import-safe",
                "boundary": "quadtree module exports",
                "implementation_prompt": "Export existing functions without changing behavior.",
                "acceptance_criteria": ["server import is safe", "exports are present"],
                "verification_commands": ["true"],
                "relevant_files": ["server.js"],
                "excluded_scope": ["unrelated application behavior"],
            },
            {
                "title": "Use production functions in tests",
                "component": "quadtree module",
                "behavior": "tests import production functions",
                "boundary": "quadtree module exports",
                "implementation_prompt": "Replace local doubles with imports from server.js.",
                "acceptance_criteria": ["tests call production functions"],
                "verification_commands": ["true"],
                "relevant_files": ["tests/quadtree-slicing.test.js"],
                "excluded_scope": ["unrelated application behavior"],
            },
            {
                "title": "Integrate the test runner",
                "component": "quadtree module",
                "behavior": "the package runner executes quadtree tests",
                "boundary": "quadtree module exports",
                "implementation_prompt": "Wire the standalone test into the package runner.",
                "acceptance_criteria": ["documented test command passes"],
                "verification_commands": ["true"],
                "relevant_files": ["package.json"],
                "excluded_scope": ["unrelated application behavior"],
            },
        ]
        payload = self.proposal(item=parent)
        self._auto_scoped_item = parent
        self._auto_plan_slices = slices
        return payload

    def passing_validation(self):
        return {
            "schema": controller.SCHEMA,
            "role": "validate",
            "verdict": "pass",
            "build_passed": True,
            "tests_passed": True,
            "critical_issues": 0,
            "high_issues": 0,
            "regressions": 0,
            "findings": [],
        }

    def test_passed_change_starts_next_examination_round(self):
        c = self.create_campaign()
        self.assertEqual(c["stage"], "examine")
        self.assertEqual(c["active_task"]["model"], "example-examiner")

        c = self.complete(c, self.proposal())
        self.assertEqual(c["stage"], "execute")
        self.assertEqual(c["active_task"]["model"], "example-executor")

        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        self.assertEqual(c["stage"], "validate")
        self.assertEqual(c["active_task"]["model"], "example-validator")

        c = self.complete(c, self.passing_validation())
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["stage"], "examine")
        self.assertEqual(c["round_no"], 2)

    def test_failed_validation_creates_repair_then_revalidation(self):
        c = self.create_campaign()
        c = self.complete(c, self.proposal())
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        failure = {
            "schema": controller.SCHEMA,
            "role": "validate",
            "verdict": "fail",
            "build_passed": True,
            "tests_passed": False,
            "critical_issues": 0,
            "high_issues": 1,
            "regressions": 0,
            "findings": ["missing regression test"],
            "correction_prompt": "Add the missing test and fix the defect.",
        }
        failed_validator_id = c["active_task_id"]
        c = self.complete(c, failure)
        # complete() auto-answers the repair scope_validate card, landing on the repair executor.
        self.assertEqual(c["stage"], "execute")
        self.assertEqual(c["repair_no"], 1)

        board = kbc.connect(board="default")
        try:
            parents = board.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (c["active_task_id"],)
            ).fetchall()
            self.assertEqual(len(parents), 1)
            scope_validator_id = parents[0]["parent_id"]
            scope_parents = board.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (scope_validator_id,)
            ).fetchall()
            self.assertEqual(
                [row["parent_id"] for row in scope_parents], [failed_validator_id]
            )
        finally:
            board.close()

        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        self.assertEqual(c["stage"], "validate")

    def test_candidate_complete_requires_final_validation(self):
        c = self.create_campaign()
        c = self.complete(
            c,
            {
                "schema": controller.SCHEMA,
                "role": "examine",
                "verdict": "candidate_complete",
                "score_breakdown": {
                    name: 10.0 for name in controller.RANKING_CATEGORIES
                },
                "score_rationale": "No critical or high-value defect remains.",
            },
        )
        self.assertEqual(c["stage"], "final_validate")
        self.assertTrue(c["final_mode"])
        c = self.complete(c, self.passing_validation())
        self.assertEqual(c["state"], "succeeded")

    def test_ranked_local_campaign_prompt_names_configured_validator_without_publish(self):
        c = self.create_campaign(target_average=9.0, publish_on_success=False)
        body = controller._task_body(c, "examine")
        self.assertIn("example-validator", body)
        self.assertNotIn("Sol", body)
        self.assertNotIn("commits and pushes", body)

    def test_ranked_campaign_below_target_average_executes_improvement(self):
        c = self.create_campaign(target_average=9.0)
        self.assertIn("TARGET AVERAGE: 9", controller._task_body(c, "examine"))
        c = self.complete(c, self.proposal(scores=7.5))
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["stage"], "execute")
        self.assertEqual(c["last_average"], 7.5)
        self.assertEqual(set(c["last_ranking"]), set(controller.RANKING_CATEGORIES))

    def test_examiner_returns_prioritized_items_and_execute_card_uses_only_first(self):
        c = self.create_campaign(target_average=9.0)
        examine_body = controller._task_body(c, "examine")
        # The narrow examiner selects exactly one defect; no multi-item lists in the prompt.
        self.assertIn("single highest-priority defect", examine_body)
        self.assertNotIn("improvement_items", examine_body)
        self.assertNotIn("execution_slices", examine_body)

        scoped = self.scoped_item(
            "Favorite button click",
            "Test only the real FavoriteButton click behavior.",
            "focused favorite test passes",
        )
        proposal = self.proposal(scores=7.5, item=scoped)
        # A legacy examiner that still emits improvement_items must be rejected.
        legacy = dict(proposal)
        legacy["improvement_items"] = [
            self.scoped_item(
                "Unselected comparison work",
                "Implement comparison behavior later.",
                "comparison test passes",
                priority=2,
            ),
        ]
        rejected = self.complete(c, legacy, auto_scope=False)
        self.assertEqual(rejected["state"], "needs_review")
        self.assertIn("improvement_items", rejected["message"])

    def test_selected_defect_flows_through_scope_plan_and_execute_cards(self):
        c = self.create_campaign(target_average=9.0)
        scoped = self.scoped_item(
            "Favorite button click",
            "Test only the real FavoriteButton click behavior.",
            "focused favorite test passes",
        )
        c = self.complete(c, self.proposal(scores=7.5, item=scoped))
        self.assertEqual(c["stage"], "execute")
        board = kbc.connect(board="default")
        try:
            task = kb.get_task(board, c["active_task_id"])
        finally:
            board.close()
        self.assertIsNotNone(task)
        body = task.body
        self.assertIn("SELECTED ITEM", body)
        self.assertIn("Favorite button click", body)
        self.assertIn("Test only the real FavoriteButton click behavior.", body)
        self.assertIn("focused favorite test passes", body)
        self.assertIn("app.py", body)
        self.assertIn("Do not implement any other findings", body)

    def test_validator_receives_only_selected_item_and_read_only_contract(self):
        c = self.create_campaign(target_average=9.0)
        scoped = self.scoped_item(
            "Favorite button click",
            "Test only the real FavoriteButton click behavior.",
            "focused favorite test passes",
        )
        c = self.complete(c, self.proposal(scores=7.5, item=scoped))
        executor_id = c["active_task_id"]
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})

        self.assertEqual(c["stage"], "validate")
        board = kbc.connect(board="default")
        try:
            task = kb.get_task(board, c["active_task_id"])
            parents = board.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (c["active_task_id"],)
            ).fetchall()
        finally:
            board.close()
        self.assertIsNotNone(task)
        self.assertEqual([row["parent_id"] for row in parents], [executor_id])
        body = task.body
        self.assertIn("VALIDATION SCOPE: Favorite button click", body)
        self.assertIn("focused favorite test passes", body)
        self.assertIn("READ-ONLY VALIDATOR", body)
        self.assertIn("return FAIL", body)
        self.assertIn("lower-priority", body)

    def test_slices_run_serially_then_integrated_validation_then_fresh_examination(self):
        c = self.create_campaign()
        examiner_id = c["active_task_id"]
        c = self.complete(c, self.sliced_proposal())
        self.assertEqual((c["stage"], c["slice_index"], c["slice_count"]), ("execute", 0, 3))

        board = kbc.connect(board="default")
        try:
            first_executor = kb.get_task(board, c["active_task_id"])
            first_parents = board.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (c["active_task_id"],)
            ).fetchall()
            first_planner_id = first_parents[0]["parent_id"]
            first_planner = kb.get_task(board, first_planner_id)
            first_planner_parents = board.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (first_planner_id,)
            ).fetchall()
            first_scope_id = first_planner_parents[0]["parent_id"]
            first_scope_parents = board.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (first_scope_id,)
            ).fetchall()
        finally:
            board.close()
        self.assertEqual(len(first_parents), 1)
        # examiner → scope_validate → plan → execute.
        self.assertEqual(len(first_planner_parents), 1)
        self.assertIn("TRUSTED_PLAN_CONTRACT", first_planner.body)
        self.assertEqual(
            [row["parent_id"] for row in first_scope_parents], [examiner_id]
        )
        self.assertIn("SEQUENTIAL SLICE 1 OF 3", first_executor.body)
        self.assertNotIn("Use production functions in tests", first_executor.body)

        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        first_validator_id = c["active_task_id"]
        c = self.complete(c, self.passing_validation())
        self.assertEqual((c["stage"], c["slice_index"], c["repair_no"]), ("execute", 1, 0))
        board = kbc.connect(board="default")
        try:
            second_executor = kb.get_task(board, c["active_task_id"])
            second_parents = board.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (c["active_task_id"],)
            ).fetchall()
            second_scope_id = second_parents[0]["parent_id"]
            second_scope_parents = board.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (second_scope_id,)
            ).fetchall()
        finally:
            board.close()
        self.assertEqual(len(second_parents), 1)
        self.assertEqual(
            [row["parent_id"] for row in second_scope_parents], [first_validator_id]
        )
        self.assertIn("SEQUENTIAL SLICE 2 OF 3", second_executor.body)

        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        c = self.complete(c, self.passing_validation())
        self.assertEqual((c["stage"], c["slice_index"]), ("execute", 2))
        self.assertIn("slice 3/3", c["active_task"]["title"])

        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        last_validator_id = c["active_task_id"]
        c = self.complete(c, self.passing_validation())
        self.assertEqual((c["stage"], c["slice_index"]), ("integrate_validate", 3))
        board = kbc.connect(board="default")
        try:
            integrated = kb.get_task(board, c["active_task_id"])
            integrated_parents = board.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (c["active_task_id"],)
            ).fetchall()
        finally:
            board.close()
        self.assertEqual([row["parent_id"] for row in integrated_parents], [last_validator_id])
        self.assertIn("INTEGRATED VALIDATION", integrated.body)
        self.assertIn("Export the production seam", integrated.body)
        self.assertIn("Integrate the test runner", integrated.body)
        self.assertIn("READ-ONLY VALIDATOR", integrated.body)

        c = self.complete(c, self.passing_validation())
        self.assertEqual((c["state"], c["stage"], c["round_no"]), ("running", "examine", 2))
        self.assertEqual((c["slice_index"], c["slice_count"]), (0, 0))
        self.assertIsNone(c["selected_improvement"])

    def test_failed_slice_repairs_same_slice_before_advancing(self):
        c = self.create_campaign()
        c = self.complete(c, self.sliced_proposal())
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        failure = {
            "schema": controller.SCHEMA,
            "role": "validate",
            "verdict": "fail",
            "build_passed": True,
            "tests_passed": False,
            "critical_issues": 0,
            "high_issues": 0,
            "regressions": 1,
            "findings": ["server import starts the listener"],
            "correction_prompt": "Make server import safe without starting the listener.",
        }
        c = self.complete(c, failure)
        # complete() auto-answers the repair scope_validate card, landing on the repair executor.
        self.assertEqual((c["stage"], c["slice_index"], c["repair_no"]), ("execute", 0, 1))
        board = kbc.connect(board="default")
        try:
            repair = kb.get_task(board, c["active_task_id"])
        finally:
            board.close()
        self.assertIn("Export the production seam", repair.body)
        self.assertIn("Make server import safe", repair.body)
        self.assertNotIn("Use production functions in tests", repair.body)

        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        self.assertEqual((c["stage"], c["slice_index"]), ("validate", 0))

    def test_failed_integrated_validation_rejects_oversized_repair(self):
        c = self.create_campaign()
        c = self.complete(c, self.sliced_proposal())
        for _ in range(3):
            c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
            c = self.complete(c, self.passing_validation())
        self.assertEqual(c["stage"], "integrate_validate")
        failure = {
            "schema": controller.SCHEMA,
            "role": "validate",
            "verdict": "fail",
            "build_passed": True,
            "tests_passed": False,
            "critical_issues": 0,
            "high_issues": 0,
            "regressions": 1,
            "findings": ["cross-slice package test command fails"],
            "correction_prompt": "Fix the cross-slice package test command.",
        }
        failed_validator = c["active_task_id"]
        c = self.complete(c, failure)
        self.assertEqual(c["state"], "needs_review")
        self.assertEqual(c["stage"], "integrate_validate")
        self.assertEqual(c["active_task_id"], failed_validator)
        self.assertIn("oversized executor", c["message"])

    def test_ranked_campaign_at_target_average_starts_final_sol_validation(self):
        c = self.create_campaign(target_average=9.0)
        # The deterministic controller computes the average and owns the threshold decision.
        scores = dict(zip(controller.RANKING_CATEGORIES, (10, 9, 9, 8, 9)))
        c = self.complete(c, self.proposal(scores=scores))
        self.assertEqual(c["stage"], "final_validate")
        self.assertTrue(c["final_mode"])
        self.assertEqual(c["last_average"], 9.0)
        c = self.complete(c, self.passing_validation())
        self.assertEqual(c["state"], "succeeded")
        self.assertIn("average target", c["message"].lower())

    def test_ranked_campaign_requires_complete_numeric_breakdown(self):
        c = self.create_campaign(target_average=9.0)
        # Drop one category: the deterministic controller must reject the incomplete ranking.
        incomplete = self.proposal()
        incomplete["score_breakdown"] = dict(incomplete["score_breakdown"])
        incomplete["score_breakdown"].pop("test_quality")
        c = self.complete(c, incomplete)
        self.assertEqual(c["state"], "needs_review")
        self.assertIn("score_breakdown", c["message"])

    def test_ranked_campaign_rejects_early_candidate_complete(self):
        c = self.create_campaign(target_average=9.0)
        c = self.complete(
            c,
            {
                "schema": controller.SCHEMA,
                "role": "examine",
                "verdict": "candidate_complete",
                "score_breakdown": {name: 8.5 for name in controller.RANKING_CATEGORIES},
                "score_rationale": "Every category remains below the configured target.",
            },
        )
        self.assertEqual(c["state"], "needs_review")
        self.assertIn("below target", c["message"].lower())

    def test_final_sol_validation_publishes_before_success(self):
        c = self.create_campaign(target_average=9.0, publish_on_success=True)
        c = self.complete(c, self.proposal(scores=9.0))
        published = {
            "ok": True,
            "committed": True,
            "pushed": True,
            "commit": "0123456789abcdef",
            "remote": "origin",
            "branch": "rank-loop",
        }
        with mock.patch.object(controller, "_publish_success", return_value=published) as publish:
            c = self.complete(c, self.passing_validation())
        publish.assert_called_once()
        self.assertEqual(c["state"], "succeeded")
        self.assertTrue(c["last_publish_result"]["ok"])
        self.assertIn("published exact commit", c["message"])

    def test_publish_success_commits_pushes_and_excludes_runtime_artifacts(self):
        repo = Path(self.temp.name) / "publish-repo"
        remote = Path(self.temp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "baseline.txt").write_text("baseline\n")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)

        initial = controller._execution_git_state(str(repo))
        local_head_before = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        (repo / "improvement.txt").write_text("verified improvement\n")
        artifact = repo / ".quality-loop" / "worker.log"
        artifact.parent.mkdir()
        artifact.write_text("runtime-only\n")
        authenticated = controller._execution_git_state(str(repo))
        authenticated["publication"] = controller._publication_manifest(
            str(repo), ["improvement.txt"]
        )
        result = self.publish_to_local_test_remote(
            {
                "workspace": str(repo),
                "publish_remote": "origin",
                "publish_branch": "rank-loop",
                "commit_message": "quality-loop: verified target average",
                "initial_snapshot": initial,
                "authenticated_snapshot": authenticated,
            },
            remote,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["committed"])
        self.assertTrue(result["pushed"])
        remote_head = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/rank-loop"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(remote_head, result["commit"])
        self.assertFalse(result["local_ref_updated"])
        self.assertEqual(subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip(), local_head_before)
        tree = subprocess.run(
            ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", remote_head],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertIn("improvement.txt", tree)
        self.assertFalse(any(path.startswith(".quality-loop/") for path in tree))

    def test_no_delta_publication_pushes_existing_exact_commit(self):
        repo = Path(self.temp.name) / "no-delta-repo"
        remote = Path(self.temp.name) / "no-delta-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "baseline.txt").write_text("already excellent\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        initial = controller._execution_git_state(str(repo))
        result = self.publish_to_local_test_remote({
            "workspace": str(repo),
            "publish_remote": "origin",
            "publish_branch": "rank-loop",
            "commit_message": "unused for no delta",
            "initial_snapshot": initial,
            "authenticated_snapshot": initial,
        }, remote)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        remote_head = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/rank-loop"], check=True, capture_output=True, text=True).stdout.strip()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["committed"])
        self.assertTrue(result["pushed"])
        self.assertFalse(result["local_ref_updated"])
        self.assertEqual(result["commit"], head)
        self.assertEqual(remote_head, head)

    def test_post_push_local_mutation_preserves_truthful_success_result(self):
        repo = Path(self.temp.name) / "post-push-race-repo"
        remote = Path(self.temp.name) / "post-push-race-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        initial = controller._execution_git_state(str(repo))
        (repo / "improvement.txt").write_text("authenticated\n", encoding="utf-8")
        authenticated = controller._execution_git_state(str(repo))
        authenticated["publication"] = controller._publication_manifest(str(repo), ["improvement.txt"])
        old_head = initial["head"]
        tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], check=True, capture_output=True, text=True).stdout.strip()
        concurrent = subprocess.run(
            ["git", "-C", str(repo), "commit-tree", tree, "-p", old_head, "-m", "concurrent"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        real_git = controller._git_without_hooks
        raced = False

        def mutate_after_push(workspace, *args, **kwargs):
            nonlocal raced
            completed = real_git(workspace, *args, **kwargs)
            if not raced and args and args[0] == "push" and completed.returncode == 0:
                raced = True
                subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/rank-loop", concurrent, old_head], check=True)
                subprocess.run(["git", "-C", str(repo), "read-tree", concurrent], check=True)
            return completed

        with mock.patch.object(controller, "_git_without_hooks", side_effect=mutate_after_push), mock.patch.object(
            controller, "_safe_publication_remote_url", return_value=str(remote)
        ):
            result = controller._publish_success({
                "workspace": str(repo), "publish_remote": "origin", "publish_branch": "rank-loop",
                "commit_message": "publish", "initial_snapshot": initial,
                "authenticated_snapshot": authenticated,
            })
        remote_head = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/rank-loop"], check=True, capture_output=True, text=True).stdout.strip()
        self.assertTrue(raced)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["committed"])
        self.assertTrue(result["pushed"])
        self.assertEqual(remote_head, result["commit"])
        self.assertEqual(subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip(), concurrent)

    def test_publish_success_disables_repository_commit_hooks(self):
        repo = Path(self.temp.name) / "hook-repo"
        remote = Path(self.temp.name) / "hook-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "baseline.txt").write_text("baseline\n")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        hooks = repo / ".git" / "hooks"
        for hook_name in ("pre-commit", "prepare-commit-msg", "post-commit", "pre-push"):
            hook = hooks / hook_name
            hook.write_text(
                f"#!/bin/sh\nprintf 'injected\\n' > hook-{hook_name}-ran.txt\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)

        initial = controller._execution_git_state(str(repo))
        (repo / "improvement.txt").write_text("verified improvement\n")
        authenticated = controller._execution_git_state(str(repo))
        authenticated["publication"] = controller._publication_manifest(
            str(repo), ["improvement.txt"]
        )
        result = self.publish_to_local_test_remote(
            {
                "workspace": str(repo),
                "publish_remote": "origin",
                "publish_branch": "rank-loop",
                "commit_message": "quality-loop: hook-contained publication",
                "initial_snapshot": initial,
                "authenticated_snapshot": authenticated,
            },
            remote,
        )

        self.assertTrue(result["ok"], result)
        for hook_name in ("pre-commit", "prepare-commit-msg", "post-commit", "pre-push"):
            self.assertFalse((repo / f"hook-{hook_name}-ran.txt").exists())
        remote_head = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/rank-loop"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(remote_head, result["commit"])

    def test_publish_success_refuses_sensitive_paths(self):
        repo = Path(self.temp.name) / "secret-repo"
        remote = Path(self.temp.name) / "secret-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "baseline.txt").write_text("baseline\n")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        initial = controller._execution_git_state(str(repo))
        (repo / ".bundle").mkdir()
        (repo / ".bundle" / "config").write_text(
            'BUNDLE_GEMS__EXAMPLE__COM: "alice:plain-bundle-password"\n'
        )
        authenticated = controller._execution_git_state(str(repo))
        authenticated["publication"] = controller._publication_manifest(
            str(repo), [".bundle/config"]
        )

        result = controller._publish_success(
            {
                "workspace": str(repo),
                "publish_remote": "origin",
                "publish_branch": "rank-loop",
                "commit_message": "must not publish",
                "initial_snapshot": initial,
                "authenticated_snapshot": authenticated,
            }
        )
        self.assertFalse(result["ok"])
        self.assertIn("sensitive", result["error"])

    def test_publish_success_isolates_authenticated_tree_from_index_race(self):
        repo = Path(self.temp.name) / "publication-race-repo"
        remote = Path(self.temp.name) / "publication-race-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "baseline.txt").write_text("baseline\n")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        initial = controller._execution_git_state(str(repo))
        improvement = repo / "improvement.txt"
        improvement.write_text("authenticated bytes\n")
        authenticated = controller._execution_git_state(str(repo))
        authenticated["publication"] = controller._publication_manifest(
            str(repo), ["improvement.txt"]
        )
        real_git = controller._git_without_hooks
        raced = False

        def mutate_main_index_before_tree(workspace, *args, **kwargs):
            nonlocal raced
            if args and args[0] == "write-tree" and not raced:
                raced = True
                object_id = subprocess.run(
                    ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                    input="UNAUTHENTICATED INDEX BYTES\n",
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                subprocess.run(
                    [
                        "git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                        "100644", object_id, "improvement.txt",
                    ],
                    check=True,
                )
            return real_git(workspace, *args, **kwargs)

        with mock.patch.object(
            controller, "_git_without_hooks", side_effect=mutate_main_index_before_tree
        ):
            result = self.publish_to_local_test_remote(
                {
                    "workspace": str(repo),
                    "publish_remote": "origin",
                    "publish_branch": "rank-loop",
                    "commit_message": "publish only authenticated bytes",
                    "initial_snapshot": initial,
                    "authenticated_snapshot": authenticated,
                },
                remote,
            )

        self.assertTrue(raced)
        self.assertFalse(result["ok"], result)
        self.assertIn("control state changed", result["error"])
        remote_ref = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "--verify", "refs/heads/rank-loop"],
            check=False, capture_output=True, text=True,
        )
        self.assertNotEqual(remote_ref.returncode, 0)

    def test_publish_success_rejects_repository_controlled_gpg_program(self):
        repo = Path(self.temp.name) / "gpg-config-repo"
        remote = Path(self.temp.name) / "gpg-config-remote.git"
        marker = Path(self.temp.name) / "gpg-program-ran"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "baseline.txt").write_text("baseline\n")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        gpg = Path(self.temp.name) / "malicious-gpg.sh"
        gpg.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
        gpg.chmod(0o755)
        subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "true"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "gpg.program", str(gpg)], check=True)
        initial = controller._execution_git_state(str(repo))
        (repo / "improvement.txt").write_text("authenticated bytes\n")
        authenticated = controller._execution_git_state(str(repo))
        authenticated["publication"] = controller._publication_manifest(
            str(repo), ["improvement.txt"]
        )

        result = controller._publish_success(
            {
                "workspace": str(repo),
                "publish_remote": "origin",
                "publish_branch": "rank-loop",
                "commit_message": "must not execute repository gpg program",
                "initial_snapshot": initial,
                "authenticated_snapshot": authenticated,
            }
        )

        self.assertFalse(result["ok"])
        self.assertIn("repository-controlled executable", result["error"])
        self.assertFalse(marker.exists())

    def test_publish_success_rejects_diff_external_without_execution(self):
        repo = Path(self.temp.name) / "diff-external-repo"
        remote = Path(self.temp.name) / "diff-external-remote.git"
        marker = Path(self.temp.name) / "diff-external-ran"
        helper = Path(self.temp.name) / "diff-external.sh"
        helper.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
        helper.chmod(0o755)
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "diff.external", str(helper)], check=True)
        initial = controller._execution_git_state(str(repo))
        (repo / "tracked.txt").write_text("authenticated bytes\n", encoding="utf-8")
        authenticated = controller._execution_git_state(str(repo))
        authenticated["publication"] = controller._publication_manifest(str(repo), ["tracked.txt"])

        result = controller._publish_success(
            {
                "workspace": str(repo),
                "publish_remote": "origin",
                "publish_branch": "rank-loop",
                "commit_message": "must reject diff.external",
                "initial_snapshot": initial,
                "authenticated_snapshot": authenticated,
            }
        )

        self.assertFalse(result["ok"])
        self.assertIn("repository-controlled executable", result["error"])
        self.assertFalse(marker.exists())

    def test_execution_snapshot_never_runs_repository_clean_filter(self):
        repo = Path(self.temp.name) / "clean-filter-repo"
        marker = Path(self.temp.name) / "clean-filter-ran"
        helper = Path(self.temp.name) / "clean-filter.sh"
        helper.write_text(
            f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8"
        )
        helper.chmod(0o755)
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / ".gitattributes").write_text("tracked.txt filter=evil\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitattributes", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.evil.clean", str(helper)], check=True
        )

        snapshot = controller._execution_git_state(str(repo))

        self.assertEqual(snapshot["files"], {})
        self.assertFalse(marker.exists())

    def test_publish_transport_ignores_config_added_after_safety_scan(self):
        repo = Path(self.temp.name) / "config-race-repo"
        remote = Path(self.temp.name) / "config-race-remote.git"
        transport_marker = Path(self.temp.name) / "transport-helper-ran"
        fsmonitor_marker = Path(self.temp.name) / "fsmonitor-helper-ran"
        textconv_marker = Path(self.temp.name) / "textconv-helper-ran"
        transport_helper = Path(self.temp.name) / "transport-helper.sh"
        fsmonitor_helper = Path(self.temp.name) / "fsmonitor-helper.sh"
        textconv_helper = Path(self.temp.name) / "textconv-helper.sh"
        transport_helper.write_text(
            f"#!/bin/sh\ntouch {transport_marker}\nexit 1\n", encoding="utf-8"
        )
        fsmonitor_helper.write_text(
            f"#!/bin/sh\ntouch {fsmonitor_marker}\nexit 1\n", encoding="utf-8"
        )
        textconv_helper.write_text(
            f"#!/bin/sh\ntouch {textconv_marker}\ncat \"$1\"\n", encoding="utf-8"
        )
        transport_helper.chmod(0o755)
        fsmonitor_helper.chmod(0o755)
        textconv_helper.chmod(0o755)
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        (repo / ".gitattributes").write_text("tracked.txt diff=evil\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt", ".gitattributes"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        initial = controller._execution_git_state(str(repo))
        (repo / "tracked.txt").write_text("authenticated bytes\n", encoding="utf-8")
        authenticated = controller._execution_git_state(str(repo))
        authenticated["publication"] = controller._publication_manifest(str(repo), ["tracked.txt"])
        real_scan = controller._unsafe_publication_git_config

        def race_config(workspace, **kwargs):
            result = real_scan(workspace, **kwargs)
            subprocess.run(
                ["git", "-C", workspace, "config", f"url.ext::{transport_helper}.insteadOf", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", workspace, "config", "protocol.ext.allow", "always"], check=True
            )
            subprocess.run(
                ["git", "-C", workspace, "config", "core.fsmonitor", str(fsmonitor_helper)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", workspace, "config", "diff.evil.textconv", str(textconv_helper)],
                check=True,
            )
            return result

        with mock.patch.object(
            controller, "_unsafe_publication_git_config", side_effect=race_config
        ):
            result = self.publish_to_local_test_remote(
                {
                    "workspace": str(repo),
                    "publish_remote": "origin",
                    "publish_branch": "rank-loop",
                    "commit_message": "must isolate mutable repository config",
                    "initial_snapshot": initial,
                    "authenticated_snapshot": authenticated,
                },
                remote,
            )

        self.assertFalse(result["ok"], result)
        self.assertIn("control state changed", result["error"])
        self.assertFalse(transport_marker.exists())
        self.assertFalse(fsmonitor_marker.exists())
        self.assertFalse(textconv_marker.exists())

    def test_publish_success_rejects_ext_transport_without_executing_helper(self):
        repo = Path(self.temp.name) / "ext-repo"
        marker = Path(self.temp.name) / "ext-helper-ran"
        helper = Path(self.temp.name) / "ext-helper.sh"
        helper.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
        helper.chmod(0o755)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "baseline.txt").write_text("baseline\n")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", f"ext::{helper}"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "protocol.ext.allow", "always"], check=True
        )
        initial = controller._execution_git_state(str(repo))
        (repo / "improvement.txt").write_text("authenticated bytes\n")
        authenticated = controller._execution_git_state(str(repo))
        authenticated["publication"] = controller._publication_manifest(
            str(repo), ["improvement.txt"]
        )

        result = controller._publish_success(
            {
                "workspace": str(repo),
                "publish_remote": "origin",
                "publish_branch": "rank-loop",
                "commit_message": "must reject ext transport",
                "initial_snapshot": initial,
                "authenticated_snapshot": authenticated,
            }
        )

        self.assertFalse(result["ok"])
        self.assertRegex(
            result["error"], "unsafe publication configuration|repository-controlled executable"
        )
        self.assertFalse(marker.exists())

    def test_publish_success_rejects_local_remote_receive_hooks(self):
        repo = Path(self.temp.name) / "local-hook-repo"
        remote = Path(self.temp.name) / "local-hook-remote.git"
        marker = Path(self.temp.name) / "receive-hook-ran"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        hook = remote / "hooks" / "pre-receive"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(0o755)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "baseline.txt").write_text("baseline\n")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        initial = controller._execution_git_state(str(repo))
        (repo / "improvement.txt").write_text("authenticated bytes\n")
        authenticated = controller._execution_git_state(str(repo))
        authenticated["publication"] = controller._publication_manifest(
            str(repo), ["improvement.txt"]
        )

        result = controller._publish_success(
            {
                "workspace": str(repo),
                "publish_remote": "origin",
                "publish_branch": "rank-loop",
                "commit_message": "must reject local receive hooks",
                "initial_snapshot": initial,
                "authenticated_snapshot": authenticated,
            }
        )

        self.assertFalse(result["ok"])
        self.assertIn("local publication remotes", result["error"])
        self.assertFalse(marker.exists())

    def test_publish_success_ignores_path_shadowed_git(self):
        repo = Path(self.temp.name) / "path-repo"
        remote = Path(self.temp.name) / "path-remote.git"
        fake_dir = Path(self.temp.name) / "fake-bin"
        fake_dir.mkdir()
        marker = Path(self.temp.name) / "fake-git-ran"
        fake_git = fake_dir / "git"
        fake_git.write_text(
            f"#!/bin/sh\ntouch {marker}\nexec /usr/bin/git \"$@\"\n", encoding="utf-8"
        )
        fake_git.chmod(0o755)
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "rank-loop", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "baseline.txt").write_text("baseline\n")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        with mock.patch.dict(os.environ, {"PATH": f"{fake_dir}:{os.environ.get('PATH', '')}"}):
            initial = controller._execution_git_state(str(repo))
            (repo / "improvement.txt").write_text("authenticated bytes\n")
            authenticated = controller._execution_git_state(str(repo))
            authenticated["publication"] = controller._publication_manifest(
                str(repo), ["improvement.txt"]
            )
            result = self.publish_to_local_test_remote(
                {
                    "workspace": str(repo),
                    "publish_remote": "origin",
                    "publish_branch": "rank-loop",
                    "commit_message": "ignore path-shadowed git",
                    "initial_snapshot": initial,
                    "authenticated_snapshot": authenticated,
                },
                remote,
            )

        self.assertTrue(result["ok"], result)
        self.assertFalse(marker.exists())

    def test_create_validation_ignores_path_shadowed_git(self):
        fake_dir = Path(self.temp.name) / "create-fake-bin"
        fake_dir.mkdir()
        marker = Path(self.temp.name) / "create-fake-git-ran"
        fake_git = fake_dir / "git"
        fake_git.write_text(
            f"#!/bin/sh\ntouch {marker}\nexec /usr/bin/git \"$@\"\n", encoding="utf-8"
        )
        fake_git.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{fake_dir}:{os.environ.get('PATH', '')}"}):
            campaign = self.create_campaign()
        self.assertEqual(campaign["workspace"], str(self.workspace))
        self.assertFalse(marker.exists())

    def test_create_validation_never_runs_repository_clean_filter(self):
        marker = Path(self.temp.name) / "create-clean-filter-ran"
        helper = Path(self.temp.name) / "create-clean-filter.sh"
        helper.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
        helper.chmod(0o755)
        (self.workspace / ".gitattributes").write_text("app.py filter=evil\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", ".gitattributes"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-m", "attributes"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "filter.evil.clean", str(helper)],
            check=True,
        )
        app = self.workspace / "app.py"
        app.write_bytes(app.read_bytes())

        campaign = self.create_campaign()

        self.assertEqual(campaign["workspace"], str(self.workspace))
        self.assertFalse(marker.exists())

    def test_raw_snapshot_rejects_escaping_git_paths(self):
        oid = "0" * 40
        with self.assertRaisesRegex(RuntimeError, "unsafe workspace path"):
            controller._parse_tree_entries(f"100644 blob {oid}\t../secret\0".encode())
        with self.assertRaisesRegex(RuntimeError, "unsafe workspace path"):
            controller._parse_index_entries(f"100644 {oid} 0\t../secret\0".encode())
        with self.assertRaisesRegex(RuntimeError, "unsafe workspace path"):
            controller._parse_tree_entries(f"100644 blob {oid}\t/absolute\0".encode())
        with self.assertRaisesRegex(RuntimeError, "unsafe workspace path"):
            controller._parse_index_entries(f"100644 {oid} 0\tdir\\secret\0".encode())

        outside = Path(self.temp.name) / "outside-tree"
        outside.mkdir()
        (outside / "secret").write_text("secret", encoding="utf-8")
        (self.workspace / "linked-parent").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "escapes the canonical root"):
            controller._safe_workspace_candidate(self.workspace, "linked-parent/secret")

    def test_descriptor_reads_resist_parent_symlink_swap(self):
        repo = Path(self.temp.name) / "descriptor-race-repo"
        outside = Path(self.temp.name) / "descriptor-race-outside"
        repo.mkdir()
        outside.mkdir()
        (repo / "sub").mkdir()
        (repo / "sub" / "victim.txt").write_bytes(b"BASELINE")
        (outside / "victim.txt").write_bytes(b"OUTSIDE-SECRET")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "sub/victim.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "baseline"], check=True)
        (repo / "sub" / "victim.txt").write_bytes(b"INSIDE")
        baseline = b"INSIDE"
        digest = hashlib.sha1(f"blob {len(baseline)}\0".encode() + baseline).hexdigest()
        real_open = os.open

        def swap_after_parent_open(path, flags, mode=0o777, *, dir_fd=None):
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == "sub" and dir_fd is not None and not (repo / "parked-sub").exists():
                (repo / "sub").rename(repo / "parked-sub")
                (repo / "sub").symlink_to(outside, target_is_directory=True)
            return descriptor

        with mock.patch.object(controller.os, "open", side_effect=swap_after_parent_open):
            self.assertEqual(
                controller._raw_worktree_entry(repo, "sub/victim.txt", "sha1"),
                ("100644", digest),
            )
        (repo / "sub").unlink()
        (repo / "parked-sub").rename(repo / "sub")

        with mock.patch.object(controller.os, "open", side_effect=swap_after_parent_open):
            manifest = controller._publication_manifest(str(repo), ["sub/victim.txt"])
        self.assertEqual(manifest, {"sub/victim.txt": f"100644:{digest}"})
        (repo / "sub").unlink()
        (repo / "parked-sub").rename(repo / "sub")
        base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        with mock.patch.object(controller.os, "open", side_effect=swap_after_parent_open):
            tree = controller._authenticated_publication_tree(str(repo), base, manifest)
        self.assertEqual(
            controller._tree_publication_manifest(str(repo), tree, ["sub/victim.txt"]),
            manifest,
        )

    def test_workspace_root_swap_fails_closed(self):
        anchor = Path(self.temp.name) / "root-race-anchor"
        repo = anchor / "repo"
        outside_anchor = Path(self.temp.name) / "root-race-outside"
        outside_repo = outside_anchor / "repo"
        repo.mkdir(parents=True)
        outside_repo.mkdir(parents=True)
        (repo / "victim.txt").write_bytes(b"INSIDE")
        (outside_repo / "victim.txt").write_bytes(b"OUTSIDE-SECRET")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "victim.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "baseline"], check=True)
        real_git_command = controller._git_command

        def swap_after_format(workspace, *args, **kwargs):
            result = real_git_command(workspace, *args, **kwargs)
            parked = Path(self.temp.name) / "parked-anchor"
            if args == ("rev-parse", "--show-object-format") and not parked.exists():
                anchor.rename(parked)
                anchor.symlink_to(outside_anchor, target_is_directory=True)
            return result

        with mock.patch.object(controller, "_git_command", side_effect=swap_after_format):
            with self.assertRaisesRegex(RuntimeError, "workspace root changed"):
                controller._publication_manifest(str(repo), ["victim.txt"])

    def test_workspace_root_aba_via_symlink_is_rejected_and_git_stays_fd_bound(self):
        anchor = Path(self.temp.name) / "aba-anchor"
        repo = anchor / "repo"
        repo.mkdir(parents=True)
        (repo / "tracked.txt").write_text("inside", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "baseline"], check=True)
        root_fd = controller._open_workspace_root(repo)
        parked = Path(self.temp.name) / "aba-parked"
        try:
            anchor.rename(parked)
            anchor.symlink_to(parked, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "Git control directory changed"):
                controller._git_control_snapshot(repo, root_fd=root_fd)
            self.assertFalse(controller._workspace_root_handle_matches(root_fd, repo))
        finally:
            os.close(root_fd)

    def test_campaign_creation_pins_git_directory_against_swap_and_restore(self):
        repo = Path(self.temp.name) / "git-swap-victim"
        attacker = Path(self.temp.name) / "git-swap-attacker"
        for path, content in ((repo, "clean\n"), (attacker, "dirty\n")):
            path.mkdir()
            (path / "app.py").write_text(content, encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.name", "Quality Loop Test"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.email", "quality-loop@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
            subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "baseline"], check=True)
        (repo / "app.py").write_text("dirty\n", encoding="utf-8")
        real_run = controller._snapshot_git_run
        parked = Path(self.temp.name) / "victim-git-parked"
        attacker_parked = attacker / ".git"

        def swap_git_only_during_command(root, *args, **kwargs):
            (repo / ".git").rename(parked)
            attacker_parked.rename(repo / ".git")
            try:
                return real_run(root, *args, **kwargs)
            finally:
                (repo / ".git").rename(attacker_parked)
                parked.rename(repo / ".git")

        with mock.patch.object(controller, "_snapshot_git_run", side_effect=swap_git_only_during_command):
            with self.assertRaisesRegex(ValueError, "clean isolated Git workspace"):
                self.create_campaign(workspace=str(repo))
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            "M app.py",
        )

    def test_git_control_snapshot_resists_nested_refs_symlink_swap(self):
        baseline = controller._git_control_snapshot(self.workspace)
        refs = self.workspace / ".git" / "refs"
        parked = self.workspace / ".git" / "refs-parked"
        outside = Path(self.temp.name) / "outside-refs"
        outside.mkdir()
        (outside / "outside-secret").write_text("OUTSIDE-REF-SECRET", encoding="utf-8")
        real_listdir = os.listdir
        swapped = False

        def swap_after_refs_enumeration(path):
            nonlocal swapped
            names = real_listdir(path)
            try:
                resolved = Path(os.readlink(f"/proc/self/fd/{path}")) if isinstance(path, int) else Path(path)
            except OSError:
                resolved = Path("/")
            if not swapped and resolved == refs:
                swapped = True
                refs.rename(parked)
                refs.symlink_to(outside, target_is_directory=True)
                refs.unlink()
                parked.rename(refs)
            return names

        try:
            with mock.patch.object(controller.os, "listdir", side_effect=swap_after_refs_enumeration):
                raced = controller._git_control_snapshot(self.workspace)
            self.assertEqual(raced, baseline)
            self.assertTrue(swapped)
        finally:
            if refs.is_symlink():
                refs.unlink()
            if parked.exists():
                parked.rename(refs)

    def test_campaign_creation_pins_refs_against_command_only_aba(self):
        victim = Path(self.temp.name) / "refs-victim"
        attacker = Path(self.temp.name) / "refs-attacker"
        for path, content in ((victim, "old\n"), (attacker, "new\n")):
            path.mkdir()
            (path / "app.py").write_text(content, encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.name", "Quality Loop Test"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.email", "quality-loop@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
            subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "baseline"], check=True)
        (victim / "app.py").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(victim), "add", "app.py"], check=True)
        real_run = controller._snapshot_git_run
        victim_refs = victim / ".git" / "refs"
        victim_parked = victim / ".git" / "refs-parked"
        attacker_refs = attacker / ".git" / "refs"

        def swap_refs_only_during_command(root, *args, **kwargs):
            victim_refs.rename(victim_parked)
            attacker_refs.rename(victim_refs)
            try:
                return real_run(root, *args, **kwargs)
            finally:
                victim_refs.rename(attacker_refs)
                victim_parked.rename(victim_refs)

        with mock.patch.object(controller, "_snapshot_git_run", side_effect=swap_refs_only_during_command):
            with self.assertRaisesRegex(ValueError, "clean isolated Git workspace"):
                self.create_campaign(workspace=str(victim))
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(victim), "status", "--porcelain"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            "M  app.py",
        )

    def test_campaign_creation_rejects_loose_and_packed_ref_read_aba(self):
        for storage in ("loose", "packed"):
            with self.subTest(storage=storage):
                repo = Path(self.temp.name) / f"ref-file-{storage}"
                repo.mkdir()
                (repo / "app.py").write_text("A\n", encoding="utf-8")
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
                subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
                subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
                subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "A"], check=True)
                oid_a = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
                (repo / "app.py").write_text("B\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(repo), "commit", "-qam", "B"], check=True)
                oid_b = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
                subprocess.run(["git", "-C", str(repo), "reset", "--hard", oid_a], check=True, capture_output=True)
                (repo / "app.py").write_text("B\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
                head_ref = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
                target = repo / ".git" / head_ref if storage == "loose" else repo / ".git" / "packed-refs"
                if storage == "packed":
                    subprocess.run(["git", "-C", str(repo), "pack-refs", "--all", "--prune"], check=True)
                    target = repo / ".git" / "packed-refs"
                    attacker_bytes = target.read_bytes().replace(oid_a.encode(), oid_b.encode())
                else:
                    attacker_bytes = (oid_b + "\n").encode()
                attacker = Path(self.temp.name) / f"attacker-{storage}"
                parked = Path(self.temp.name) / f"parked-{storage}"
                attacker.write_bytes(attacker_bytes)
                real_open = controller._open_retained_control_file
                raced = False

                def swap_only_during_read(directory_fd, name, limit=64 * 1024):
                    nonlocal raced
                    if not raced and name == target.name:
                        raced = True
                        target.rename(parked)
                        attacker.rename(target)
                        try:
                            return real_open(directory_fd, name, limit)
                        finally:
                            target.rename(attacker)
                            parked.rename(target)
                    return real_open(directory_fd, name, limit)

                with mock.patch.object(controller, "_open_retained_control_file", side_effect=swap_only_during_read):
                    with self.assertRaisesRegex(RuntimeError, "Git control directory changed"):
                        self.create_campaign(workspace=str(repo))
                self.assertTrue(raced)
                self.assertEqual(subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                    capture_output=True, text=True,
                ).stdout.strip(), oid_a)

    def test_same_inode_loose_ref_aba_fails_closed(self):
        repo = Path(self.temp.name) / "same-inode-ref"
        repo.mkdir()
        (repo / "app.py").write_text("A\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "A"], check=True)
        oid_a = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        (repo / "app.py").write_text("B\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "commit", "-qam", "B"], check=True)
        oid_b = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "reset", "--hard", oid_a], check=True, capture_output=True)
        (repo / "app.py").write_text("B\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
        head_ref = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        target = repo / ".git" / head_ref
        real_open = controller._open_retained_control_file
        raced = False

        def in_place_aba(directory_fd, name, limit=64 * 1024):
            nonlocal raced
            if not raced and name == target.name:
                raced = True
                target.write_text(oid_b + "\n", encoding="ascii")
                try:
                    return real_open(directory_fd, name, limit)
                finally:
                    target.write_text(oid_a + "\n", encoding="ascii")
            return real_open(directory_fd, name, limit)

        with mock.patch.object(controller, "_open_retained_control_file", side_effect=in_place_aba):
            with self.assertRaisesRegex(RuntimeError, "Git control directory changed"):
                controller._execution_git_state(str(repo))
        self.assertTrue(raced)

    def test_sha256_repository_snapshot_uses_matching_shim_format(self):
        repo = Path(self.temp.name) / "sha256-repo"
        repo.mkdir()
        supported = subprocess.run(
            ["git", "init", "-q", "--object-format=sha256", str(repo)],
            check=False, capture_output=True, text=True,
        )
        if supported.returncode != 0:
            self.skipTest("installed Git does not support SHA-256 repositories")
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "quality-loop@example.invalid"], check=True)
        (repo / "app.py").write_text("sha256\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "baseline"], check=True)
        snapshot = controller._execution_git_state(str(repo))
        self.assertEqual(len(snapshot["head"]), 64)
        self.assertEqual(snapshot["files"], {})

    def test_campaign_creation_ignores_command_only_config_swap(self):
        untracked = self.workspace / "attacker.bin"
        untracked.write_text("must remain visible\n", encoding="utf-8")
        git_dir = self.workspace / ".git"
        config = git_dir / "config"
        parked = git_dir / "config-parked"
        attacker_config = Path(self.temp.name) / "attacker-config"
        excludes = Path(self.temp.name) / "attacker-excludes"
        excludes.write_text("attacker.bin\n", encoding="utf-8")
        attacker_config.write_bytes(config.read_bytes())
        subprocess.run(
            ["git", "config", "--file", str(attacker_config), "core.excludesFile", str(excludes)],
            check=True,
        )
        real_run = controller._snapshot_git_run

        def swap_config_only_during_command(root, *args, **kwargs):
            config.rename(parked)
            attacker_config.rename(config)
            try:
                return real_run(root, *args, **kwargs)
            finally:
                config.rename(attacker_config)
                parked.rename(config)

        with mock.patch.object(controller, "_snapshot_git_run", side_effect=swap_config_only_during_command):
            with self.assertRaisesRegex(ValueError, "clean isolated Git workspace"):
                self.create_campaign()
        self.assertTrue(untracked.exists())

    def test_campaign_creation_uses_retained_index_during_command_swap(self):
        (self.workspace / "app.py").write_text("staged attacker bytes\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "app.py"], check=True)
        git_dir = self.workspace / ".git"
        index = git_dir / "index"
        dirty_index = git_dir / "index-dirty"
        clean_index = Path(self.temp.name) / "clean-index"
        subprocess.run(["git", "-C", str(self.workspace), "read-tree", "HEAD"], check=True)
        clean_index.write_bytes(index.read_bytes())
        subprocess.run(["git", "-C", str(self.workspace), "add", "app.py"], check=True)
        real_run = controller._snapshot_git_run

        def swap_index_only_during_command(root, *args, **kwargs):
            index.rename(dirty_index)
            clean_index.rename(index)
            try:
                return real_run(root, *args, **kwargs)
            finally:
                index.rename(clean_index)
                dirty_index.rename(index)

        with mock.patch.object(controller, "_snapshot_git_run", side_effect=swap_index_only_during_command):
            with self.assertRaisesRegex(ValueError, "clean isolated Git workspace"):
                self.create_campaign()
        self.assertIn("M  app.py", subprocess.run(
            ["git", "-C", str(self.workspace), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout)

    @unittest.skip("publication deliberately never mutates local refs")
    def test_pinned_ref_update_resists_refs_symlink_aba(self):
        root_fd = controller._open_workspace_root(self.workspace)
        outside = Path(self.temp.name) / "outside-update-refs"
        (outside / "heads").mkdir(parents=True)
        outside_ref = outside / "heads" / "master"
        old_oid = subprocess.run(
            ["git", "-C", str(self.workspace), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        outside_ref.write_text(old_oid + "\n", encoding="ascii")
        new_oid = "1" * len(old_oid)
        refs = self.workspace / ".git" / "refs"
        parked = self.workspace / ".git" / "refs-update-parked"
        real_rename = os.rename
        swapped = False
        try:
            with controller._pinned_git_context(self.workspace, root_fd) as context:
                _oid, head_ref = controller._pinned_head(context)
                self.assertIsNotNone(head_ref)

                def swap_during_ref_rename(src, dst, *args, **kwargs):
                    nonlocal swapped
                    if not swapped and kwargs.get("src_dir_fd") is not None:
                        swapped = True
                        refs.rename(parked)
                        refs.symlink_to(outside, target_is_directory=True)
                        try:
                            return real_rename(src, dst, *args, **kwargs)
                        finally:
                            refs.unlink()
                            parked.rename(refs)
                    return real_rename(src, dst, *args, **kwargs)

                with mock.patch.object(controller.os, "rename", side_effect=swap_during_ref_rename):
                    controller._write_pinned_ref(context, head_ref, new_oid, old_oid)
                self.assertEqual(controller._pinned_ref_oid(context, head_ref), new_oid)
            self.assertEqual(outside_ref.read_text(encoding="ascii").strip(), old_oid)
            self.assertTrue(swapped)
        finally:
            os.close(root_fd)

    @unittest.skip("publication deliberately never mutates local refs")
    def test_pinned_ref_update_is_compare_and_swap_after_lock(self):
        root_fd = controller._open_workspace_root(self.workspace)
        old_oid = subprocess.run(
            ["git", "-C", str(self.workspace), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(self.workspace), "rev-parse", "HEAD^{tree}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        concurrent_oid = subprocess.run(
            ["git", "-C", str(self.workspace), "commit-tree", tree, "-p", old_oid, "-m", "concurrent"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        publication_oid = "1" * len(old_oid)
        real_open = os.open
        raced = False
        try:
            with self.assertRaisesRegex(RuntimeError, "Git control directory changed"):
                with controller._pinned_git_context(self.workspace, root_fd) as context:
                    _head, head_ref = controller._pinned_head(context)
                    self.assertIsNotNone(head_ref)

                    def advance_before_lock(path, flags, *args, **kwargs):
                        nonlocal raced
                        if not raced and str(path).endswith(".lock") and flags & os.O_EXCL:
                            raced = True
                            subprocess.run(
                                ["git", "-C", str(self.workspace), "update-ref", head_ref, concurrent_oid, old_oid],
                                check=True,
                            )
                        return real_open(path, flags, *args, **kwargs)

                    with mock.patch.object(controller.os, "open", side_effect=advance_before_lock):
                        with self.assertRaisesRegex(RuntimeError, "changed before update"):
                            controller._write_pinned_ref(
                                context, head_ref, publication_oid, old_oid
                            )
            actual = subprocess.run(
                ["git", "-C", str(self.workspace), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(actual, concurrent_oid)
            self.assertTrue(raced)
        finally:
            os.close(root_fd)

    @unittest.skip("publication deliberately never mutates local refs")
    def test_packed_ref_update_detects_concurrent_advance_and_repack(self):
        subprocess.run(["git", "-C", str(self.workspace), "pack-refs", "--all", "--prune"], check=True)
        root_fd = controller._open_workspace_root(self.workspace)
        old_oid = subprocess.run(["git", "-C", str(self.workspace), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        tree = subprocess.run(["git", "-C", str(self.workspace), "rev-parse", "HEAD^{tree}"], check=True, capture_output=True, text=True).stdout.strip()
        concurrent_oid = subprocess.run(["git", "-C", str(self.workspace), "commit-tree", tree, "-p", old_oid, "-m", "concurrent"], check=True, capture_output=True, text=True).stdout.strip()
        real_open = os.open
        raced = False
        try:
            with self.assertRaisesRegex(RuntimeError, "changed before update"):
                with controller._pinned_git_context(self.workspace, root_fd) as context:
                    _head, head_ref = controller._pinned_head(context)
                    self.assertIsNone(context.ref_info)

                    def advance_and_pack(path, flags, *args, **kwargs):
                        nonlocal raced
                        if not raced and str(path).endswith(".lock") and flags & os.O_EXCL:
                            raced = True
                            subprocess.run(["git", "-C", str(self.workspace), "update-ref", head_ref, concurrent_oid, old_oid], check=True)
                            subprocess.run(["git", "-C", str(self.workspace), "pack-refs", "--all", "--prune"], check=True)
                        return real_open(path, flags, *args, **kwargs)

                    with mock.patch.object(controller.os, "open", side_effect=advance_and_pack):
                        controller._write_pinned_ref(context, head_ref, "1" * len(old_oid), old_oid)
            self.assertEqual(subprocess.run(
                ["git", "-C", str(self.workspace), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip(), concurrent_oid)
            self.assertTrue(raced)
        finally:
            os.close(root_fd)

    @unittest.skip("publication deliberately never mutates local refs")
    def test_ref_parent_replacement_fails_before_detached_write(self):
        root_fd = controller._open_workspace_root(self.workspace)
        old_oid = subprocess.run(["git", "-C", str(self.workspace), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        tree = subprocess.run(["git", "-C", str(self.workspace), "rev-parse", "HEAD^{tree}"], check=True, capture_output=True, text=True).stdout.strip()
        concurrent_oid = subprocess.run(["git", "-C", str(self.workspace), "commit-tree", tree, "-p", old_oid, "-m", "concurrent"], check=True, capture_output=True, text=True).stdout.strip()
        real_open = os.open
        heads = self.workspace / ".git" / "refs" / "heads"
        parked = self.workspace / ".git" / "refs" / "heads-old"
        swapped = False
        head_ref = ""
        try:
            with self.assertRaisesRegex(RuntimeError, "ref parent changed"):
                with controller._pinned_git_context(self.workspace, root_fd) as context:
                    _head, head_ref = controller._pinned_head(context)

                    def replace_parent(path, flags, *args, **kwargs):
                        nonlocal swapped
                        if not swapped and str(path).endswith(".lock") and flags & os.O_EXCL:
                            swapped = True
                            heads.rename(parked)
                            heads.mkdir()
                            (heads / Path(head_ref).relative_to("refs/heads")).write_text(
                                concurrent_oid + "\n", encoding="ascii"
                            )
                        return real_open(path, flags, *args, **kwargs)

                    with mock.patch.object(controller.os, "open", side_effect=replace_parent):
                        controller._write_pinned_ref(context, head_ref, "1" * len(old_oid), old_oid)
            self.assertEqual((parked / Path(head_ref).relative_to("refs/heads")).read_text().strip(), old_oid)
            self.assertEqual(subprocess.run(
                ["git", "-C", str(self.workspace), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip(), concurrent_oid)
            self.assertTrue(swapped)
        finally:
            os.close(root_fd)

    def test_execution_snapshot_supports_linked_worktree_git_file(self):
        source = Path(self.temp.name) / "worktree-source"
        linked = Path(self.temp.name) / "worktree-linked"
        source.mkdir()
        (source / "app.py").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Quality Loop Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "quality-loop@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(source), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "baseline"], check=True)
        subprocess.run(["git", "-C", str(source), "worktree", "add", "-q", "-b", "linked", str(linked)], check=True)
        self.assertTrue((linked / ".git").is_file())
        snapshot = controller._execution_git_state(str(linked))
        self.assertEqual(snapshot["files"], {})
        self.assertTrue(snapshot["head"])

    def test_ignored_read_limit_is_enforced_after_file_growth(self):
        growing = self.workspace / "growing.bin"
        growing.write_bytes(b"")
        real_fstat = os.fstat
        grown = False

        def grow_after_fstat(fd):
            nonlocal grown
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_size == 0 and not grown:
                grown = True
                growing.write_bytes(b"x" * (2 * 1024 * 1024))
            return info

        with mock.patch.object(controller.os, "fstat", side_effect=grow_after_fstat):
            with self.assertRaisesRegex(RuntimeError, "bounded read limit"):
                controller._read_workspace_entry(
                    self.workspace, "growing.bin", content_limit=1
                )

    def test_ignored_snapshot_charges_growth_at_exact_global_limit(self):
        (self.workspace / ".gitignore").write_text("grow-*.bin\n", encoding="utf-8")
        targets = [self.workspace / "grow-a.bin", self.workspace / "grow-b.bin"]
        for target in targets:
            target.write_bytes(b"")
        real_fstat = os.fstat
        grown: set[str] = set()

        def grow_each_after_initial_fstat(fd):
            info = real_fstat(fd)
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                return info
            if info.st_size == 0 and target in {str(path) for path in targets} and target not in grown:
                grown.add(target)
                Path(target).write_bytes(b"x")
            return info

        with mock.patch.object(controller, "_MAX_IGNORED_HASH_BYTES", 1), mock.patch.object(
            controller.os, "fstat", side_effect=grow_each_after_initial_fstat
        ):
            with self.assertRaisesRegex(RuntimeError, "bounded read limit"):
                controller._ignored_snapshot(self.workspace)
        self.assertEqual(grown, {str(path) for path in targets})

    def test_relevant_file_validation_ignores_path_shadowed_git(self):
        fake_dir = Path(self.temp.name) / "relevant-fake-bin"
        fake_dir.mkdir()
        marker = Path(self.temp.name) / "relevant-fake-git-ran"
        fake_git = fake_dir / "git"
        fake_git.write_text(
            f"#!/bin/sh\ntouch {marker}\nexec /usr/bin/git \"$@\"\n", encoding="utf-8"
        )
        fake_git.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{fake_dir}:{os.environ.get('PATH', '')}"}):
            error = controller._relevant_files_error(self.workspace, ["app.py"])
        self.assertIsNone(error)
        self.assertFalse(marker.exists())

    def test_snapshot_rejects_replacement_refs_and_hidden_index_flags(self):
        original = subprocess.run(
            ["git", "-C", str(self.workspace), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with (self.workspace / "hidden.txt").open("w", encoding="utf-8") as handle:
            handle.write("hidden replacement bytes\n")
        subprocess.run(["git", "-C", str(self.workspace), "add", "hidden.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-q", "-m", "replacement"], check=True
        )
        replacement = subprocess.run(
            ["git", "-C", str(self.workspace), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(self.workspace), "reset", "--hard", original], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "replace", original, replacement], check=True)
        with self.assertRaisesRegex(RuntimeError, "replacement refs"):
            controller._execution_git_state(str(self.workspace))
        subprocess.run(["git", "-C", str(self.workspace), "replace", "-d", original], check=True)

        git_dir = Path(
            subprocess.run(
                ["git", "-C", str(self.workspace), "rev-parse", "--git-dir"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        if not git_dir.is_absolute():
            git_dir = self.workspace / git_dir
        graft = git_dir / "info" / "grafts"
        graft.write_text(f"{original} {replacement}\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "grafts"):
            controller._execution_git_state(str(self.workspace))
        graft.unlink()

        for flag, clear in (
            ("--assume-unchanged", "--no-assume-unchanged"),
            ("--skip-worktree", "--no-skip-worktree"),
        ):
            with self.subTest(flag=flag):
                subprocess.run(
                    ["git", "-C", str(self.workspace), "update-index", flag, "app.py"], check=True
                )
                with self.assertRaisesRegex(RuntimeError, "index flags"):
                    controller._execution_git_state(str(self.workspace))
                subprocess.run(
                    ["git", "-C", str(self.workspace), "update-index", clear, "app.py"], check=True
                )

    def test_snapshot_and_manifest_do_not_execute_repository_helpers(self):
        marker = Path(self.temp.name) / "repository-helper-ran"
        helper = Path(self.temp.name) / "repository-helper.sh"
        helper.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
        helper.chmod(0o755)
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "core.fsmonitor", str(helper)], check=True
        )
        controller._execution_git_state(str(self.workspace))
        self.assertFalse(marker.exists())
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "--unset", "core.fsmonitor"], check=True
        )

        (self.workspace / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", ".gitattributes"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-q", "-m", "attributes"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "filter.evil.clean", str(helper)], check=True
        )
        (self.workspace / "safe.txt").write_text("safe bytes\n", encoding="utf-8")
        manifest = controller._publication_manifest(str(self.workspace), ["safe.txt"])
        self.assertRegex(manifest["safe.txt"] or "", r"^100644:[0-9a-f]{40,64}$")
        self.assertFalse(marker.exists())

    def test_sensitive_path_filter_covers_common_credential_locations(self):
        for path in (
            ".env", "config/.env.production", ".npmrc", ".pypirc", ".netrc",
            ".git-credentials", ".ssh/id_ed25519", ".aws/credentials",
            ".docker/config.json", "composer/auth.json", "auth.json",
            "config/auth.json", ".composer/auth.json", ".bundle/config", ".kube/config",
            ".terraform.d/credentials.tfrc.json", ".vault-token",
            "deploy/service-account.json", "certs/client.pem", "keys/app.keystore",
            "production.tfvars", "secrets.auto.tfvars.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(controller._sensitive_staged_path(path))
        for path in (".env.example", ".env.sample", "docs/credentials.md", "app.py"):
            with self.subTest(path=path):
                self.assertFalse(controller._sensitive_staged_path(path))

    def test_repair_limit_pauses_for_review(self):
        c = self.create_campaign(max_repairs=0)
        c = self.complete(c, self.proposal())
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        c = self.complete(
            c,
            {
                "schema": controller.SCHEMA,
                "role": "validate",
                "verdict": "fail",
                "build_passed": False,
                "tests_passed": False,
                "critical_issues": 1,
                "high_issues": 0,
                "regressions": 0,
                "findings": ["repair is required"],
                "correction_prompt": "repair",
            },
        )
        self.assertEqual(c["state"], "needs_review")
        self.assertIn("repair limit", c["message"].lower())

    def test_malformed_examination_pauses_instead_of_looping(self):
        c = self.create_campaign()
        c = self.complete(c, None, summary="I think it looks good")
        self.assertEqual(c["state"], "needs_review")
        self.assertIn("missing namespaced", c["message"])

    def test_actionable_examination_summary_without_metadata_fails_closed(self):
        c = self.create_campaign()
        summary = (
            "Examined the codebase and found one high-value improvement: replace the unsafe "
            "force unwrap in RenderJobProjectSync.swift with safe optional binding, add focused "
            "regression tests for timeline-present and timeline-absent behavior, and run all gates."
        )
        c = self.complete(c, None, summary=summary)
        self.assertEqual(c["state"], "needs_review")
        self.assertEqual(c["stage"], "examine")
        self.assertIsNone(c["proposal_task_id"])

    def test_examination_summary_with_fixing_without_metadata_fails_closed(self):
        c = self.create_campaign()
        summary = (
            "Round 2 examination complete. The codebase is functional with a solid foundation. "
            "Identified one high-priority bug: duplicate status badge rendering in the vehicle "
            "detail page. Recommend fixing the duplicate badge as the next implementation task."
        )
        c = self.complete(c, None, summary=summary)
        self.assertEqual(c["state"], "needs_review")
        self.assertEqual(c["stage"], "examine")
        self.assertIsNone(c["proposal_task_id"])

    def test_passing_validation_summary_without_metadata_fails_closed(self):
        c = self.create_campaign()
        c = self.complete(c, self.proposal())
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        summary = (
            "Validation passed: swift build succeeds without warnings; all tests pass with "
            "0 failures; no critical/high issues and no regressions were found."
        )
        c = self.complete(c, None, summary=summary)
        self.assertEqual(c["state"], "needs_review")
        self.assertEqual(c["stage"], "validate")
        self.assertEqual(c["round_no"], 1)

    def test_passing_validator_comment_without_run_metadata_fails_closed(self):
        c = self.create_campaign()
        c = self.complete(c, self.proposal())
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        conn = kbc.connect(board=c["board"])
        try:
            kb.add_comment(
                conn,
                c["active_task_id"],
                c["assignee"],
                "Verdict: PASS. Build passes. Tests pass. Acceptance criteria met. "
                "0 errors. 0 critical issues. 0 high issues. 0 regressions.",
            )
            ok = kb.complete_task(
                conn,
                c["active_task_id"],
                summary="Validation complete.",
                metadata={},
                fire_lifecycle_hook=False,
            )
            self.assertTrue(ok)
        finally:
            conn.close()
        c = controller.reconcile_campaign(c["id"])
        self.assertEqual(c["state"], "needs_review")
        self.assertEqual(c["stage"], "validate")
        self.assertEqual(c["round_no"], 1)

    def test_ranked_examination_summary_without_metadata_fails_closed(self):
        c = self.create_campaign(target_average=9.0)
        summary = (
            "Round 3 examination complete. Five-category scoring: correctness_reliability 8.5, "
            "security_safety 7.5, architecture_maintainability 8.0, test_quality 4.0, "
            "user_experience_performance 8.5. Average 7.3/10 — below target 9.0. "
            "Verdict: proposal. Single high-value improvement: add comprehensive test coverage "
            "for hooks, helpers, and critical UI components."
        )
        c = self.complete(c, None, summary=summary)
        self.assertEqual(c["state"], "needs_review")
        self.assertEqual(c["stage"], "examine")
        self.assertIsNone(c["last_average"])

    def test_explicit_validation_pass_without_metadata_fails_closed(self):
        c = self.create_campaign()
        c = self.complete(c, self.proposal())
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        summary = (
            "Validation PASS: Build succeeds, lint has only warnings (no errors), duplicate status "
            "badge removed from src/app/vehicle/[slug]/page.tsx. Acceptance criteria met."
        )
        c = self.complete(c, None, summary=summary)
        self.assertEqual(c["state"], "needs_review")
        self.assertEqual(c["stage"], "validate")
        self.assertEqual(c["round_no"], 1)

    def test_campaign_database_is_shared_across_named_profiles(self):
        root = Path(self.temp.name) / "hermes-root"
        profile_home = root / "profiles" / "ollama"
        profile_home.mkdir(parents=True)
        os.environ["HERMES_HOME"] = str(profile_home)

        conn = controller._conn()
        conn.close()

        self.assertTrue((root / "plugin-data" / "quality-loop" / "data.db").is_file())
        self.assertFalse((profile_home / "plugin-data" / "quality-loop" / "data.db").exists())

    def test_unrelated_worker_metadata_cannot_enable_summary_fallback(self):
        summary = (
            "Found one high-value improvement: replace the unsafe force unwrap with safe "
            "optional binding and add focused regression tests for both code paths."
        )
        run = SimpleNamespace(metadata={"worker_session_id": "session-1"}, summary=summary)
        payload = controller._handoff(run, expected_role="examine")
        self.assertIsNone(payload)

    def test_quality_loop_json_prefix_is_parsed_from_summary(self):
        payload = self.proposal()
        summary = "Worker summary\nQUALITY_LOOP_JSON: " + json.dumps({"quality_loop": payload})
        run = SimpleNamespace(metadata={}, summary=summary)
        self.assertIsNone(controller._handoff(run, expected_role="examine"))
        self.assertEqual(
            controller._handoff(
                run, expected_role="examine", allow_pre_hardening_migration=True
            ),
            payload,
        )

    def test_task_body_requires_metadata_argument(self):
        c = self.create_campaign()
        for stage in ("examine", "scope_validate", "plan", "execute", "validate", "final_validate"):
            body = controller._task_body(c, stage)
            self.assertIn("typed top-level `quality_loop` argument", body, stage)
            self.assertIn("QUALITY_LOOP_JSON:", body, stage)
            self.assertIn("Do not repeat a failing completion call unchanged", body, stage)

    def test_validation_task_body_requires_recoverable_summary_fallback(self):
        c = self.create_campaign()
        body = controller._task_body(c, "validate")
        self.assertIn("acceptance criteria met", body)
        self.assertIn("0 critical issues", body)
        self.assertIn("0 high issues", body)
        self.assertIn("0 regressions", body)
        self.assertIn("correction prompt", body)

    def test_repeated_reconcile_does_not_duplicate_active_card(self):
        c = self.create_campaign()
        conn = kbc.connect(board="default")
        try:
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(before, 1)
        for _ in range(3):
            c = controller.reconcile_campaign(c["id"])
        conn = kbc.connect(board="default")
        try:
            after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(after, 1)

    def test_task_idempotency_distinguishes_parent_lineage(self):
        c = self.create_campaign()
        first_parent = c["active_task_id"]
        conn = kbc.connect(board="default")
        try:
            second_parent = kb.create_task(
                conn,
                title="alternate parent",
                body="alternate lineage",
                assignee=c["assignee"],
                workspace_kind="dir",
                workspace_path=str(self.workspace),
                board="default",
            )
        finally:
            conn.close()

        improvement = self.scoped_item(
            "Focused improvement",
            "Implement one bounded change.",
            "focused test passes",
        )
        first = controller._create_task(c, "execute", [first_parent], improvement=improvement)
        repeated = controller._create_task(c, "execute", [first_parent], improvement=improvement)
        alternate = controller._create_task(c, "execute", [second_parent], improvement=improvement)

        self.assertEqual(repeated, first)
        self.assertNotEqual(alternate, first)

    def test_manual_stop_prevents_new_cards(self):
        c = self.create_campaign()
        active_task = c["active_task_id"]
        c = controller.set_campaign_state(c["id"], "stop")
        self.assertEqual(c["state"], "stopped")
        after = controller.reconcile_campaign(c["id"])
        self.assertEqual(after["state"], "stopped")
        self.assertEqual(after["active_task_id"], active_task)

    def test_failed_hard_gate_overrides_validator_pass(self):
        c = self.create_campaign(build_command="true", test_command="false")
        c = self.complete(c, self.proposal(commands=["true", "false"]))
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        c = self.complete(c, self.passing_validation())
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["stage"], "execute")
        self.assertEqual(c["repair_no"], 1)
        self.assertFalse(c["last_gate_result"]["ok"])
        self.assertEqual(c["last_gate_result"]["commands"][-1]["exit_code"], 1)
        board = kbc.connect(board="default")
        try:
            repair = kb.get_task(board, c["active_task_id"])
        finally:
            board.close()
        assert repair is not None
        repair_body = repair.body or ""
        self.assertIn("REPAIR SCOPE FROM FAILED VALIDATION", repair_body)
        self.assertIn("Command: false", repair_body)
        self.assertIn("exit: 1", repair_body)

    def test_campaign_process_lock_is_non_blocking(self):
        with controller._campaign_process_lock("ql_lock_test") as first:
            self.assertTrue(first)
            with controller._campaign_process_lock("ql_lock_test") as second:
                self.assertFalse(second)

    def test_campaign_process_lock_hashes_valid_campaign_id_before_path_construction(self):
        campaign_id = "ql_sensitive-but-valid"
        with controller._campaign_process_lock(campaign_id) as acquired:
            self.assertTrue(acquired)
            lock_dir = (
                Path(self.temp.name) / "plugin-data" / controller.PLUGIN_ID / "locks"
            )
            names = [path.name for path in lock_dir.iterdir()]
            self.assertEqual(
                names,
                [hashlib.sha256(campaign_id.encode("utf-8")).hexdigest() + ".lock"],
            )
            self.assertNotIn(campaign_id, names[0])

    def test_campaign_process_lock_rejects_unsafe_campaign_ids(self):
        for campaign_id in (
            "../escape",
            "ql/escape",
            "ql\\escape",
            "/absolute",
            ".",
            "..",
            "ql\ncontrol",
            "ql\x85control",
        ):
            with self.subTest(campaign_id=repr(campaign_id)):
                with self.assertRaisesRegex(ValueError, "campaign id"):
                    with controller._campaign_process_lock(campaign_id):
                        self.fail("unsafe campaign id acquired a process lock")

    def test_campaign_process_lock_rejects_symlinked_lock_directory(self):
        lock_parent = Path(self.temp.name) / "plugin-data" / controller.PLUGIN_ID
        lock_parent.mkdir(parents=True, exist_ok=True)
        outside = Path(self.temp.name) / "outside-locks"
        outside.mkdir()
        (lock_parent / "locks").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(RuntimeError, "symlink|escape"):
            with controller._campaign_process_lock("ql_symlink"):
                self.fail("symlinked lock directory was trusted")
        self.assertEqual(list(outside.iterdir()), [])

    def test_execution_snapshot_detects_ignored_and_repository_control_mutations(self):
        (self.workspace / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.workspace), "add", ".gitignore"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-q", "-m", "ignore fixture"],
            check=True,
        )
        ignored = self.workspace / "ignored.txt"
        ignored.write_text("baseline ignored\n", encoding="utf-8")
        hook = self.workspace / ".git" / "hooks" / "quality-loop-test"
        hook.write_text("baseline hook\n", encoding="utf-8")

        baseline = controller._execution_git_state(str(self.workspace), ["app.py"])
        self.assertEqual(
            set(baseline), {"version", "head", "index", "control", "ignored", "files"}
        )

        mutations = {
            "ignored": lambda: ignored.write_text("mutated ignored\n", encoding="utf-8"),
            "config": lambda: subprocess.run(
                ["git", "-C", str(self.workspace), "config", "quality.loop", "mutated"],
                check=True,
            ),
            "hook": lambda: hook.write_text("mutated hook\n", encoding="utf-8"),
            "index": lambda: subprocess.run(
                ["git", "-C", str(self.workspace), "update-index", "--assume-unchanged", "app.py"],
                check=True,
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                subprocess.run(
                    ["git", "-C", str(self.workspace), "reset", "--hard", "-q", "HEAD"],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(self.workspace), "config", "--unset-all", "quality.loop"],
                    check=False,
                )
                ignored.write_text("baseline ignored\n", encoding="utf-8")
                hook.write_text("baseline hook\n", encoding="utf-8")
                subprocess.run(
                    ["git", "-C", str(self.workspace), "update-index", "--no-assume-unchanged", "app.py"],
                    check=True,
                )
                baseline = controller._execution_git_state(str(self.workspace), ["app.py"])
                mutate()
                if name == "index":
                    with self.assertRaisesRegex(RuntimeError, "index flags"):
                        controller._execution_git_state(str(self.workspace), ["app.py"])
                    continue
                current = controller._execution_git_state(str(self.workspace), ["app.py"])
                self.assertNotEqual(current, baseline)
                self.assertTrue(
                    any(current[field] != baseline[field] for field in ("index", "control", "ignored")),
                    name,
                )

    def test_execute_state_rejects_ignored_and_repository_control_delta(self):
        (self.workspace / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-q", "-m", "ignore fixture"],
            check=True,
        )
        campaign = self.create_campaign()
        campaign = self.complete(campaign, self.proposal(), auto_scope=True)
        self.assertEqual(campaign["stage"], "execute", campaign.get("message"))
        conn = kbc.connect(board=campaign["board"])
        try:
            task = kb.get_task(conn, campaign["active_task_id"])
        finally:
            conn.close()
        assert task is not None
        contract = controller._execution_contract_from_body(task.body or "")
        self.assertIsNotNone(contract)
        (self.workspace / "ignored.txt").write_text("executor artifact\n", encoding="utf-8")
        payload = {
            "schema": controller.SCHEMA,
            "role": "execute",
            "changed_files": [],
            "verification": [
                {"command": command, "exit_code": 0} for command in contract["commands"]
            ],
            "residual_risk": [],
        }
        self.assertRegex(
            controller._execute_state_error(campaign, task, payload) or "",
            "ignored|repository-control",
        )

    def test_every_read_only_stage_embeds_and_enforces_full_workspace_snapshot(self):
        campaign = self.create_campaign()
        improvement = self.scoped_item(
            "Read-only snapshot", "Validate without mutation.", "workspace stays unchanged"
        )
        for stage in (
            "examine", "scope_validate", "validate", "integrate_validate", "final_validate"
        ):
            with self.subTest(stage=stage):
                task_id = controller._create_task(
                    campaign,
                    stage,
                    [],
                    improvement=improvement if stage != "examine" else None,
                    integration_validation=stage == "integrate_validate",
                )
                conn = kbc.connect(board=campaign["board"])
                try:
                    task = kb.get_task(conn, task_id)
                finally:
                    conn.close()
                assert task is not None
                self.assertTrue(
                    controller._read_only_snapshot_unchanged(campaign, task.body or "")
                )
                with (self.workspace / "app.py").open("a", encoding="utf-8") as handle:
                    handle.write(f"validator mutation for {stage}\n")
                self.assertFalse(
                    controller._read_only_snapshot_unchanged(campaign, task.body or "")
                )
                subprocess.run(
                    ["git", "-C", str(self.workspace), "checkout", "--", "app.py"], check=True
                )

    def test_examine_mutation_fails_closed_without_creating_child(self):
        campaign = self.create_campaign()
        with (self.workspace / "app.py").open("a", encoding="utf-8") as handle:
            handle.write("examiner illegally mutated the workspace\n")
        result = self.complete(campaign, self.proposal(), auto_scope=False)
        self.assertEqual(result["state"], "needs_review")
        self.assertIn("read-only", result["message"].lower())
        conn = kbc.connect(board=campaign["board"])
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        finally:
            conn.close()

    def test_reconcile_rejects_body_not_matching_creation_provenance(self):
        campaign = self.create_campaign()
        original_task_id = campaign["active_task_id"]
        conn = kbc.connect(board=campaign["board"])
        try:
            task = kb.get_task(conn, original_task_id)
            if task is None:
                self.fail("campaign task is missing")
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET body = ? WHERE id = ?",
                    (str(task.body or "") + "\nforged contract\n", original_task_id),
                )
            self.assertTrue(
                kb.complete_task(
                    conn,
                    original_task_id,
                    summary="forged proposal",
                    metadata={"quality_loop": self.proposal()},
                    fire_lifecycle_hook=False,
                )
            )
        finally:
            conn.close()

        result = controller.reconcile_campaign(campaign["id"])
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result["stage"], "examine")
        self.assertEqual(result["active_task_id"], original_task_id)
        self.assertIn("immutable controller creation provenance", result["message"])

    def test_validator_and_hard_gates_must_leave_workspace_read_only(self):
        campaign = self.create_campaign(test_command="printf gate-mutation >> app.py")
        campaign = self.complete(
            campaign,
            self.proposal(commands=["true", "printf gate-mutation >> app.py"]),
        )
        campaign = self.complete(campaign, {"schema": controller.SCHEMA, "role": "execute"})
        self.assertEqual(campaign["stage"], "validate")
        result = self.complete(campaign, self.passing_validation())
        self.assertEqual(result["state"], "needs_review")
        self.assertIn("hard gate", result["message"].lower())
        self.assertIn("read-only", result["message"].lower())

    def test_publish_success_rejects_state_not_matching_authenticated_executor_snapshot(self):
        campaign = self.create_campaign(publish_on_success=True, target_average=9.0)
        initial = controller._execution_git_state(str(self.workspace))
        with (self.workspace / "app.py").open("a", encoding="utf-8") as handle:
            handle.write("authenticated executor change\n")
        authenticated = controller._execution_git_state(str(self.workspace))
        campaign["initial_snapshot"] = initial
        campaign["authenticated_snapshot"] = authenticated
        with (self.workspace / "server.js").open("a", encoding="utf-8") as handle:
            handle.write("unauthenticated validator change\n")

        with mock.patch.object(controller, "_git_command", wraps=controller._git_command) as git:
            result = controller._publish_success(campaign)

        self.assertFalse(result["ok"])
        self.assertIn("authenticated executor", result["error"])
        self.assertFalse(any(call.args[1] == "push" for call in git.call_args_list))

    def test_executor_authentication_rejects_second_snapshot_race(self):
        campaign = self.create_campaign()
        campaign = self.complete(campaign, self.proposal(), auto_scope=True)
        self.assertEqual(campaign["stage"], "execute", campaign.get("message"))
        conn = kbc.connect(board=campaign["board"])
        try:
            task = kb.get_task(conn, campaign["active_task_id"])
        finally:
            conn.close()
        assert task is not None
        contract = controller._execution_contract_from_body(task.body or "")
        self.assertIsNotNone(contract)
        changed_file = contract["allowed_files"][0]
        with (self.workspace / changed_file).open("a", encoding="utf-8") as handle:
            handle.write("authenticated executor change\n")
        payload = {
            "schema": controller.SCHEMA,
            "role": "execute",
            "changed_files": [changed_file],
            "verification": [
                {"command": command, "exit_code": 0} for command in contract["commands"]
            ],
            "residual_risk": [],
        }
        before_authenticated = campaign["authenticated_snapshot"]
        self._complete_active_without_reconcile(campaign, payload)

        real_snapshot = controller._execution_git_state
        calls = 0

        def racing_snapshot(*args, **kwargs):
            nonlocal calls
            calls += 1
            snapshot = real_snapshot(*args, **kwargs)
            if calls == 2:
                (self.workspace / "out-of-scope.txt").write_text(
                    "raced after validation\n", encoding="utf-8"
                )
            return snapshot

        with mock.patch.object(
            controller, "_execution_git_state", side_effect=racing_snapshot
        ):
            result = controller.reconcile_campaign(campaign["id"])

        self.assertEqual(result["state"], "needs_review")
        self.assertIn("snapshots disagree", result["message"])
        self.assertEqual(result["authenticated_snapshot"], before_authenticated)

    def test_executor_authentication_rejects_reported_new_file_disappearing(self):
        scoped = self.scoped_item(
            "Tested application change",
            "Implement only the tested application change.",
            "configured gates pass",
            relevant_file="new-output.txt",
        )
        campaign = self.create_campaign()
        campaign = self.complete(
            campaign, self.proposal(item=scoped), auto_scope=True
        )
        self.assertEqual(campaign["stage"], "execute", campaign.get("message"))
        conn = kbc.connect(board=campaign["board"])
        try:
            task = kb.get_task(conn, campaign["active_task_id"])
        finally:
            conn.close()
        assert task is not None
        contract = controller._execution_contract_from_body(task.body or "")
        self.assertIsNotNone(contract)
        added = self.workspace / "new-output.txt"
        added.write_text("authenticated executor output\n", encoding="utf-8")
        payload = {
            "schema": controller.SCHEMA,
            "role": "execute",
            "changed_files": ["new-output.txt"],
            "verification": [
                {"command": command, "exit_code": 0} for command in contract["commands"]
            ],
            "residual_risk": [],
        }
        self._complete_active_without_reconcile(campaign, payload)

        real_snapshot = controller._execution_git_state
        calls = 0

        def disappearing_snapshot(*args, **kwargs):
            nonlocal calls
            calls += 1
            snapshot = real_snapshot(*args, **kwargs)
            if calls == 2:
                added.unlink()
            return snapshot

        with mock.patch.object(
            controller, "_execution_git_state", side_effect=disappearing_snapshot
        ):
            result = controller.reconcile_campaign(campaign["id"])

        self.assertEqual(result["state"], "needs_review")
        self.assertIn("snapshots disagree", result["message"])

    def test_real_completion_boundary_advances_scope_execute_validate_lineage(self):
        from tools import kanban_tools as kt

        campaign = self.create_campaign()

        def complete_through_tool(current, payload):
            task_id = current["active_task_id"]
            conn = kbc.connect(board=current["board"])
            try:
                self.assertTrue(kb.claim_task(conn, task_id))
                claimed = kb.get_task(conn, task_id)
                self.assertIsNotNone(claimed)
                assert claimed is not None
                run_id = claimed.current_run_id
            finally:
                conn.close()
            os.environ["HERMES_KANBAN_TASK"] = task_id
            os.environ["HERMES_KANBAN_RUN_ID"] = str(run_id)
            outcome = json.loads(
                kt._handle_complete(
                    {"summary": f"Completed {payload['role']}.", "quality_loop": payload}
                )
            )
            self.assertTrue(outcome.get("ok"), outcome)
            return controller.reconcile_campaign(current["id"])

        campaign = complete_through_tool(campaign, self.proposal())
        self.assertEqual(campaign["stage"], "scope_validate")
        scoped = self.scoped_item(
            "Tested application change",
            "Implement only the tested application change.",
            "configured gates pass",
        )
        campaign = complete_through_tool(
            campaign,
            {
                "schema": controller.SCHEMA,
                "role": "scope_validate",
                "verdict": "pass",
                "scoped_improvement": scoped,
                "findings": [],
            },
        )
        self.assertEqual(campaign["stage"], "plan")
        campaign = complete_through_tool(
            campaign,
            {
                "schema": controller.SCHEMA,
                "role": "plan",
                "decomposition_required": False,
                "rationale": "The scoped item fits one bounded execution slice.",
            },
        )
        self.assertEqual(campaign["stage"], "execute")
        conn = kbc.connect(board=campaign["board"])
        try:
            execute_task = kb.get_task(conn, campaign["active_task_id"])
        finally:
            conn.close()
        assert execute_task is not None
        contract = controller._execution_contract_from_body(execute_task.body or "")
        self.assertIsNotNone(contract)
        changed_file = contract["allowed_files"][0]
        with (self.workspace / changed_file).open("a", encoding="utf-8") as handle:
            handle.write("real completion boundary change\n")
        campaign = complete_through_tool(
            campaign,
            {
                "schema": controller.SCHEMA,
                "role": "execute",
                "changed_files": [changed_file],
                "verification": [
                    {"command": command, "exit_code": 0}
                    for command in contract["commands"]
                ],
                "residual_risk": [],
            },
        )
        self.assertEqual(campaign["stage"], "validate")
        conn = kbc.connect(board=campaign["board"])
        try:
            validator = kb.get_task(conn, campaign["active_task_id"])
            self.assertIsNotNone(validator)
            assert validator is not None
            self.assertEqual(validator.status, "ready")
            self.assertEqual(kb.parent_ids(conn, validator.id), [execute_task.id])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

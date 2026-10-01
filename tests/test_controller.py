from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
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
        self.old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self.temp.name
        self.workspace = Path(self.temp.name) / "repo"
        self.workspace.mkdir()
        (self.workspace / ".git").mkdir()

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.old_home
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

    def complete(self, campaign, payload=None, summary="done"):
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
        return controller.reconcile_campaign(campaign["id"])

    def proposal(self, *, scores=None):
        payload = {
            "schema": controller.SCHEMA,
            "role": "examine",
            "verdict": "proposal",
            "implementation_prompt": "Implement the tested change.",
            "acceptance_criteria": ["tests pass"],
        }
        if scores is not None:
            if isinstance(scores, (int, float)):
                scores = {name: float(scores) for name in controller.RANKING_CATEGORIES}
            payload["score_breakdown"] = scores
            payload["score_rationale"] = "Deterministic test scores"
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
        self.assertEqual(c["stage"], "execute")
        self.assertEqual(c["repair_no"], 1)

        board = kbc.connect(board="default")
        try:
            parents = board.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (c["active_task_id"],)
            ).fetchall()
            self.assertEqual([row["parent_id"] for row in parents], [failed_validator_id])
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
                "reason": "No important work remains",
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
        self.assertIn('"improvement_items"', examine_body)
        self.assertIn("highest-priority item", examine_body)

        proposal = self.proposal(scores=7.5)
        proposal["implementation_prompt"] = "Broad legacy prompt that must not reach execution."
        proposal["improvement_items"] = [
            {
                "priority": 2,
                "title": "Comparison behavior",
                "implementation_prompt": "Implement comparison behavior second.",
                "acceptance_criteria": ["comparison test passes"],
                "relevant_files": ["src/components/comparison-button.tsx"],
            },
            {
                "priority": 1,
                "title": "Favorite button click",
                "implementation_prompt": "Test only the real FavoriteButton click behavior.",
                "acceptance_criteria": ["focused favorite test passes"],
                "relevant_files": ["src/components/favorite-button.tsx"],
            },
        ]

        c = self.complete(c, proposal)
        self.assertEqual(c["stage"], "execute")
        board = kbc.connect(board="default")
        try:
            task = kb.get_task(board, c["active_task_id"])
        finally:
            board.close()
        self.assertIsNotNone(task)
        body = task.body
        self.assertIn("Favorite button click", body)
        self.assertIn("Test only the real FavoriteButton click behavior.", body)
        self.assertIn("focused favorite test passes", body)
        self.assertIn("src/components/favorite-button.tsx", body)
        self.assertNotIn("Implement comparison behavior second.", body)
        self.assertNotIn("Broad legacy prompt that must not reach execution.", body)
        self.assertIn("Do not implement any other findings", body)

    def test_validator_receives_only_selected_item_and_read_only_contract(self):
        c = self.create_campaign(target_average=9.0)
        proposal = self.proposal(scores=7.5)
        proposal["improvement_items"] = [
            {
                "priority": 1,
                "title": "Favorite button click",
                "implementation_prompt": "Test only the real FavoriteButton click behavior.",
                "acceptance_criteria": ["focused favorite test passes"],
                "relevant_files": ["src/components/favorite-button.tsx"],
            },
            {
                "priority": 2,
                "title": "Unselected comparison work",
                "implementation_prompt": "Implement comparison behavior later.",
                "acceptance_criteria": ["comparison test passes"],
                "relevant_files": ["src/components/comparison-button.tsx"],
            },
        ]

        c = self.complete(c, proposal)
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
        self.assertNotIn("Implement comparison behavior later.", body)

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
        c = self.complete(c, self.proposal())
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
                "reason": "Looks good",
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
        self.assertIn("committed and pushed", c["message"])

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

        (repo / "improvement.txt").write_text("verified improvement\n")
        artifact = repo / ".quality-loop" / "worker.log"
        artifact.parent.mkdir()
        artifact.write_text("runtime-only\n")
        result = controller._publish_success(
            {
                "workspace": str(repo),
                "publish_remote": "origin",
                "publish_branch": "rank-loop",
                "commit_message": "quality-loop: verified target average",
            }
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["committed"])
        self.assertTrue(result["pushed"])
        remote_head = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/rank-loop"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(remote_head, result["commit"])
        tree = subprocess.run(
            ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", remote_head],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertIn("improvement.txt", tree)
        self.assertFalse(any(path.startswith(".quality-loop/") for path in tree))

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
        (repo / ".env").write_text("EXAMPLE_NOT_A_REAL_SECRET=value\n")

        result = controller._publish_success(
            {
                "workspace": str(repo),
                "publish_remote": "origin",
                "publish_branch": "rank-loop",
                "commit_message": "must not publish",
            }
        )
        self.assertFalse(result["ok"])
        self.assertIn("sensitive", result["error"])

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
                "correction_prompt": "repair",
            },
        )
        self.assertEqual(c["state"], "needs_review")
        self.assertIn("repair limit", c["message"].lower())

    def test_malformed_examination_pauses_instead_of_looping(self):
        c = self.create_campaign()
        c = self.complete(c, None, summary="I think it looks good")
        self.assertEqual(c["state"], "needs_review")
        self.assertIn("valid proposal", c["message"])

    def test_actionable_examination_summary_recovers_missing_metadata(self):
        c = self.create_campaign()
        summary = (
            "Examined the codebase and found one high-value improvement: replace the unsafe "
            "force unwrap in RenderJobProjectSync.swift with safe optional binding, add focused "
            "regression tests for timeline-present and timeline-absent behavior, and run all gates."
        )
        c = self.complete(c, None, summary=summary)
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["stage"], "execute")
        self.assertEqual(c["proposal_task_id"] is not None, True)

    def test_examination_summary_with_fixing_recovers_missing_metadata(self):
        c = self.create_campaign()
        summary = (
            "Round 2 examination complete. The codebase is functional with a solid foundation. "
            "Identified one high-priority bug: duplicate status badge rendering in the vehicle "
            "detail page. Recommend fixing the duplicate badge as the next implementation task."
        )
        c = self.complete(c, None, summary=summary)
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["stage"], "execute")
        self.assertIsNotNone(c["proposal_task_id"])

    def test_passing_validation_summary_recovers_missing_metadata(self):
        c = self.create_campaign()
        c = self.complete(c, self.proposal())
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        summary = (
            "Validation passed: swift build succeeds without warnings; all tests pass with "
            "0 failures; no critical/high issues and no regressions were found."
        )
        c = self.complete(c, None, summary=summary)
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["stage"], "examine")
        self.assertEqual(c["round_no"], 2)
        self.assertTrue(c["last_gate_result"]["ok"])

    def test_passing_validator_comment_recovers_missing_run_metadata(self):
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
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["stage"], "examine")
        self.assertEqual(c["round_no"], 2)
        self.assertTrue(c["last_gate_result"]["ok"])

    def test_ranked_examination_summary_recovers_complete_score_breakdown(self):
        c = self.create_campaign(target_average=9.0)
        summary = (
            "Round 3 examination complete. Five-category scoring: correctness_reliability 8.5, "
            "security_safety 7.5, architecture_maintainability 8.0, test_quality 4.0, "
            "user_experience_performance 8.5. Average 7.3/10 — below target 9.0. "
            "Verdict: proposal. Single high-value improvement: add comprehensive test coverage "
            "for hooks, helpers, and critical UI components."
        )
        c = self.complete(c, None, summary=summary)
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["stage"], "execute")
        self.assertEqual(c["last_average"], 7.3)
        self.assertEqual(set(c["last_ranking"]), set(controller.RANKING_CATEGORIES))

    def test_explicit_validation_pass_with_acceptance_criteria_recovers_missing_metadata(self):
        c = self.create_campaign()
        c = self.complete(c, self.proposal())
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        summary = (
            "Validation PASS: Build succeeds, lint has only warnings (no errors), duplicate status "
            "badge removed from src/app/vehicle/[slug]/page.tsx. Acceptance criteria met."
        )
        c = self.complete(c, None, summary=summary)
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["stage"], "examine")
        self.assertEqual(c["round_no"], 2)
        self.assertTrue(c["last_gate_result"]["ok"])

    def test_campaign_database_is_shared_across_named_profiles(self):
        root = Path(self.temp.name) / "hermes-root"
        profile_home = root / "profiles" / "ollama"
        profile_home.mkdir(parents=True)
        os.environ["HERMES_HOME"] = str(profile_home)

        conn = controller._conn()
        conn.close()

        self.assertTrue((root / "plugin-data" / "quality-loop" / "data.db").is_file())
        self.assertFalse((profile_home / "plugin-data" / "quality-loop" / "data.db").exists())

    def test_unrelated_worker_metadata_does_not_mask_summary_fallback(self):
        summary = (
            "Found one high-value improvement: replace the unsafe force unwrap with safe "
            "optional binding and add focused regression tests for both code paths."
        )
        run = SimpleNamespace(metadata={"worker_session_id": "session-1"}, summary=summary)
        payload = controller._handoff(run, expected_role="examine")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["verdict"], "proposal")
        self.assertTrue(payload["recovered_from_summary"])

    def test_task_body_requires_metadata_argument(self):
        c = self.create_campaign()
        for stage in ("examine", "execute", "validate", "final_validate"):
            body = controller._task_body(c, stage)
            self.assertIn("metadata argument is mandatory", body)
            self.assertIn("QUALITY_LOOP_JSON:", body)
            self.assertIn("Do not repeat a failing empty-metadata call", body)

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
        c = self.complete(c, self.proposal())
        c = self.complete(c, {"schema": controller.SCHEMA, "role": "execute"})
        c = self.complete(c, self.passing_validation())
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["stage"], "execute")
        self.assertEqual(c["repair_no"], 1)
        self.assertFalse(c["last_gate_result"]["ok"])
        self.assertEqual(c["last_gate_result"]["commands"][-1]["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()

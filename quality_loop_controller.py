"""Durable controller for model-overridden Kanban quality loops."""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.sqlite_util import open_db
from hermes_constants import get_default_hermes_root

logger = logging.getLogger(__name__)
PLUGIN_ID = "quality-loop"
SCHEMA = "quality-loop/v1"
RANKING_CATEGORIES = (
    "correctness_reliability",
    "security_safety",
    "architecture_maintainability",
    "test_quality",
    "user_experience_performance",
)
TERMINAL_STATES = {"succeeded", "stopped", "max_rounds", "needs_review"}
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_GIT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_LOCK = threading.RLock()

_CAMPAIGN_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    board TEXT NOT NULL,
    workspace TEXT NOT NULL,
    assignee TEXT NOT NULL,
    examiner_model TEXT NOT NULL,
    executor_model TEXT NOT NULL,
    validator_model TEXT NOT NULL,
    provider_override TEXT,
    build_command TEXT NOT NULL DEFAULT '',
    test_command TEXT NOT NULL DEFAULT '',
    gate_timeout_seconds INTEGER NOT NULL DEFAULT 900,
    last_gate_result TEXT,
    target_average REAL,
    last_average REAL,
    last_ranking TEXT,
    publish_on_success INTEGER NOT NULL DEFAULT 0,
    publish_remote TEXT NOT NULL DEFAULT 'origin',
    publish_branch TEXT,
    commit_message TEXT NOT NULL DEFAULT 'quality-loop: reach target quality average',
    last_publish_result TEXT,
    state TEXT NOT NULL,
    stage TEXT NOT NULL,
    round_no INTEGER NOT NULL DEFAULT 1,
    repair_no INTEGER NOT NULL DEFAULT 0,
    max_rounds INTEGER NOT NULL DEFAULT 20,
    max_repairs INTEGER NOT NULL DEFAULT 3,
    active_task_id TEXT,
    proposal_task_id TEXT,
    selected_improvement TEXT,
    slice_index INTEGER NOT NULL DEFAULT 0,
    slice_count INTEGER NOT NULL DEFAULT 0,
    processed_run_id INTEGER,
    final_mode INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_loop_state ON campaigns(state, board);
"""


def _conn() -> sqlite3.Connection:
    # Campaign state coordinates the dashboard API and the default gateway hook. Those
    # processes can run under different named profiles, so profile-local plugin storage would
    # split one campaign across multiple invisible databases.
    db_path = get_default_hermes_root() / "plugin-data" / PLUGIN_ID / "data.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(
        db_path,
        db_label=f"plugin-data/{PLUGIN_ID}/data.db",
        foreign_keys=True,
        row_factory=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(_CAMPAIGN_SCHEMA)
    # Additive migration for campaigns created by pre-release versions.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    migrations = {
        "build_command": "TEXT NOT NULL DEFAULT ''",
        "test_command": "TEXT NOT NULL DEFAULT ''",
        "gate_timeout_seconds": "INTEGER NOT NULL DEFAULT 900",
        "last_gate_result": "TEXT",
        "target_average": "REAL",
        "last_average": "REAL",
        "last_ranking": "TEXT",
        "publish_on_success": "INTEGER NOT NULL DEFAULT 0",
        "publish_remote": "TEXT NOT NULL DEFAULT 'origin'",
        "publish_branch": "TEXT",
        "commit_message": "TEXT NOT NULL DEFAULT 'quality-loop: reach target quality average'",
        "last_publish_result": "TEXT",
        "selected_improvement": "TEXT",
        "slice_index": "INTEGER NOT NULL DEFAULT 0",
        "slice_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, declaration in migrations.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE campaigns ADD COLUMN {name} {declaration}")
    conn.commit()
    return conn


@contextmanager
def _campaign_process_lock(campaign_id: str) -> Iterator[bool]:
    """Try to own one campaign across profile gateway processes.

    Reconciliation is intentionally non-blocking: another process already running a long hard
    gate or publication step makes this caller leave the campaign for a later tick.
    """
    lock_dir = get_default_hermes_root() / "plugin-data" / PLUGIN_ID / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / f"{campaign_id}.lock").open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                acquired = False
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _row_dict(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    out["final_mode"] = bool(out.get("final_mode"))
    out["publish_on_success"] = bool(out.get("publish_on_success"))
    raw_gates = out.get("last_gate_result")
    if raw_gates:
        try:
            out["last_gate_result"] = json.loads(raw_gates)
        except (TypeError, json.JSONDecodeError):
            out["last_gate_result"] = {"ok": False, "error": "invalid stored gate result"}
    for name in ("last_ranking", "last_publish_result", "selected_improvement"):
        raw = out.get(name)
        if raw:
            try:
                out[name] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                out[name] = None if name == "selected_improvement" else {
                    "ok": False, "error": f"invalid stored {name}"
                }
    return out


def list_campaigns() -> list[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
        return [_decorate(_row_dict(row) or {}) for row in rows]
    finally:
        conn.close()


def get_campaign(campaign_id: str) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return _decorate(_row_dict(row) or {}) if row else None
    finally:
        conn.close()


def _decorate(campaign: dict[str, Any]) -> dict[str, Any]:
    if not campaign:
        return campaign
    task_id = campaign.get("active_task_id")
    if not task_id:
        campaign["active_task"] = None
        return campaign
    try:
        board_conn = kbc.connect(board=campaign["board"])
        try:
            task = kb.get_task(board_conn, task_id)
            run = kb.latest_run(board_conn, task_id) if task else None
            campaign["active_task"] = (
                {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "model": task.model_override,
                    "assignee": task.assignee,
                    "run_id": run.id if run else None,
                    "run_outcome": run.outcome if run else None,
                }
                if task
                else None
            )
        finally:
            board_conn.close()
    except Exception as exc:
        campaign["active_task"] = None
        campaign["active_task_error"] = str(exc)
    return campaign


def _validate_create(data: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(str(data.get("workspace") or "")).expanduser()
    if not workspace.is_absolute():
        raise ValueError("workspace must be an absolute path")
    if not workspace.is_dir():
        raise ValueError(f"workspace directory does not exist: {workspace}")
    board = str(data.get("board") or "default").strip().lower()
    if not _BOARD_RE.fullmatch(board):
        raise ValueError("board must be a valid Hermes Kanban board slug")
    assignee = str(data.get("assignee") or "").strip().lower()
    if not assignee:
        raise ValueError("assignee is required")
    models = {
        "examiner_model": str(data.get("examiner_model") or "").strip(),
        "executor_model": str(data.get("executor_model") or "").strip(),
        "validator_model": str(data.get("validator_model") or "").strip(),
    }
    if not all(models.values()):
        raise ValueError("all three model names are required")
    max_rounds = int(data.get("max_rounds") or 20)
    max_repairs = int(data.get("max_repairs") if data.get("max_repairs") is not None else 3)
    if not 1 <= max_rounds <= 100:
        raise ValueError("max_rounds must be between 1 and 100")
    if not 0 <= max_repairs <= 20:
        raise ValueError("max_repairs must be between 0 and 20")
    build_command = str(data.get("build_command") or "").strip()
    test_command = str(data.get("test_command") or "").strip()
    if not build_command and not test_command:
        raise ValueError("at least one fixed build or test command is required")
    gate_timeout_seconds = int(data.get("gate_timeout_seconds") or 900)
    if not 10 <= gate_timeout_seconds <= 3600:
        raise ValueError("gate_timeout_seconds must be between 10 and 3600")
    raw_target = data.get("target_average")
    target_average = None if raw_target in (None, "") else float(raw_target)
    if target_average is not None and (not math.isfinite(target_average) or not 0 < target_average <= 10):
        raise ValueError("target_average must be greater than 0 and at most 10")
    publish_on_success = bool(data.get("publish_on_success", False))
    publish_remote = str(data.get("publish_remote") or "origin").strip()
    publish_branch = str(data.get("publish_branch") or "").strip() or None
    commit_message = str(
        data.get("commit_message") or "quality-loop: reach target quality average"
    ).strip()
    if not _GIT_NAME_RE.fullmatch(publish_remote):
        raise ValueError("publish_remote must be a valid Git remote name")
    if publish_branch and not _GIT_NAME_RE.fullmatch(publish_branch):
        raise ValueError("publish_branch must be a valid Git branch name")
    if publish_on_success and target_average is None:
        raise ValueError("publish_on_success requires target_average")
    if not commit_message:
        raise ValueError("commit_message is required")
    return {
        "name": str(data.get("name") or workspace.name or "Quality Loop").strip(),
        "board": board,
        "workspace": str(workspace.resolve()),
        "assignee": assignee,
        **models,
        "provider_override": (str(data.get("provider_override") or "").strip() or None),
        "build_command": build_command,
        "test_command": test_command,
        "gate_timeout_seconds": gate_timeout_seconds,
        "target_average": target_average,
        "publish_on_success": publish_on_success,
        "publish_remote": publish_remote,
        "publish_branch": publish_branch,
        "commit_message": commit_message,
        "max_rounds": max_rounds,
        "max_repairs": max_repairs,
    }


def create_campaign(data: dict[str, Any]) -> dict[str, Any]:
    clean = _validate_create(data)
    # Opening the board now catches an invalid/missing board before state is stored.
    board_conn = kbc.connect(board=clean["board"])
    board_conn.close()
    campaign_id = "ql_" + uuid.uuid4().hex[:12]
    now = int(time.time())
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO campaigns (
                id, name, board, workspace, assignee,
                examiner_model, executor_model, validator_model, provider_override,
                build_command, test_command, gate_timeout_seconds,
                target_average, publish_on_success, publish_remote, publish_branch, commit_message,
                state, stage, round_no, repair_no, max_rounds, max_repairs,
                created_at, updated_at, message
            ) VALUES (
                :id, :name, :board, :workspace, :assignee,
                :examiner_model, :executor_model, :validator_model, :provider_override,
                :build_command, :test_command, :gate_timeout_seconds,
                :target_average, :publish_on_success, :publish_remote, :publish_branch, :commit_message,
                'running', 'examine', 1, 0, :max_rounds, :max_repairs,
                :created_at, :updated_at, :message
            )
            """,
            {
                "id": campaign_id,
                **clean,
                "publish_on_success": int(clean["publish_on_success"]),
                "created_at": now,
                "updated_at": now,
                "message": "Creating first examination card",
            },
        )
        conn.commit()
    finally:
        conn.close()
    reconcile_campaign(campaign_id)
    result = get_campaign(campaign_id)
    if result is None:
        raise RuntimeError("campaign disappeared after creation")
    return result


def _update(campaign_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = int(time.time())
    names = list(fields)
    conn = _conn()
    try:
        conn.execute(
            "UPDATE campaigns SET " + ", ".join(f"{name} = ?" for name in names) + " WHERE id = ?",
            [fields[name] for name in names] + [campaign_id],
        )
        conn.commit()
    finally:
        conn.close()


def set_campaign_state(campaign_id: str, action: str) -> dict[str, Any]:
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise KeyError(campaign_id)
    if action == "pause":
        _update(campaign_id, state="paused", message="Paused by user; active worker is not terminated")
    elif action == "resume":
        if campaign["state"] == "succeeded":
            raise ValueError("a succeeded campaign cannot be resumed")
        _update(campaign_id, state="running", message="Resumed; reconciling current card")
        reconcile_campaign(campaign_id)
    elif action == "stop":
        _update(campaign_id, state="stopped", message="Stopped by user; no new cards will be created")
    else:
        raise ValueError(f"unknown action: {action}")
    return get_campaign(campaign_id) or {}


def _task_body(c: dict[str, Any], stage: str) -> str:
    header = f"""QUALITY LOOP CAMPAIGN: {c['id']}
ROUND: {c['round_no']}
ROLE: {stage.upper()}
WORKSPACE: {c['workspace']}

This is an autonomous Kanban stage. Work only inside the assigned workspace.
The final board action MUST be kanban_complete or kanban_block.
For kanban_complete, its metadata argument is mandatory: pass the exact quality_loop object
requested below in metadata as well as writing a concise human-readable summary. Never omit it.
If the tool model cannot populate nested metadata and would send metadata={{}}, put the same complete
outer JSON object on one summary line prefixed exactly `QUALITY_LOOP_JSON: `; the Quality Loop
controller will parse that line while reconciling the completed card.
Do not repeat a failing empty-metadata call.
"""
    if stage == "examine":
        target = c.get("target_average")
        if target is not None:
            categories = ", ".join(f'"{name}": 0.0' for name in RANKING_CATEGORIES)
            last = c.get("last_average")
            last_line = f"\nPREVIOUS COMPUTED AVERAGE: {last:g}/10" if last is not None else ""
            validator = str(c.get("validator_model") or "the configured validation model")
            completion_action = (
                "After that validation passes, the controller will commit and push the configured branch."
                if c.get("publish_on_success")
                else "After that validation passes, the campaign will stop successfully without committing or pushing."
            )
            return header + f"""
Examine and rank the CURRENT application thoroughly. Do not modify source files and do not create
logs, temporary files, or a .quality-loop directory inside the workspace.
TARGET AVERAGE: {float(target):g}/10{last_line}
Score every category independently from 0.0 to 10.0 using concrete repository evidence:
- correctness_reliability: correctness, failure handling, data integrity, concurrency
- security_safety: secrets, unsafe operations, input boundaries, privacy
- architecture_maintainability: design, coupling, clarity, duplication, evolvability
- test_quality: meaningful coverage, regression protection, determinism
- user_experience_performance: observable UX, responsiveness, resource use
The controller computes the simple arithmetic average; do not provide or choose the average yourself.
If the expected average is below {float(target):g}, return verdict "proposal" with 1 to 5
improvement_items ordered by priority. Each item must address exactly one behavior or defect and have
its own acceptance criteria. Prefer one focused execution card. Only when the selected item cannot be
safely completed and verified in one card, add 2 to 5 execution_slices. Every slice must be independently
executable, change one coherent behavior, name its own acceptance criteria, and touch the minimum files.
Order slices by dependency. The controller activates only the highest-priority item, runs exactly one
slice at a time, validates each slice before activating the next, then runs one integrated validation
before a fresh examination. Lower-priority items are findings for reconsideration, not executor scope.
If the expected average is at least {float(target):g}, return verdict "candidate_complete"; the
configured validator ({validator}) will still perform a final independent validation.
{completion_action}
Complete with metadata exactly shaped as:
{{"quality_loop": {{"schema": "{SCHEMA}", "role": "examine", "verdict": "proposal|candidate_complete", "score_breakdown": {{{categories}}}, "score_rationale": "evidence for every category", "improvement_items": [{{"priority": 1, "title": "one improvement", "implementation_prompt": "implement only this item", "acceptance_criteria": ["item-level integrated check"], "relevant_files": ["..."], "risks": ["..."], "execution_slices": [{{"title": "atomic slice", "implementation_prompt": "one coherent change", "acceptance_criteria": ["slice-specific check"], "relevant_files": ["..."]}}]}}]}}}}
All five score_breakdown keys are mandatory and each value must be numeric from 0 through 10.
"""
        return header + f"""
Examine the CURRENT codebase thoroughly. Do not modify source files.
Return 1 to 5 improvement_items ordered by priority. Each item must cover exactly one behavior or
defect. Prefer one focused execution card. Only when an item genuinely requires dependent steps, add
2 to 5 independently executable execution_slices, each with its own prompt, acceptance criteria, and
minimal relevant files. The controller activates only the highest-priority item, validates every slice
before starting the next, runs an integrated validation, and then requests a fresh examination.
If meaningful work remains, complete with metadata exactly shaped as:
{{"quality_loop": {{"schema": "{SCHEMA}", "role": "examine", "verdict": "proposal", "improvement_items": [{{"priority": 1, "title": "one improvement", "implementation_prompt": "implement only this item", "acceptance_criteria": ["integrated check"], "relevant_files": ["..."], "risks": ["..."], "execution_slices": [{{"title": "atomic slice", "implementation_prompt": "one coherent change", "acceptance_criteria": ["slice-specific check"], "relevant_files": ["..."]}}]}}]}}}}
If no critical or high-value work remains, use verdict "candidate_complete" and explain why.
Do not use candidate_complete merely because the repository is large or unfamiliar.
"""
    if stage == "execute":
        return header + f"""
Implement the specification or correction in the parent task result.
Stay within scope, modify the code, add/update tests, and run relevant verification.
Complete with a concise summary and metadata shaped as:
{{"quality_loop": {{"schema": "{SCHEMA}", "role": "execute", "changed_files": ["..."], "verification": [{{"command": "...", "exit_code": 0}}], "residual_risk": ["..."]}}}}
"""
    if stage == "final_validate":
        final_text = "This is the FINAL whole-codebase audit."
    elif stage == "integrate_validate":
        final_text = "This is the INTEGRATED VALIDATION of all validated slices for one selected improvement."
    else:
        final_text = "Validate the proposed implementation."
    gate_lines = "\n".join(
        f"- {name}: `{command}`"
        for name, command in (("build", c.get("build_command")), ("test", c.get("test_command")))
        if command
    )
    return header + f"""
{final_text}
READ-ONLY VALIDATOR: do not modify, create, delete, stage, commit, reset, or restore source files.
Do not call patch/write tools and do not use shell commands that mutate the repository. If the
selected implementation is incomplete or incorrect, return FAIL with a precise correction_prompt;
never implement the correction yourself. Independently inspect the code and git diff, verify only
the acceptance criteria explicitly named in this card, and run the required build/tests.
Lower-priority examiner findings are context only: do not implement them and do not require them
for this verdict. A fresh examination after a PASS will reconsider them.
The controller will independently rerun these fixed hard gates after a claimed PASS:
{gate_lines}
Complete with metadata exactly shaped as:
{{"quality_loop": {{"schema": "{SCHEMA}", "role": "validate", "verdict": "pass|fail", "build_passed": true, "tests_passed": true, "critical_issues": 0, "high_issues": 0, "regressions": 0, "findings": ["..."], "correction_prompt": "required when fail"}}}}
PASS is allowed only when build and tests pass, every parent acceptance criterion is met, and critical/high/regression counts are zero.
As a recovery fallback, your human-readable summary MUST also state all of: Verdict PASS or FAIL;
build passes or fails; tests pass or fail; acceptance criteria met or unmet; 0 critical issues;
0 high issues; and 0 regressions. On FAIL, include a specific correction prompt in the summary.
"""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_slice(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    prompt = str(raw.get("implementation_prompt") or "").strip()
    criteria = _string_list(raw.get("acceptance_criteria"))
    if not title or not prompt or not criteria:
        return None
    return {
        "title": title,
        "implementation_prompt": prompt,
        "acceptance_criteria": criteria,
        "relevant_files": _string_list(raw.get("relevant_files")),
        "risks": _string_list(raw.get("risks")),
    }


def _priority_improvement(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Select and normalize one examiner item, including an optional serial slice plan.

    Legacy handoffs remain accepted as one item so in-flight campaigns can be reconciled after a
    controller upgrade. A present but malformed improvement_items or execution_slices field is never
    widened into the legacy broad prompt.
    """
    if "improvement_items" in payload:
        raw_items = payload.get("improvement_items")
        if not isinstance(raw_items, list) or not raw_items:
            return None
        candidates: list[tuple[float, int, dict[str, Any]]] = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            normalized = _normalize_slice(raw)
            if not normalized:
                continue
            raw_priority = raw.get("priority", index + 1)
            if isinstance(raw_priority, bool):
                continue
            try:
                priority = float(raw_priority)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(priority) or priority < 1:
                continue

            item: dict[str, Any] = {"priority": priority, **normalized}
            if "execution_slices" in raw:
                raw_slices = raw.get("execution_slices")
                if not isinstance(raw_slices, list) or not 2 <= len(raw_slices) <= 5:
                    continue
                slices = [_normalize_slice(value) for value in raw_slices]
                if any(value is None for value in slices):
                    continue
                item["execution_slices"] = [value for value in slices if value is not None]
            candidates.append((priority, index, item))
        return min(candidates, key=lambda value: (value[0], value[1]))[2] if candidates else None

    prompt = str(payload.get("implementation_prompt") or "").strip()
    if not prompt:
        return None
    criteria = _string_list(payload.get("acceptance_criteria"))
    return {
        "priority": 1.0,
        "title": "Legacy examiner proposal",
        "implementation_prompt": prompt,
        "acceptance_criteria": criteria or ["Implement and verify the focused proposal."],
        "relevant_files": _string_list(payload.get("relevant_files")),
        "risks": _string_list(payload.get("risks")),
    }


def _execution_slices(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item.get("execution_slices")
    if isinstance(raw, list) and raw:
        return [value for value in raw if isinstance(value, dict)]
    return [item]


def _slice_item(item: dict[str, Any], index: int) -> Optional[dict[str, Any]]:
    slices = _execution_slices(item)
    if not 0 <= index < len(slices):
        return None
    selected = dict(slices[index])
    selected["slice_index"] = index
    selected["slice_count"] = len(slices)
    selected["parent_title"] = item.get("title")
    return selected


def _execute_item_body(c: dict[str, Any], item: dict[str, Any]) -> str:
    criteria = "\n".join(f"- {value}" for value in item["acceptance_criteria"])
    files = "\n".join(f"- {value}" for value in item.get("relevant_files", [])) or "- Determine the minimum files needed for this item."
    risks = "\n".join(f"- {value}" for value in item.get("risks", [])) or "- None supplied by the examiner."
    slice_count = int(item.get("slice_count") or 1)
    slice_index = int(item.get("slice_index") or 0)
    if item.get("integrated_repair"):
        scope_heading = "INTEGRATED REPAIR"
    elif slice_count > 1:
        scope_heading = f"SEQUENTIAL SLICE {slice_index + 1} OF {slice_count}"
    else:
        scope_heading = "SELECTED ITEM"
    return f"""
{scope_heading}
HIGHEST-PRIORITY ITEM: {item['title']}

Implement only this independently scoped item:
{item['implementation_prompt']}

Acceptance criteria:
{criteria}

Relevant files:
{files}

Known risks:
{risks}

Do not implement any other findings, recommendations, or lower-priority items from the parent
examination. They are context only and will be reconsidered after this item is independently
validated. Do not broaden the task while working.
"""


def _validation_item_body(item: dict[str, Any]) -> str:
    criteria = "\n".join(f"- {value}" for value in item["acceptance_criteria"])
    files = "\n".join(f"- {value}" for value in item.get("relevant_files", [])) or "- Inspect the minimum files needed for this item."
    return f"""
VALIDATION SCOPE: {item['title']}

Validate only this selected item:
{item['implementation_prompt']}

Acceptance criteria:
{criteria}

Relevant files:
{files}

Do not validate, implement, or require any lower-priority item from an examiner handoff. Those
items were deliberately excluded from executor scope and will be reconsidered by a fresh examination.
If this selected item is not complete, return FAIL with a correction_prompt. Do not repair it.
"""


def _integration_validation_body(item: dict[str, Any]) -> str:
    lines: list[str] = []
    for index, value in enumerate(_execution_slices(item), start=1):
        criteria = "; ".join(value.get("acceptance_criteria", []))
        lines.append(f"{index}. {value['title']}: {criteria}")
    item_criteria = "\n".join(f"- {value}" for value in item["acceptance_criteria"])
    return f"""
INTEGRATED VALIDATION: {item['title']}

All component slices already passed their focused validators. Validate that they now work together
as one coherent change and that no cross-slice regression or unintended file change remains.

Validated slices:
{chr(10).join(lines)}

Item-level acceptance criteria:
{item_criteria}

Inspect the complete git diff for this selected improvement and run the configured full hard gates.
Do not reopen lower-priority examiner findings. If integration is incomplete, return FAIL with one
precise correction_prompt; do not repair it.
"""


def _repair_body(correction: str) -> str:
    return f"""
REPAIR SCOPE FROM FAILED VALIDATION:
{correction[:8000]}

Implement this correction only, rerun focused verification, and preserve the original selected
item's scope. The controller will create a fresh independent validation card after this repair.
"""


def _selected_campaign_improvement(c: dict[str, Any]) -> Optional[dict[str, Any]]:
    stored = c.get("selected_improvement")
    if isinstance(stored, dict):
        return stored
    proposal_task_id = c.get("proposal_task_id")
    if not proposal_task_id:
        return None
    conn = kbc.connect(board=c["board"])
    try:
        run = kb.latest_run(conn, proposal_task_id)
    finally:
        conn.close()
    payload = _handoff(run, expected_role="examine")
    return _priority_improvement(payload or {})


def _current_campaign_slice(c: dict[str, Any]) -> Optional[dict[str, Any]]:
    item = _selected_campaign_improvement(c)
    if not item:
        return None
    return _slice_item(item, int(c.get("slice_index") or 0))


def _create_task(
    c: dict[str, Any],
    stage: str,
    parents: list[str],
    improvement: Optional[dict[str, Any]] = None,
    correction: str = "",
    integration_validation: bool = False,
) -> str:
    model_key = {
        "examine": "examiner_model",
        "execute": "executor_model",
        "validate": "validator_model",
        "integrate_validate": "validator_model",
        "final_validate": "validator_model",
    }[stage]
    label = {
        "examine": "Examine current codebase and propose next prompt",
        "execute": "Execute proposed implementation prompt",
        "validate": "Validate implementation",
        "integrate_validate": "Validate integrated improvement",
        "final_validate": "Final whole-codebase validation",
    }[stage]
    if stage == "execute" and improvement and int(improvement.get("slice_count") or 1) > 1:
        slice_label = (
            f"slice {int(improvement.get('slice_index') or 0) + 1}/"
            f"{int(improvement['slice_count'])}"
        )
        suffix = (
            f"{slice_label}, repair {c['repair_no']}"
            if c.get("repair_no", 0)
            else slice_label
        )
    elif stage == "execute" and c.get("repair_no", 0):
        suffix = f"repair {c['repair_no']}"
    else:
        suffix = f"round {c['round_no']}"
    lineage = json.dumps(
        {"parents": sorted(parents), "improvement": improvement or {}},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    lineage_hash = hashlib.sha256(lineage.encode("utf-8")).hexdigest()[:12]
    key = (
        f"quality-loop:{c['id']}:{stage}:r{c['round_no']}:"
        f"p{c['repair_no']}:l{lineage_hash}"
    )
    conn = kbc.connect(board=c["board"])
    try:
        return kb.create_task(
            conn,
            title=f"[{c['name']}] {label} ({suffix})",
            body=(
                _task_body(c, stage)
                + (_execute_item_body(c, improvement) if stage == "execute" and improvement else "")
                + (_repair_body(correction) if stage == "execute" and correction else "")
                + (_validation_item_body(improvement) if stage == "validate" and improvement else "")
                + (_integration_validation_body(improvement) if integration_validation and improvement else "")
            ),
            assignee=c["assignee"],
            created_by="quality-loop",
            workspace_kind="dir",
            workspace_path=c["workspace"],
            tenant=c["id"],
            priority=10,
            parents=parents,
            idempotency_key=key,
            max_runtime_seconds=7200,
            max_retries=2,
            model_override=c[model_key],
            provider_override=c.get("provider_override"),
            board=c["board"],
        )
    finally:
        conn.close()


def _summary_fallback_handoff(run: Any, expected_role: str | None) -> Optional[dict[str, Any]]:
    """Recover a handoff only when an unstructured completion summary is explicit.

    Some otherwise successful local models omit ``metadata.quality_loop`` from
    ``kanban_complete``. Ambiguous summaries still pause for review instead of being guessed.
    """
    summary = str(run if isinstance(run, str) else getattr(run, "summary", "") or "").strip()
    if not summary or not expected_role:
        return None
    normalized = re.sub(r"\s+", " ", summary.lower())

    if expected_role == "examine":
        complete_markers = (
            "candidate complete",
            "no meaningful work remains",
            "no important work remains",
            "no high-value work remains",
        )
        if any(marker in normalized for marker in complete_markers):
            return {
                "schema": SCHEMA,
                "role": "examine",
                "verdict": "candidate_complete",
                "reason": summary,
                "recovered_from_summary": True,
            }

        actionable = "improvement" in normalized or bool(
            re.search(
                r"\b(add|fix(?:es|ed|ing)?|replace|remove|implement|harden|prevent|avoid|refactor)\b",
                normalized,
            )
        )
        if len(summary) >= 80 and actionable:
            payload: dict[str, Any] = {
                "schema": SCHEMA,
                "role": "examine",
                "verdict": "proposal",
                "implementation_prompt": summary,
                "acceptance_criteria": [
                    "Implement the examiner's stated proposal without unrelated changes.",
                    "Keep the configured build and test gates passing.",
                ],
                "recovered_from_summary": True,
            }
            scores: dict[str, float] = {}
            for category in RANKING_CATEGORIES:
                match = re.search(
                    rf"\b{re.escape(category)}\b\s*[:=]?\s*(10(?:\.0+)?|[0-9](?:\.\d+)?)\b",
                    normalized,
                )
                if match:
                    scores[category] = float(match.group(1))
            if len(scores) == len(RANKING_CATEGORIES):
                payload["score_breakdown"] = scores
                payload["score_rationale"] = summary
            return payload
        return None

    if expected_role == "validate":
        build_passed = any(
            marker in normalized
            for marker in ("build passes", "build passed", "build succeeds", "build succeeded", "build: pass")
        )
        tests_passed = any(
            marker in normalized
            for marker in ("tests pass", "tests passed", "test passes", "test passed", "0 failures")
        )
        no_critical_or_high = (
            "no critical/high" in normalized
            or "no critical or high" in normalized
            or ("no critical" in normalized and "no high" in normalized)
            or ("0 critical" in normalized and "0 high" in normalized)
        )
        no_regressions = "no regressions" in normalized or bool(
            re.search(r"\bno\b[^.]{0,100}\bregressions?\b", normalized)
        )
        explicit_pass = "validation pass" in normalized or "verdict: pass" in normalized
        acceptance_met = (
            "acceptance criteria met" in normalized
            or "all acceptance criteria met" in normalized
            or "acceptance criteria passed" in normalized
        )
        no_errors = "no errors" in normalized or "0 errors" in normalized
        strict_pass = build_passed and tests_passed and no_critical_or_high and no_regressions
        explicit_verified_pass = build_passed and explicit_pass and acceptance_met and no_errors
        if strict_pass or explicit_verified_pass:
            return {
                "schema": SCHEMA,
                "role": "validate",
                "verdict": "pass",
                "build_passed": True,
                "tests_passed": True,
                "critical_issues": 0,
                "high_issues": 0,
                "regressions": 0,
                "recovered_from_summary": True,
            }

        failure_markers = (
            "validation failed",
            "verdict: fail",
            "build failed",
            "build fails",
            "tests failed",
            "tests fail",
            "critical issue found",
            "high-severity issue found",
        )
        if any(marker in normalized for marker in failure_markers):
            return {
                "schema": SCHEMA,
                "role": "validate",
                "verdict": "fail",
                "build_passed": not any(marker in normalized for marker in ("build failed", "build fails")),
                "tests_passed": not any(marker in normalized for marker in ("tests failed", "tests fail")),
                "critical_issues": 1 if "critical issue found" in normalized else 0,
                "high_issues": 1 if "high-severity issue found" in normalized else 0,
                "regressions": 1 if "regression found" in normalized else 0,
                "findings": [summary],
                "correction_prompt": summary,
                "recovered_from_summary": True,
            }
    return None


def _handoff(run: Any, *, expected_role: str | None = None) -> Optional[dict[str, Any]]:
    if not run:
        return None
    meta = run.metadata if isinstance(run.metadata, dict) else None
    if meta:
        candidate = meta.get("quality_loop")
        if isinstance(candidate, dict):
            return candidate
        # Backward compatibility for workers that supplied the handoff object itself as
        # metadata. Ordinary worker bookkeeping (for example worker_session_id) must not mask
        # the summary fallback.
        if meta.get("schema") == SCHEMA and isinstance(meta.get("role"), str):
            return meta
    text = (run.summary or "").strip()
    for line in text.splitlines():
        if not line.startswith("QUALITY_LOOP_JSON: "):
            continue
        try:
            parsed = json.loads(line.removeprefix("QUALITY_LOOP_JSON: ").strip())
            candidate = parsed.get("quality_loop", parsed) if isinstance(parsed, dict) else None
            if isinstance(candidate, dict):
                return candidate
        except (TypeError, json.JSONDecodeError):
            pass
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            candidate = parsed.get("quality_loop", parsed) if isinstance(parsed, dict) else None
            if isinstance(candidate, dict):
                return candidate
        except Exception:
            pass
    return _summary_fallback_handoff(run, expected_role)


def _worker_comment_handoff(
    board: str,
    task_id: str,
    run: Any,
    *,
    expected_role: str,
    expected_author: str,
) -> Optional[dict[str, Any]]:
    """Recover an explicit handoff posted as a worker comment during its run.

    Local models sometimes call ``kanban_comment`` with the complete structured verdict and then
    call ``kanban_complete`` without metadata. Restrict recovery to comments authored by the
    assigned worker during the latest run; arbitrary human comments must never drive automation.
    The existing conservative summary parser still decides whether the text is complete enough.
    """
    if not run:
        return None
    started_at = int(getattr(run, "started_at", 0) or 0)
    ended_at = int(getattr(run, "ended_at", 0) or int(time.time()))
    conn = kbc.connect(board=board)
    try:
        comments = kb.list_comments(conn, task_id)
    finally:
        conn.close()
    eligible = (
        comment
        for comment in reversed(comments)
        if comment.author == expected_author and started_at <= comment.created_at <= ended_at + 5
    )
    for comment in eligible:
        payload = _summary_fallback_handoff(str(comment.body or ""), expected_role)
        if payload:
            payload["recovered_from_comment"] = True
            return payload
    return None


def _ranking_average(payload: dict[str, Any]) -> tuple[float | None, dict[str, float] | None, str | None]:
    raw = payload.get("score_breakdown")
    if not isinstance(raw, dict):
        return None, None, "score_breakdown must contain all five ranking categories"
    missing = [name for name in RANKING_CATEGORIES if name not in raw]
    extra = [name for name in raw if name not in RANKING_CATEGORIES]
    if missing or extra:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unknown: " + ", ".join(map(str, extra)))
        return None, None, "score_breakdown categories invalid (" + "; ".join(details) + ")"
    scores: dict[str, float] = {}
    for name in RANKING_CATEGORIES:
        value = raw[name]
        if isinstance(value, bool):
            return None, None, f"score_breakdown.{name} must be numeric from 0 through 10"
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None, None, f"score_breakdown.{name} must be numeric from 0 through 10"
        if not math.isfinite(score) or not 0 <= score <= 10:
            return None, None, f"score_breakdown.{name} must be numeric from 0 through 10"
        scores[name] = score
    average = round(sum(scores.values()) / len(RANKING_CATEGORIES), 2)
    return average, scores, None


def _git_command(workspace: str, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=workspace, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=timeout, check=False,
    )


def _sensitive_staged_path(path: str) -> bool:
    lowered = path.lower()
    name = Path(lowered).name
    if name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample"}):
        return True
    return Path(lowered).suffix in {".pem", ".key", ".p12", ".pfx"}


def _publish_success(c: dict[str, Any]) -> dict[str, Any]:
    """Commit and push only after final model validation and hard gates passed."""
    workspace = str(c["workspace"])
    result: dict[str, Any] = {"ok": False, "committed": False, "pushed": False}

    root = _git_command(workspace, "rev-parse", "--show-toplevel")
    if root.returncode != 0 or Path(root.stdout.strip()).resolve() != Path(workspace).resolve():
        result["error"] = "workspace is not the root of a Git worktree"
        return result

    branch = str(c.get("publish_branch") or "").strip()
    if not branch:
        current = _git_command(workspace, "branch", "--show-current")
        branch = current.stdout.strip() if current.returncode == 0 else ""
    if not branch or not _GIT_NAME_RE.fullmatch(branch):
        result["error"] = "cannot publish from a detached or invalid branch"
        return result
    checked = _git_command(workspace, "check-ref-format", "--branch", branch)
    if checked.returncode != 0:
        result["error"] = "publish branch failed Git ref validation"
        return result

    remote = str(c.get("publish_remote") or "origin").strip()
    if _git_command(workspace, "remote", "get-url", remote).returncode != 0:
        result["error"] = f"Git remote {remote!r} does not exist"
        return result

    added = _git_command(
        workspace, "add", "-A", "--", ".",
        ":(exclude).quality-loop/**", ":(exclude).hermes/**",
    )
    if added.returncode != 0:
        result["error"] = "git add failed"
        return result

    names = _git_command(workspace, "diff", "--cached", "--name-only", "-z")
    if names.returncode != 0:
        result["error"] = "could not inspect staged paths"
        return result
    staged_paths = [path for path in names.stdout.split("\0") if path]
    if any(_sensitive_staged_path(path) for path in staged_paths):
        _git_command(workspace, "reset", "--quiet")
        result["error"] = "refusing to commit a sensitive credential/key path"
        return result

    diff = _git_command(workspace, "diff", "--cached", "--no-ext-diff", "--unified=0", timeout=120)
    secret_pattern = re.compile(
        r"(?im)^\+.*(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{20,})"
    )
    if diff.returncode != 0 or secret_pattern.search(diff.stdout or ""):
        _git_command(workspace, "reset", "--quiet")
        result["error"] = "refusing to commit because staged secret screening failed"
        return result

    if staged_paths:
        checked_diff = _git_command(workspace, "diff", "--cached", "--check")
        if checked_diff.returncode != 0:
            result["error"] = "staged changes fail git diff --check"
            return result
        committed = _git_command(workspace, "commit", "-m", str(c["commit_message"]), timeout=300)
        if committed.returncode != 0:
            result["error"] = "git commit failed"
            return result
        result["committed"] = True

    revision = _git_command(workspace, "rev-parse", "HEAD")
    if revision.returncode != 0:
        result["error"] = "could not resolve commit after publication preparation"
        return result
    result["commit"] = revision.stdout.strip()
    result["branch"] = branch
    result["remote"] = remote

    pushed = _git_command(workspace, "push", remote, f"HEAD:refs/heads/{branch}", timeout=600)
    if pushed.returncode != 0:
        result["error"] = f"git push failed with exit code {pushed.returncode}"
        return result
    result.update(ok=True, pushed=True)
    return result


def _run_gates(c: dict[str, Any]) -> dict[str, Any]:
    """Run fixed user-configured gates without asking an LLM for the outcome."""
    results: list[dict[str, Any]] = []
    timeout = int(c.get("gate_timeout_seconds") or 900)
    for name, command in (("build", c.get("build_command")), ("test", c.get("test_command"))):
        if not command:
            continue
        started = time.monotonic()
        item: dict[str, Any] = {"name": name, "command": command}
        try:
            completed = subprocess.run(
                command,
                cwd=c["workspace"],
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            item.update(
                exit_code=completed.returncode,
                timed_out=False,
                output=(completed.stdout or "")[-8000:],
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            item.update(exit_code=None, timed_out=True, output=str(output)[-8000:])
        item["duration_seconds"] = round(time.monotonic() - started, 3)
        results.append(item)
        if item.get("exit_code") != 0 or item.get("timed_out"):
            break
    return {
        "ok": bool(results) and all(
            result.get("exit_code") == 0 and not result.get("timed_out") for result in results
        ),
        "commands": results,
        "checked_at": int(time.time()),
    }


def _validation_passed(payload: dict[str, Any], gate_result: dict[str, Any]) -> bool:
    return (
        str(payload.get("verdict", "")).lower() == "pass"
        and payload.get("build_passed") is True
        and payload.get("tests_passed") is True
        and int(payload.get("critical_issues", 1)) == 0
        and int(payload.get("high_issues", 1)) == 0
        and int(payload.get("regressions", 1)) == 0
        and gate_result.get("ok") is True
    )


def _pause(c: dict[str, Any], message: str, state: str = "needs_review", run_id: int | None = None) -> None:
    fields: dict[str, Any] = {"state": state, "message": message}
    if run_id is not None:
        fields["processed_run_id"] = run_id
    _update(c["id"], **fields)


def _queue_next_examination(c: dict[str, Any], parent_id: str, run_id: int | None) -> None:
    if c["round_no"] >= c["max_rounds"]:
        _pause(
            c,
            "Maximum rounds reached after a passing change; final completion was not yet declared",
            state="max_rounds",
            run_id=run_id,
        )
        return
    next_round = c["round_no"] + 1
    next_campaign = dict(c, round_no=next_round, repair_no=0, final_mode=False)
    task_id = _create_task(next_campaign, "examine", [parent_id])
    _update(
        c["id"],
        stage="examine",
        active_task_id=task_id,
        proposal_task_id=None,
        selected_improvement=None,
        slice_index=0,
        slice_count=0,
        round_no=next_round,
        repair_no=0,
        processed_run_id=run_id,
        final_mode=0,
        message=f"Integrated validation passed; round {next_round} fresh examination ready",
    )


def _reconcile_campaign_in_process(campaign_id: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        c = get_campaign(campaign_id)
        if not c or c["state"] != "running":
            return c

        if not c.get("active_task_id"):
            task_id = _create_task(c, c["stage"], [])
            _update(campaign_id, active_task_id=task_id, message=f"{c['stage']} card ready")
            return get_campaign(campaign_id)

        board_conn = kbc.connect(board=c["board"])
        try:
            task = kb.get_task(board_conn, c["active_task_id"])
            run = kb.latest_run(board_conn, c["active_task_id"]) if task else None
        finally:
            board_conn.close()

        if task is None:
            _pause(c, f"Active task {c['active_task_id']} is missing")
            return get_campaign(campaign_id)
        if task.status == "blocked":
            _pause(c, f"Worker blocked on {task.id}: {task.result or task.last_failure_error or 'inspect card'}")
            return get_campaign(campaign_id)
        if task.status != "done":
            return c
        if run and c.get("processed_run_id") == run.id:
            return c

        stage = c["stage"]
        if stage == "examine":
            payload = _handoff(run, expected_role="examine")
            if not payload:
                payload = _worker_comment_handoff(
                    c["board"], task.id, run,
                    expected_role="examine", expected_author=c["assignee"],
                )
            verdict = str((payload or {}).get("verdict", "")).lower()
            improvement = _priority_improvement(payload or {})
            target_average = c.get("target_average")

            if target_average is not None:
                average, ranking, ranking_error = _ranking_average(payload or {})
                if ranking_error:
                    _pause(
                        c,
                        f"Ranked examiner handoff is invalid: {ranking_error}",
                        run_id=run.id if run else None,
                    )
                elif average is not None and average >= float(target_average):
                    c["proposal_task_id"] = task.id
                    c["final_mode"] = True
                    c["last_average"] = average
                    c["last_ranking"] = ranking
                    task_id = _create_task(c, "final_validate", [task.id])
                    _update(
                        campaign_id,
                        stage="final_validate", active_task_id=task_id, proposal_task_id=task.id,
                        processed_run_id=run.id if run else None, final_mode=1,
                        last_average=average, last_ranking=json.dumps(ranking, sort_keys=True),
                        message=(
                            f"Computed ranking average {average:g}/10 reached target "
                            f"{float(target_average):g}/10; final Sol validation ready"
                        ),
                    )
                elif verdict == "proposal" and improvement:
                    c["last_average"] = average
                    c["last_ranking"] = ranking
                    slices = _execution_slices(improvement)
                    current_slice = _slice_item(improvement, 0)
                    task_id = _create_task(c, "execute", [task.id], improvement=current_slice)
                    _update(
                        campaign_id,
                        stage="execute", active_task_id=task_id, proposal_task_id=task.id,
                        processed_run_id=run.id if run else None, final_mode=0,
                        selected_improvement=json.dumps(improvement, sort_keys=True),
                        slice_index=0, slice_count=len(slices), repair_no=0,
                        last_average=average, last_ranking=json.dumps(ranking, sort_keys=True),
                        message=(
                            f"Computed ranking average {average:g}/10 is below target "
                            f"{float(target_average):g}/10; improvement execution ready"
                        ),
                    )
                elif verdict == "candidate_complete":
                    _pause(
                        c,
                        f"Examiner declared candidate_complete at average {average:g}/10, below target "
                        f"{float(target_average):g}/10",
                        run_id=run.id if run else None,
                    )
                else:
                    _pause(
                        c,
                        f"Ranking average {average:g}/10 is below target but examiner did not return a valid improvement prompt",
                        run_id=run.id if run else None,
                    )
            elif verdict == "proposal" and improvement:
                slices = _execution_slices(improvement)
                current_slice = _slice_item(improvement, 0)
                task_id = _create_task(c, "execute", [task.id], improvement=current_slice)
                _update(
                    campaign_id,
                    stage="execute", active_task_id=task_id, proposal_task_id=task.id,
                    processed_run_id=run.id if run else None, final_mode=0,
                    selected_improvement=json.dumps(improvement, sort_keys=True),
                    slice_index=0, slice_count=len(slices), repair_no=0,
                    message="Proposal accepted; execution card ready",
                )
            elif verdict == "candidate_complete":
                c["proposal_task_id"] = task.id
                c["final_mode"] = True
                task_id = _create_task(c, "final_validate", [task.id])
                _update(
                    campaign_id,
                    stage="final_validate", active_task_id=task_id, proposal_task_id=task.id,
                    processed_run_id=run.id if run else None, final_mode=1,
                    message="Examiner reports candidate complete; final audit ready",
                )
            else:
                _pause(c, "Examiner did not return a valid proposal or candidate_complete payload", run_id=run.id if run else None)

        elif stage == "execute":
            if c["final_mode"]:
                validate_stage = "final_validate"
                improvement = None
                integration_validation = False
            elif int(c.get("slice_index") or 0) >= int(c.get("slice_count") or 0) > 0:
                validate_stage = "integrate_validate"
                improvement = _selected_campaign_improvement(c)
                integration_validation = True
            else:
                validate_stage = "validate"
                improvement = _current_campaign_slice(c)
                integration_validation = False
            if validate_stage != "final_validate" and not improvement:
                _pause(
                    c,
                    "Cannot create a scoped validator: the selected examiner improvement is missing or invalid",
                    run_id=run.id if run else None,
                )
                return get_campaign(campaign_id)
            # Validators depend only on the just-completed executor. Scope is copied into the body.
            task_id = _create_task(
                c,
                validate_stage,
                [task.id],
                improvement=improvement,
                integration_validation=integration_validation,
            )
            _update(
                campaign_id, stage=validate_stage, active_task_id=task_id,
                processed_run_id=run.id if run else None,
                message="Execution complete; validation card ready",
            )

        elif stage in {"validate", "integrate_validate", "final_validate"}:
            payload = _handoff(run, expected_role="validate")
            if not payload:
                payload = _worker_comment_handoff(
                    c["board"], task.id, run,
                    expected_role="validate", expected_author=c["assignee"],
                )
            if not payload or str(payload.get("verdict", "")).lower() not in {"pass", "fail"}:
                _pause(c, "Validator did not return the required structured verdict", run_id=run.id if run else None)
                return get_campaign(campaign_id)

            model_claims_pass = (
                str(payload.get("verdict", "")).lower() == "pass"
                and payload.get("build_passed") is True
                and payload.get("tests_passed") is True
                and int(payload.get("critical_issues", 1)) == 0
                and int(payload.get("high_issues", 1)) == 0
                and int(payload.get("regressions", 1)) == 0
            )
            gate_result = _run_gates(c) if model_claims_pass else {
                "ok": False,
                "commands": [],
                "checked_at": int(time.time()),
                "skipped": "validator did not satisfy model-reviewed gates",
            }
            _update(campaign_id, last_gate_result=json.dumps(gate_result, sort_keys=True))

            if _validation_passed(payload, gate_result):
                if stage == "final_validate":
                    score_text = (
                        f"; ranking average {float(c['last_average']):g}/10 reached the "
                        f"{float(c['target_average']):g}/10 average target"
                        if c.get("target_average") is not None and c.get("last_average") is not None
                        else ""
                    )
                    if c.get("publish_on_success"):
                        publish_result = _publish_success(c)
                        _update(
                            campaign_id,
                            last_publish_result=json.dumps(publish_result, sort_keys=True),
                        )
                        if publish_result.get("ok"):
                            _update(
                                campaign_id, state="succeeded",
                                processed_run_id=run.id if run else None,
                                message=(
                                    "Final Sol audit and hard gates passed"
                                    f"{score_text}; committed and pushed "
                                    f"{publish_result.get('commit', '')[:12]} to "
                                    f"{publish_result.get('remote')}/{publish_result.get('branch')}"
                                ),
                            )
                        else:
                            _pause(
                                c,
                                "Final audit passed but publication failed: "
                                + str(publish_result.get("error") or "unknown publication error"),
                                run_id=run.id if run else None,
                            )
                    else:
                        _update(
                            campaign_id, state="succeeded", processed_run_id=run.id if run else None,
                            message=(
                                "Final audit passed: build/tests green and no critical, high, or regression findings"
                                + score_text
                            ),
                        )
                elif stage == "validate":
                    selected = _selected_campaign_improvement(c)
                    slices = _execution_slices(selected) if selected else []
                    slice_index = int(c.get("slice_index") or 0)
                    if len(slices) > 1 and 0 <= slice_index < len(slices) - 1:
                        next_index = slice_index + 1
                        c["slice_index"] = next_index
                        c["repair_no"] = 0
                        next_slice = _slice_item(selected, next_index) if selected else None
                        task_id = _create_task(c, "execute", [task.id], improvement=next_slice)
                        _update(
                            campaign_id,
                            stage="execute",
                            active_task_id=task_id,
                            slice_index=next_index,
                            repair_no=0,
                            processed_run_id=run.id if run else None,
                            message=(
                                f"Slice {slice_index + 1}/{len(slices)} validated; "
                                f"slice {next_index + 1}/{len(slices)} ready"
                            ),
                        )
                    elif len(slices) > 1 and slice_index == len(slices) - 1:
                        c["slice_index"] = len(slices)
                        c["repair_no"] = 0
                        task_id = _create_task(
                            c,
                            "integrate_validate",
                            [task.id],
                            improvement=selected,
                            integration_validation=True,
                        )
                        _update(
                            campaign_id,
                            stage="integrate_validate",
                            active_task_id=task_id,
                            slice_index=len(slices),
                            repair_no=0,
                            processed_run_id=run.id if run else None,
                            message=(
                                f"All {len(slices)} slices passed focused validation; "
                                "integrated validation ready"
                            ),
                        )
                    else:
                        _queue_next_examination(c, task.id, run.id if run else None)
                elif stage == "integrate_validate":
                    _queue_next_examination(c, task.id, run.id if run else None)
            else:
                next_repair = c["repair_no"] + 1
                if next_repair > c["max_repairs"]:
                    _pause(c, f"Validation failed and repair limit ({c['max_repairs']}) was reached", run_id=run.id if run else None)
                else:
                    correction = str(payload.get("correction_prompt") or "").strip()
                    if model_claims_pass and not gate_result.get("ok"):
                        failed = gate_result.get("commands", [])[-1] if gate_result.get("commands") else {}
                        correction = (
                            f"Fix the independently observed {failed.get('name', 'verification')} gate failure. "
                            f"Command: {failed.get('command', 'unknown')}; exit: {failed.get('exit_code')}; "
                            f"output: {str(failed.get('output') or '')[-2000:]}"
                        )
                    if not correction:
                        findings = payload.get("findings") or []
                        correction = "Correct all validator findings: " + "; ".join(map(str, findings))
                    # The failed validator handoff is the repair executor's sole parent context.
                    c["repair_no"] = next_repair
                    repair_scope: Optional[dict[str, Any]] = None
                    if stage == "validate":
                        repair_scope = _current_campaign_slice(c)
                    elif stage == "integrate_validate":
                        selected = _selected_campaign_improvement(c)
                        if selected:
                            repair_scope = dict(selected)
                            repair_scope["integrated_repair"] = True
                    task_id = _create_task(
                        c,
                        "execute",
                        [task.id],
                        improvement=repair_scope,
                        correction=correction,
                    )
                    _update(
                        campaign_id, stage="execute", active_task_id=task_id,
                        repair_no=next_repair, processed_run_id=run.id if run else None,
                        message=f"Validation failed; repair {next_repair} ready: {correction[:240]}",
                    )

        return get_campaign(campaign_id)


def reconcile_campaign(campaign_id: str) -> Optional[dict[str, Any]]:
    with _campaign_process_lock(campaign_id) as acquired:
        if not acquired:
            return get_campaign(campaign_id)
        return _reconcile_campaign_in_process(campaign_id)


def reconcile_all(board: str | None = None) -> None:
    conn = _conn()
    try:
        if board:
            rows = conn.execute(
                "SELECT id FROM campaigns WHERE state = 'running' AND board = ?", (board,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT id FROM campaigns WHERE state = 'running'").fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            reconcile_campaign(row["id"])
        except Exception:
            logger.exception("quality-loop reconcile failed for %s", row["id"])
            _update(row["id"], state="needs_review", message="Controller error; inspect gateway log")

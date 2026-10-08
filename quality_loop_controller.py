"""Durable controller for model-overridden Kanban quality loops."""

from __future__ import annotations

import errno
import json
import hashlib
import logging
import math
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, cast

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
try:
    from hermes_cli import kanban_db_controller as controller_tasks
except ImportError:  # Hermes releases before the controller-task capability.
    controller_tasks = None
from hermes_cli.sqlite_util import open_db
from hermes_constants import get_default_hermes_root

logger = logging.getLogger(__name__)
PLUGIN_ID = "quality-loop"
SCHEMA = "quality-loop/v1"
CARD_SCHEMA = "quality-loop-card/v2"
CARD_ROLES = {
    "discover", "examine", "scope_validate", "plan", "execute", "validate",
    "integrate_validate", "final_validate",
}
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


def _require_controller_task_api() -> Callable[..., str]:
    create = getattr(controller_tasks, "create_controller_task", None)
    if not callable(create):
        raise RuntimeError(
            "Quality Loop requires Hermes' controller-owned Kanban task API; "
            "upgrade Hermes before creating or resuming a campaign"
        )
    return cast(Callable[..., str], create)

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
    discovery_contract TEXT,
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
    initial_snapshot TEXT,
    authenticated_snapshot TEXT,
    state TEXT NOT NULL,
    stage TEXT NOT NULL,
    round_no INTEGER NOT NULL DEFAULT 1,
    repair_no INTEGER NOT NULL DEFAULT 0,
    max_rounds INTEGER NOT NULL DEFAULT 20,
    max_repairs INTEGER NOT NULL DEFAULT 3,
    active_task_id TEXT,
    proposal_task_id TEXT,
    selected_improvement TEXT,
    prompt_profile TEXT NOT NULL DEFAULT 'complete',
    pending_correction TEXT,
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
        "discovery_contract": "TEXT",
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
        "initial_snapshot": "TEXT",
        "authenticated_snapshot": "TEXT",
        "selected_improvement": "TEXT",
        "prompt_profile": "TEXT NOT NULL DEFAULT 'complete'",
        "pending_correction": "TEXT",
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
    if (
        not isinstance(campaign_id, str)
        or not campaign_id
        or campaign_id in {".", ".."}
        or Path(campaign_id).is_absolute()
        or "/" in campaign_id
        or "\\" in campaign_id
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in campaign_id)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", campaign_id)
    ):
        raise ValueError("campaign id is unsafe for process locking")

    lock_name = hashlib.sha256(campaign_id.encode("utf-8")).hexdigest() + ".lock"
    root = get_default_hermes_root().resolve(strict=True)
    directory_fd: int | None = None
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(root, flags)
        try:
            for component in ("plugin-data", PLUGIN_ID, "locks"):
                try:
                    info = os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                    info = os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise RuntimeError("Quality Loop lock directory contains a symlink or escape")
                next_fd = os.open(component, flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(lock_name, file_flags, 0o600, dir_fd=directory_fd)
            handle = os.fdopen(file_fd, "a+b")
        except Exception:
            os.close(directory_fd)
            raise
    else:
        lock_dir = root / "plugin-data" / PLUGIN_ID / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        if lock_dir.is_symlink() or lock_dir.resolve(strict=True).parent != (root / "plugin-data" / PLUGIN_ID).resolve(strict=True):
            raise RuntimeError("Quality Loop lock directory contains a symlink or escape")
        handle = (lock_dir / lock_name).open("a+b")
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
        if directory_fd is not None:
            os.close(directory_fd)


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
    for name in (
        "last_ranking", "last_publish_result", "selected_improvement",
        "initial_snapshot", "authenticated_snapshot",
    ):
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


def _validate_create_repository(root: Path, root_fd: int) -> tuple[set[str], set[str]]:
    git_toplevel = _snapshot_git_run(
        root, "rev-parse", "--show-toplevel", root_fd=root_fd
    )
    if (
        git_toplevel.returncode != 0
        or Path(os.fsdecode(git_toplevel.stdout).strip()).resolve() != root
    ):
        raise ValueError("workspace must equal the canonical Git toplevel")
    try:
        _pinned_head_oid(root_fd)
    except RuntimeError as exc:
        raise ValueError("workspace must have a resolvable HEAD commit") from exc
    try:
        dirty_paths = _raw_dirty_paths(root, root_fd=root_fd)
        untracked_paths = _raw_untracked_paths(root, root_fd=root_fd)
    except RuntimeError as exc:
        raise ValueError("workspace must be a clean isolated Git workspace") from exc
    if not _workspace_root_handle_matches(root_fd, root):
        raise ValueError("workspace root changed during campaign creation validation")
    return dirty_paths, untracked_paths


def _validate_create(
    data: dict[str, Any], *, root_fd: Optional[int] = None,
    pinned_workspace: Optional[Path] = None,
) -> dict[str, Any]:
    workspace = Path(str(data.get("workspace") or "")).expanduser()
    if not workspace.is_absolute():
        raise ValueError("workspace must be an absolute path")
    if not workspace.is_dir():
        raise ValueError(f"workspace directory does not exist: {workspace}")
    canonical_workspace = workspace.resolve()
    if pinned_workspace is not None and canonical_workspace != pinned_workspace:
        raise ValueError("workspace changed before campaign creation validation")
    owned_root = root_fd is None
    if owned_root:
        root_fd = _open_workspace_root(canonical_workspace)
    assert root_fd is not None
    try:
        if owned_root:
            with _pinned_git_context(canonical_workspace, root_fd):
                dirty_paths, untracked_paths = _validate_create_repository(
                    canonical_workspace, root_fd
                )
        else:
            dirty_paths, untracked_paths = _validate_create_repository(
                canonical_workspace, root_fd
            )
    finally:
        if owned_root:
            os.close(root_fd)
    if dirty_paths or untracked_paths:
        raise ValueError(
            "workspace must be a clean isolated Git workspace; commit a campaign baseline in a "
            "dedicated worktree or snapshot clone before starting"
        )
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

    gate_timeout_seconds = int(data.get("gate_timeout_seconds") or 900)
    if not 10 <= gate_timeout_seconds <= 3600:
        raise ValueError("gate_timeout_seconds must be between 10 and 3600")
    raw_target = data.get("target_average")
    target_average = None if raw_target in (None, "") else float(raw_target)
    if target_average is not None and (not math.isfinite(target_average) or not 0 < target_average <= 10):
        raise ValueError("target_average must be greater than 0 and at most 10")
    publish_on_success = bool(data.get("publish_on_success", False))
    prompt_profile = str(data.get("prompt_profile") or "complete").strip().lower()
    if prompt_profile not in {"complete", "simple"}:
        raise ValueError("prompt_profile must be 'complete' or 'simple'")
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
        "workspace": str(canonical_workspace),
        "assignee": assignee,
        **models,
        "provider_override": (str(data.get("provider_override") or "").strip() or None),
        "build_command": build_command,
        "test_command": test_command,
        "gate_timeout_seconds": gate_timeout_seconds,
        "target_average": target_average,
        "publish_on_success": publish_on_success,
        "prompt_profile": prompt_profile,
        "publish_remote": publish_remote,
        "publish_branch": publish_branch,
        "commit_message": commit_message,
        "max_rounds": max_rounds,
        "max_repairs": max_repairs,
    }


def create_campaign(data: dict[str, Any]) -> dict[str, Any]:
    _require_controller_task_api()
    requested_workspace = Path(str(data.get("workspace") or "")).expanduser()
    if not requested_workspace.is_absolute():
        raise ValueError("workspace must be an absolute path")
    canonical_workspace = requested_workspace.resolve(strict=True)
    root_fd = _open_workspace_root(canonical_workspace)
    try:
        with _pinned_git_context(canonical_workspace, root_fd):
            clean = _validate_create(
                data, root_fd=root_fd, pinned_workspace=canonical_workspace
            )
            initial_snapshot = _execution_git_state_with_root(
                canonical_workspace, None, root_fd
            )
            discovery_contract = (
                None
                if clean["build_command"] or clean["test_command"]
                else _discover_project_contract(canonical_workspace, root_fd)
            )
            if not _workspace_root_handle_matches(root_fd, canonical_workspace):
                raise ValueError("workspace root changed during campaign creation")
    finally:
        os.close(root_fd)
    # Opening the board now catches an invalid/missing board before state is stored.
    board_conn = kbc.connect(board=clean["board"])
    board_conn.close()
    campaign_id = "ql_" + uuid.uuid4().hex[:12]
    now = int(time.time())
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        terminal_states = sorted(TERMINAL_STATES)
        placeholders = ", ".join("?" for _ in terminal_states)
        duplicate = conn.execute(
            "SELECT id FROM campaigns WHERE board = ? AND workspace = ? "
            f"AND state NOT IN ({placeholders}) LIMIT 1",
            (clean["board"], clean["workspace"], *terminal_states),
        ).fetchone()
        if duplicate:
            raise ValueError(
                f"campaign {duplicate['id']} already targets this workspace and board"
            )
        conn.execute(
            """
            INSERT INTO campaigns (
                id, name, board, workspace, assignee,
                examiner_model, executor_model, validator_model, provider_override,
                build_command, test_command, discovery_contract, gate_timeout_seconds,
                target_average, publish_on_success, publish_remote, publish_branch, commit_message,
                prompt_profile,
                initial_snapshot, authenticated_snapshot,
                state, stage, round_no, repair_no, max_rounds, max_repairs,
                created_at, updated_at, message
            ) VALUES (
                :id, :name, :board, :workspace, :assignee,
                :examiner_model, :executor_model, :validator_model, :provider_override,
                :build_command, :test_command, :discovery_contract, :gate_timeout_seconds,
                :target_average, :publish_on_success, :publish_remote, :publish_branch, :commit_message,
                :prompt_profile,
                :initial_snapshot, :authenticated_snapshot,
                'running', :initial_stage, 1, 0, :max_rounds, :max_repairs,
                :created_at, :updated_at, :message
            )
            """,
            {
                "id": campaign_id,
                **clean,
                "publish_on_success": int(clean["publish_on_success"]),
                "initial_snapshot": json.dumps(initial_snapshot, sort_keys=True),
                "authenticated_snapshot": json.dumps(initial_snapshot, sort_keys=True),
                "discovery_contract": (
                    json.dumps(discovery_contract, sort_keys=True)
                    if discovery_contract is not None else None
                ),
                "initial_stage": "discover" if discovery_contract is not None else "examine",
                "created_at": now,
                "updated_at": now,
                "message": (
                    "Creating project discovery card"
                    if discovery_contract is not None
                    else "Creating first examination card"
                ),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    try:
        reconcile_campaign(campaign_id)
    except Exception as exc:
        persisted = get_campaign(campaign_id)
        if persisted is not None and not persisted.get("active_task_id"):
            _update(
                campaign_id,
                state="needs_review",
                message=(
                    "Campaign creation failed before first card: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        raise
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
    if action not in {"pause", "resume", "stop"}:
        raise ValueError(f"unknown action: {action}")
    if action == "resume":
        _require_controller_task_api()
    with _campaign_process_lock(campaign_id) as acquired:
        if not acquired:
            raise ValueError(f"campaign {campaign_id} is busy reconciling or changing state")
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, board, workspace, active_task_id, processed_run_id "
                "FROM campaigns WHERE id = ?",
                (campaign_id,),
            ).fetchone()
            if row is None:
                raise KeyError(campaign_id)
            state = str(row["state"])

            if action == "pause":
                if state == "paused":
                    conn.rollback()
                    return get_campaign(campaign_id) or {}
                if state != "running":
                    raise ValueError(f"a {state} campaign cannot be paused")
                next_state = "paused"
                message = "Paused by user; active worker is not terminated"
            elif action == "stop":
                if state == "stopped":
                    conn.rollback()
                    return get_campaign(campaign_id) or {}
                if state not in {"running", "paused"}:
                    raise ValueError(f"a {state} campaign cannot be stopped")
                next_state = "stopped"
                message = "Stopped by user; no new cards will be created"
            else:
                if state == "running":
                    raise ValueError("campaign is already running")
                if state not in {"paused", "stopped"}:
                    raise ValueError(f"a {state} campaign cannot be resumed")

                canonical_workspace = Path(row["workspace"]).resolve()
                terminal_states = sorted(TERMINAL_STATES)
                placeholders = ", ".join("?" for _ in terminal_states)
                candidates = conn.execute(
                    "SELECT id, workspace FROM campaigns WHERE id != ? AND board = ? "
                    f"AND state NOT IN ({placeholders})",
                    (campaign_id, row["board"], *terminal_states),
                ).fetchall()
                duplicate = next(
                    (
                        candidate
                        for candidate in candidates
                        if Path(candidate["workspace"]).resolve() == canonical_workspace
                    ),
                    None,
                )
                if duplicate:
                    raise ValueError(
                        f"campaign {duplicate['id']} already targets this workspace and board"
                    )

                task = None
                run = None
                if row["active_task_id"]:
                    board_conn = kbc.connect(board=row["board"])
                    try:
                        task = kb.get_task(board_conn, row["active_task_id"])
                        run = kb.latest_run(board_conn, row["active_task_id"]) if task else None
                    finally:
                        board_conn.close()
                if task and task.status == "done" and run is None:
                    raise ValueError("completed active card has no run and cannot be resumed safely")
                if (
                    task
                    and task.status == "done"
                    and run
                    and row["processed_run_id"] == run.id
                ):
                    raise ValueError(
                        "completed active run was already processed; refusing to replay it on resume"
                    )
                next_state = "running"
                message = "Resumed; reconciling current card"

            conn.execute(
                "UPDATE campaigns SET state = ?, message = ?, updated_at = ? WHERE id = ?",
                (next_state, message, int(time.time()), campaign_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if action == "resume":
            return _reconcile_campaign_in_process(campaign_id) or {}
        return get_campaign(campaign_id) or {}


def _task_body(c: dict[str, Any], stage: str) -> str:
    marker = json.dumps(
        {"schema": CARD_SCHEMA, "campaign_id": c["id"], "role": stage},
        sort_keys=True,
        separators=(",", ":"),
    )
    simple = str(c.get("prompt_profile") or "complete") == "simple"
    completion_guidance = (
        "If this tool model cannot reliably construct nested metadata, pass the same role payload "
        "through the typed top-level `quality_loop` compatibility argument; the handler stores it "
        "canonically as `metadata.quality_loop`. "
        if stage in {"execute", "validate", "integrate_validate", "final_validate"}
        else ""
    )
    header = f"""QUALITY LOOP CAMPAIGN: {c['id']}
ROUND: {c['round_no']}
ROLE: {stage.upper()}
TRUSTED_QUALITY_LOOP_CARD: {marker}
WORKSPACE: {c['workspace']}

This is an autonomous Kanban stage. Work only inside the assigned workspace.
The final board action MUST be kanban_complete or kanban_block.
For kanban_complete, put the role payload under `metadata.quality_loop` and write a concise
human-readable summary. {completion_guidance}Never bury the payload in summary prose or summary
JSON: hardened cards reject unstructured completion and remain in flight for a retry.
Do not repeat a failing completion call unchanged.
"""
    if simple:
        header += (
            "The stage instructions below are intentionally short. Follow them exactly and stop.\n"
        )
    if stage == "discover":
        if simple:
            return header + """
READ-ONLY: inspect only. Do not modify or install anything.
The trusted contract lists allowed commands. Pick build/test commands and max_repairs only from
TRUSTED_DISCOVERY_CONTRACT. Put the `discover` payload under `metadata.quality_loop`.
"""
        return header + """
READ-ONLY PROJECT DISCOVERY: identify the project type, languages, tracked manifests, and exact
runtime/build/test commands before any quality ranking or implementation work starts. Do not modify,
install, create, delete, stage, commit, reset, or restore files. The controller has independently
derived a trusted candidate set from authenticated repository bytes and fixed host executables.
Read the named manifests, verify the evidence, and select only commands present in
TRUSTED_DISCOVERY_CONTRACT. Do not use the Hermes-bundled runtime, ambient PATH, invented commands,
shell aliases, curl probes, directory placeholders, or commands copied from prose when they are not
listed in that contract. Choose max_repairs from the bounded trusted candidates: use the smallest
number justified by project complexity and test feedback cost. Put the `discover` payload under
`metadata.quality_loop`. At least one selected command must be non-empty. Every non-empty value must
byte-match the trusted contract. The controller, not this worker, persists the selected configuration.
"""
    if stage == "examine":
        target = c.get("target_average")
        last = c.get("last_average")
        target_line = (
            f"TARGET AVERAGE: {float(target):g}/10"
            + (f"\nPREVIOUS COMPUTED AVERAGE: {float(last):g}/10" if last is not None else "")
            if target is not None
            else "No numeric completion target is configured; use candidate_complete only when no critical or high-value defect remains."
        )
        validator = str(c.get("validator_model") or "the configured validation model")
        completion_action = (
            "After final validation passes, the controller will commit and push the configured branch."
            if c.get("publish_on_success")
            else "After final validation passes, the campaign will stop successfully without committing or pushing."
        )
        categories = "\n".join(
            f"   - {name}" for name in RANKING_CATEGORIES
        )
        if simple:
            return header + f"""
READ-ONLY: inspect the project by reading files only. Do not modify anything and do not run
ANY commands — no installs, no tests, no builds, nothing.
{target_line}

Score these five categories from 0.0 to 10.0:
{categories}

Then pick the ONE highest-priority defect. Return verdict=proposal with one selected_defect
(title, description, evidence, proposed_outcome). Return verdict=candidate_complete with no
selected_defect only when nothing important remains. Put the `examine` payload under
`metadata.quality_loop`. {completion_action}
"""
        return header + f"""
READ-ONLY EXAMINATION: inspect the CURRENT project without modifying source files or creating logs,
temporary files, caches, or a .quality-loop directory inside the workspace.
{target_line}

Do only these four things:
1. Inspect the project and existing repository evidence.
2. Score all five categories independently from 0.0 to 10.0:
   - correctness_reliability: correctness, failure handling, data integrity, concurrency
   - security_safety: secrets, unsafe operations, input boundaries, privacy
   - architecture_maintainability: design, coupling, clarity, duplication, evolvability
   - test_quality: meaningful coverage, regression protection, determinism
   - user_experience_performance: observable UX, responsiveness, resource use
3. Identify the single highest-priority defect when meaningful work remains.
4. Provide concise evidence and a short observable proposed outcome.

Do not scope files, define component boundaries, write implementation instructions, create execution
slices, or plan the work. Later stages own those responsibilities. Do not run dependency installation
or the full build or test suite. Prefer repository inspection and existing artifacts; run only a focused,
read-only check when it is genuinely necessary to substantiate a score.

The controller computes the arithmetic average. Use proposal with exactly one selected_defect when
work remains; use candidate_complete without a selected_defect only when the project is ready for final
validation by {validator}. Put the `examine` payload under `metadata.quality_loop`.
{completion_action}
"""
    if stage == "execute":
        if simple:
            return header + """
Implement only the selected item in this card. Modify the allowed files, run the exact
verification commands, then call kanban_complete with the `execute` payload under
`metadata.quality_loop`.
Do NOT commit, stage, or run any git command that changes history — the controller owns Git.
If a required change falls outside the allowlist, call kanban_block instead of broadening scope.
"""
        return header + """
Implement the specification or correction in the parent task result.
Stay within scope, modify the code, add/update tests, and run relevant verification.
Do not commit, stage, or otherwise change Git history or HEAD — the controller owns the
repository state and authenticates your exact working-tree delta at completion time.
If a hidden dependency would require another component, boundary, or file outside the allowlist,
stop and call kanban_block with the exact dependency instead of broadening the task. After the exact
verification commands pass, immediately call kanban_complete; do not perform optional cleanup,
additional refactoring, file-size analysis, or exploratory work. Put the `execute` payload under
`metadata.quality_loop`.
"""
    if stage == "scope_validate":
        if simple:
            return header + """
READ-ONLY: do not modify, create, or delete files.
Turn the selected defect or repair request into ONE scoped_improvement: one component, one
behavior, one boundary, short implementation instructions, acceptance criteria, the exact trusted
verification commands, at most five relevant files, excluded scope, and risks. If it cannot be
scoped safely, return verdict=fail with a correction_prompt. Do not create execution slices.
Put the `scope_validate` payload under `metadata.quality_loop`.
"""
        return header + """
READ-ONLY SCOPE VALIDATION: do not modify, create, delete, stage, commit, reset, or restore files.
Convert the selected defect or repair request into exactly one component, one observable behavior,
and one immediate dependency or state boundary. Inspect the real production import, mutable-state,
lifecycle, database, queue, timer, rendering, and configuration seams before choosing the boundary.
Produce one scoped_improvement with narrow implementation instructions, acceptance criteria, the exact
trusted verification commands, no more than five relevant files, explicit excluded scope, and risks.
Return FAIL with a correction prompt when the finding cannot be scoped safely. Do not decompose the
work or create execution slices; the planning stage owns that decision. Put the `scope_validate`
payload under `metadata.quality_loop`.
"""
    if stage == "plan":
        if simple:
            return header + """
READ-ONLY: do not modify, create, or delete files.
Review the validated scoped improvement. If it fits in at most two relevant files, return
decomposition_required=false with no execution_slices. Only when the work genuinely needs ordered
steps, return decomposition_required=true with 2 to 5 execution_slices; every slice keeps the same
component and boundary, touches at most two files, and together they cover exactly the parent files.
Put the `plan` payload under `metadata.quality_loop`.
"""
        return header + """
READ-ONLY PLANNING: do not modify, create, delete, stage, commit, reset, or restore files.
Review the already validated scoped improvement. Keep it as one bounded execution item whenever it can
be changed and verified safely in at most two relevant files. Set decomposition_required to false in
that case and do not include execution_slices.

Only when dependent steps are genuinely required, set decomposition_required to true and create 2 to 5
ordered execution_slices. Every slice must keep the same component and immediate boundary, change one
coherent behavior, use the exact trusted verification commands, touch at most two relevant files, stay
inside the parent allowlist, and preserve its excluded scope. The slices must cover exactly the parent
relevant files. Put the `plan` payload under `metadata.quality_loop`.
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
    if simple:
        return header + f"""
{final_text}
READ-ONLY: do not modify files. Inspect the code and git diff, verify only this card's acceptance
criteria, and run the required build/tests.

{gate_lines}

If anything fails or is incomplete, return verdict=fail with a correction_prompt; never fix it
yourself. On success return verdict=pass with build_passed=true, tests_passed=true, and zero
critical/high/regression counts. Put the `validate` payload under `metadata.quality_loop`.

Recovery fallback: your summary MUST also state: Verdict PASS or FAIL; build passes or fails;
tests pass or fail; acceptance criteria met or unmet; 0 critical issues; 0 high issues; 0 regressions.
On FAIL, include a specific correction prompt in the summary.
"""
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
Put the `validate` payload under `metadata.quality_loop`. PASS is allowed only when build and tests
pass, every parent acceptance criterion is met, and critical/high/regression counts are zero.
As a recovery fallback, your human-readable summary MUST also state all of: Verdict PASS or FAIL;
build passes or fails; tests pass or fail; acceptance criteria met or unmet; 0 critical issues;
0 high issues; and 0 regressions. On FAIL, include a specific correction prompt in the summary.
"""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _strict_string_list(
    value: Any, *, allow_empty: bool = False, unique: bool = True
) -> Optional[list[str]]:
    if not isinstance(value, list) or (not value and not allow_empty):
        return None
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    normalized = [item.strip() for item in value]
    if unique and len(normalized) != len(set(normalized)):
        return None
    return normalized


def _campaign_commands(c: dict[str, Any]) -> list[str]:
    commands = [
        str(command).strip()
        for command in (c.get("build_command"), c.get("test_command"))
        if str(command or "").strip()
    ]
    return list(dict.fromkeys(commands))


_REPOSITORY_CONTROL_NAMES = {
    ".git", ".gitignore", ".gitattributes", ".gitmodules",
    ".git-blame-ignore-revs", ".mailmap", ".hg", ".svn",
}


def _safe_repo_file(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith(("/", "\\"))
        and "\\" not in value
        and not any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and not any(part.lower() in _REPOSITORY_CONTROL_NAMES for part in value.split("/"))
    )


def _relevant_files_error(
    workspace: str | Path, files: list[str], *, root_fd: Optional[int] = None,
) -> Optional[str]:
    """Validate an allowlist through stable descriptor-relative access."""
    root = Path(workspace)
    owned_fd = root_fd is None
    try:
        if owned_fd:
            root = root.resolve(strict=True)
            root_fd = _open_workspace_root(root)
        assert root_fd is not None
        if owned_fd:
            with _pinned_git_context(root, root_fd):
                error = _relevant_files_error_with_root(root, files, root_fd)
                if not _workspace_root_handle_matches(root_fd, root):
                    return "workspace root changed during relevant-file validation"
        else:
            error = _relevant_files_error_with_root(root, files, root_fd)
        return error
    except OSError:
        return "workspace is not a resolvable directory"
    finally:
        if owned_fd and root_fd is not None:
            os.close(root_fd)


def _relevant_files_error_with_root(
    root: Path, files: list[str], root_fd: int,
) -> Optional[str]:
    top = _snapshot_git_run(
        root, "rev-parse", "--show-toplevel", root_fd=root_fd
    )
    if top.returncode != 0 or Path(os.fsdecode(top.stdout).strip()).resolve() != root:
        return "workspace is not the canonical Git toplevel"
    for value in files:
        if not _safe_repo_file(value):
            return f"unsafe repository path: {value!r}"
        try:
            entry = _read_workspace_entry(root, value, root_fd=root_fd)
            if entry is None:
                parent_fd, _name = _open_workspace_parent(root, value, root_fd=root_fd)
                os.close(parent_fd)
            elif entry[0] != "file":
                return f"relevant path must be a regular non-symlink file: {value}"
        except FileNotFoundError:
            return f"new path requires an existing real parent: {value}"
        except (OSError, RuntimeError):
            return f"path cannot be inspected safely: {value}"
        ignored = _snapshot_git_run(
            root, "check-ignore", "--no-index", "-q", "--", value,
            root_fd=root_fd,
        )
        if ignored.returncode == 0:
            return f"ignored paths are not allowed: {value}"
        if ignored.returncode != 1:
            return f"could not determine ignore status: {value}"
    return None


def _normalize_slice(
    raw: Any,
    c: dict[str, Any],
    *,
    parent: Optional[dict[str, Any]] = None,
    allow_priority: bool = True,
    max_files: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    # Strict field validation: reject unknown keys
    allowed_fields = {
        "title", "component", "behavior", "boundary", "implementation_prompt",
        "acceptance_criteria", "verification_commands", "relevant_files",
        "excluded_scope", "risks",
    }
    if allow_priority:
        allowed_fields.update({"priority", "execution_slices"})
    # Slice lineage metadata is controller-owned: a scope validator may echo it from
    # its input, but it is never trusted from the payload and is re-derived later.
    lineage_fields = {"slice_index", "slice_count", "parent_title", "integrated_repair"}
    unknown = set(raw) - allowed_fields - lineage_fields
    if unknown:
        return None
    scalars = {}
    for name in ("title", "component", "behavior", "boundary", "implementation_prompt"):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            return None
        scalars[name] = value.strip()
    criteria = _strict_string_list(raw.get("acceptance_criteria"))
    files = _strict_string_list(raw.get("relevant_files"))
    exclusions = _strict_string_list(raw.get("excluded_scope"))
    commands = _strict_string_list(raw.get("verification_commands"))
    risks = _strict_string_list(raw.get("risks", []), allow_empty=True)
    if criteria is None or files is None or exclusions is None or commands is None or risks is None:
        return None
    is_sliced_parent = parent is None and allow_priority and "execution_slices" in raw
    file_limit = max_files if max_files is not None else (5 if is_sliced_parent else 2)
    if (
        not 1 <= len(files) <= file_limit
        or any(not _safe_repo_file(value) for value in files)
        or _relevant_files_error(c["workspace"], files)
    ):
        return None
    if commands != _campaign_commands(c):
        return None
    if parent and (
        scalars["component"] != parent["component"]
        or scalars["boundary"] != parent["boundary"]
        or not set(files).issubset(parent["relevant_files"])
        or not set(parent["excluded_scope"]).issubset(exclusions)
    ):
        return None
    return {
        **scalars,
        "acceptance_criteria": criteria,
        "verification_commands": commands,
        "relevant_files": files,
        "excluded_scope": exclusions,
        "risks": risks,
    }


def _selected_defect(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw = payload.get("selected_defect")
    if not isinstance(raw, dict) or set(raw) != {
        "title", "description", "evidence", "proposed_outcome"
    }:
        return None
    for name in ("title", "description", "proposed_outcome"):
        if not isinstance(raw.get(name), str) or not raw[name].strip():
            return None
    evidence = _strict_string_list(raw.get("evidence"))
    if evidence is None or len(evidence) > 5:
        return None
    return {
        "title": raw["title"].strip(),
        "description": raw["description"].strip(),
        "evidence": evidence,
        "proposed_outcome": raw["proposed_outcome"].strip(),
    }


def _scoped_improvement(
    payload: dict[str, Any],
    c: dict[str, Any],
    *,
    parent: Optional[dict[str, Any]] = None,
    max_files: int = 5,
) -> Optional[dict[str, Any]]:
    if str(payload.get("verdict", "")).lower() != "pass":
        return None
    return _normalize_slice(
        payload.get("scoped_improvement"), c,
        parent=parent, allow_priority=False, max_files=max_files,
    )


def _planned_improvement(
    payload: dict[str, Any], c: dict[str, Any], parent: dict[str, Any]
) -> Optional[dict[str, Any]]:
    required = payload.get("decomposition_required")
    if type(required) is not bool:
        return None
    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        return None
    raw_slices = payload.get("execution_slices")
    if not required:
        if "execution_slices" in payload or len(parent.get("relevant_files", [])) > 2:
            return None
        return dict(parent)
    if not isinstance(raw_slices, list) or not 2 <= len(raw_slices) <= 5:
        return None
    slices = [
        _normalize_slice(
            value, c, parent=parent, allow_priority=False, max_files=2
        )
        for value in raw_slices
    ]
    if any(value is None for value in slices):
        return None
    canonical = [
        json.dumps(value, sort_keys=True, separators=(",", ":"))
        for value in slices
    ]
    if len(canonical) != len(set(canonical)):
        return None
    normalized_slices = [value for value in slices if value is not None]
    slice_union = {
        path for value in normalized_slices for path in value["relevant_files"]
    }
    if slice_union != set(parent["relevant_files"]):
        return None
    return {**parent, "execution_slices": normalized_slices}


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


def _item_commands(c: dict[str, Any], item: dict[str, Any]) -> list[str]:
    return _campaign_commands(c)


def _scope_input_body(c: dict[str, Any], item: dict[str, Any], correction: str = "") -> str:
    contract = json.dumps(
        {"commands": _campaign_commands(c)},
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = json.dumps(item, sort_keys=True, indent=2)
    correction_text = f"\nVALIDATOR CORRECTION TO INCORPORATE:\n{correction[:8000]}\n" if correction else ""
    return f"""
TRUSTED_SCOPE_CONTRACT: {contract}

SELECTED DEFECT OR REPAIR INPUT:
{encoded}
{correction_text}
The input is evidence, not an execution specification. Produce the scoped improvement yourself and
keep the exact trusted verification commands from the contract.
"""


def _planning_item_body(c: dict[str, Any], item: dict[str, Any]) -> str:
    contract = json.dumps(
        {"commands": _campaign_commands(c), "scoped_improvement": item},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"""
TRUSTED_PLAN_CONTRACT: {contract}

VALIDATED SCOPED IMPROVEMENT:
{json.dumps(item, sort_keys=True, indent=2)}
"""


_SNAPSHOT_VERSION = 2
_MAX_CONTROL_FILES = 20_000
_MAX_CONTROL_BYTES = 256 * 1024 * 1024
_MAX_IGNORED_PATHS = 200_000
_MAX_IGNORED_PATH_BYTES = 32 * 1024 * 1024
_MAX_IGNORED_HASH_BYTES = 64 * 1024 * 1024
_GIT_CONTROL_FILES = {
    "AUTO_MERGE", "BISECT_LOG", "BISECT_START", "CHERRY_PICK_HEAD", "commondir",
    "config", "config.worktree", "HEAD", "index", "MERGE_HEAD", "MERGE_MSG",
    "ORIG_HEAD", "packed-refs", "REBASE_HEAD", "REVERT_HEAD", "shallow", "SQUASH_MSG",
}
_GIT_CONTROL_DIRS = {
    "branches", "hooks", "info", "rebase-apply", "rebase-merge", "refs", "sequencer",
}


def _workspace_fd_path(root_fd: int) -> str:
    proc_path = f"/proc/self/fd/{root_fd}"
    if Path("/proc/self/fd").is_dir():
        return proc_path
    if Path("/dev/fd").is_dir():
        return f"/dev/fd/{root_fd}"
    raise RuntimeError("descriptor-bound Git execution is unavailable")


_PINNED_GIT = threading.local()


def _read_control_file(directory_fd: int, name: str, limit: int = 64 * 1024) -> bytes | None:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"could not securely open Git control file {name}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"Git control path is not a regular file: {name}")
        chunks: list[bytes] = []
        consumed = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit - consumed + 1))
            if not chunk:
                break
            chunks.append(chunk)
            consumed += len(chunk)
            if consumed > limit:
                raise RuntimeError(f"Git control file exceeds bounded read limit: {name}")
        after = os.fstat(fd)
        if (
            (before.st_dev, before.st_ino, before.st_mode, before.st_size,
             before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns)
            or after.st_size != consumed
        ):
            raise RuntimeError(f"Git control file changed during read: {name}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _open_retained_control_file(
    directory_fd: int, name: str, limit: int = 64 * 1024,
) -> tuple[int, os.stat_result, bytes]:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise RuntimeError(f"could not pin Git control file {name}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"Git control path is not a regular file: {name}")
        chunks: list[bytes] = []
        consumed = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit - consumed + 1))
            if not chunk:
                break
            chunks.append(chunk)
            consumed += len(chunk)
            if consumed > limit:
                raise RuntimeError(f"Git control file exceeds bounded read limit: {name}")
        after = os.fstat(fd)
        if (
            (before.st_dev, before.st_ino, before.st_mode, before.st_size,
             before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns)
            or after.st_size != consumed
        ):
            raise RuntimeError(f"Git control file changed during read: {name}")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd, after, b"".join(chunks)
    except Exception:
        os.close(fd)
        raise


def _packed_ref_oid(packed: bytes, ref: str) -> str:
    for raw_line in packed.splitlines():
        if not raw_line or raw_line.startswith((b"#", b"^")):
            continue
        try:
            raw_oid, raw_ref = raw_line.split(b" ", 1)
        except ValueError as exc:
            raise RuntimeError("packed-refs is malformed") from exc
        if os.fsdecode(raw_ref) == ref:
            return _validate_object_id(raw_oid.decode("ascii"))
    raise RuntimeError("Git HEAD reference is unresolved")


class _PinnedGitContext:
    def __init__(self, root: Path, root_fd: int) -> None:
        self.root = root
        self.root_fd = root_fd
        self.dotgit = os.stat(".git", dir_fd=root_fd, follow_symlinks=False)
        self.git_fd = -1
        self.common_fd = -1
        self.refs_fd = -1
        self.objects_fd = -1
        self.index_fd = -1
        self.config_fd = -1
        self.head_fd = -1
        self.ref_fd = -1
        self.ref_parent_fd = -1
        self.packed_refs_fd = -1
        self.allow_final_mismatch = False
        self._git_tmp: Optional[tempfile.TemporaryDirectory[str]] = None
        self.git_path: Path
        self.common_path: Path
        try:
            if stat.S_ISDIR(self.dotgit.st_mode):
                self.git_fd = os.open(
                    ".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
                )
                self.git_path = root / ".git"
            elif stat.S_ISREG(self.dotgit.st_mode):
                dotgit = _read_control_file(root_fd, ".git", 16 * 1024)
                if dotgit is None or not dotgit.startswith(b"gitdir: "):
                    raise RuntimeError("worktree .git file is malformed")
                raw = os.fsdecode(dotgit[8:].strip())
                candidate = Path(raw) if os.path.isabs(raw) else root / raw
                self.git_path = Path(os.path.abspath(os.path.normpath(candidate)))
                self.git_fd = _open_workspace_root(self.git_path)
            else:
                raise RuntimeError("workspace .git is not a real directory or regular file")
            commondir = _read_control_file(self.git_fd, "commondir", 16 * 1024)
            if commondir is None:
                self.common_path = self.git_path
                self.common_fd = os.dup(self.git_fd)
            else:
                raw_common = os.fsdecode(commondir.strip())
                candidate = Path(raw_common) if os.path.isabs(raw_common) else self.git_path / raw_common
                self.common_path = Path(os.path.abspath(os.path.normpath(candidate)))
                self.common_fd = _open_workspace_root(self.common_path)
            self.refs_fd = os.open(
                "refs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.common_fd,
            )
            self.objects_fd = os.open(
                "objects", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.common_fd,
            )
            self.index_fd, self.index_info, _index = _open_retained_control_file(
                self.git_fd, "index", _MAX_CONTROL_BYTES
            )
            try:
                os.stat("config", dir_fd=self.common_fd, follow_symlinks=False)
            except FileNotFoundError:
                self.config_info = None
                self.config_bytes = b""
            else:
                self.config_fd, self.config_info, self.config_bytes = _open_retained_control_file(
                    self.common_fd, "config", _MAX_CONTROL_BYTES
                )
            self.head_fd, self.head_info, head_bytes = _open_retained_control_file(
                self.git_fd, "HEAD", 4096
            )
            self._capture_head(head_bytes)
            self._git_tmp = tempfile.TemporaryDirectory(prefix="quality-loop-git-control-")
            shim = Path(self._git_tmp.name)
            (shim / "refs").mkdir()
            (shim / "objects").mkdir()
            (shim / "HEAD").write_text("ref: refs/heads/__quality_loop__\n", encoding="ascii")
            object_format = "sha1"
            if self.config_fd >= 0:
                configured = _pinned_config_command(
                    self, "--get", "extensions.objectformat"
                )
                candidate = configured.stdout.strip().lower()
                if configured.returncode == 0 and candidate:
                    if candidate not in {"sha1", "sha256"}:
                        raise RuntimeError("Git repository uses an unsupported object format")
                    object_format = candidate
            if object_format == "sha256":
                (shim / "config").write_text(
                    "[core]\n\trepositoryformatversion = 1\n"
                    "[extensions]\n\tobjectFormat = sha256\n",
                    encoding="ascii",
                )
        except Exception:
            self.close()
            raise

    @property
    def shim_git_dir(self) -> str:
        if self._git_tmp is None:
            raise RuntimeError("pinned Git context is closed")
        return self._git_tmp.name

    def _capture_head(self, raw_head: bytes) -> None:
        text = os.fsdecode(raw_head).strip()
        self.head_ref: Optional[str] = None
        self.ref_name: Optional[str] = None
        self.ref_info: Optional[os.stat_result] = None
        self.packed_refs_info: Optional[os.stat_result] = None
        if not text.startswith("ref: "):
            self.head_oid = _validate_object_id(text)
            return
        self.head_ref = text[5:]
        _safe_git_relative_path(os.fsencode(self.head_ref))
        if not self.head_ref.startswith("refs/"):
            raise RuntimeError("Git HEAD contains an unsafe reference")
        relative = self.head_ref[len("refs/"):]
        self.ref_parent_fd, self.ref_name = _open_workspace_parent(
            Path(os.path.sep), relative, root_fd=self.refs_fd
        )
        try:
            os.stat(self.ref_name, dir_fd=self.ref_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            loose_exists = False
        else:
            loose_exists = True
        if loose_exists:
            self.ref_fd, self.ref_info, ref_bytes = _open_retained_control_file(
                self.ref_parent_fd, self.ref_name, 4096
            )
            self.head_oid = _validate_object_id(os.fsdecode(ref_bytes).strip())
        else:
            self.ref_info = None
            self.packed_refs_fd, self.packed_refs_info, packed = _open_retained_control_file(
                self.common_fd, "packed-refs", 64 * 1024 * 1024
            )
            self.head_oid = _packed_ref_oid(packed, self.head_ref)

    def matches(self) -> bool:
        try:
            current = os.stat(".git", dir_fd=self.root_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_mode) != (
                self.dotgit.st_dev, self.dotgit.st_ino, self.dotgit.st_mode,
            ):
                return False
            return (
                _workspace_root_handle_matches(self.git_fd, self.git_path)
                and _workspace_root_handle_matches(self.common_fd, self.common_path)
                and self._refs_match()
                and self._directory_child_matches(self.common_fd, "objects", self.objects_fd)
                and self._file_matches(self.git_fd, "index", self.index_fd)
                and self._file_matches(self.git_fd, "HEAD", self.head_fd)
                and (self.config_fd < 0 or self._file_matches(self.common_fd, "config", self.config_fd))
                and self._head_storage_matches()
            )
        except OSError:
            return False

    def _refs_match(self) -> bool:
        current = os.stat("refs", dir_fd=self.common_fd, follow_symlinks=False)
        opened = os.fstat(self.refs_fd)
        return stat.S_ISDIR(current.st_mode) and (
            current.st_dev, current.st_ino
        ) == (opened.st_dev, opened.st_ino)

    @staticmethod
    def _directory_child_matches(parent_fd: int, name: str, opened_fd: int) -> bool:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(opened_fd)
        return stat.S_ISDIR(current.st_mode) and (
            current.st_dev, current.st_ino
        ) == (opened.st_dev, opened.st_ino)

    @staticmethod
    def _file_matches(parent_fd: int, name: str, opened_fd: int) -> bool:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(opened_fd)
        return stat.S_ISREG(current.st_mode) and (
            current.st_dev, current.st_ino, current.st_mode, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns,
        ) == (
            opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size,
            opened.st_mtime_ns, opened.st_ctime_ns,
        )

    def _ref_parent_matches(self) -> bool:
        if self.head_ref is None or self.ref_parent_fd < 0:
            return True
        current_fd = -1
        try:
            relative = self.head_ref[len("refs/"):]
            current_fd, current_name = _open_workspace_parent(
                Path(os.path.sep), relative, root_fd=self.refs_fd
            )
            expected = os.fstat(self.ref_parent_fd)
            current = os.fstat(current_fd)
            return current_name == self.ref_name and (
                current.st_dev, current.st_ino
            ) == (expected.st_dev, expected.st_ino)
        except (OSError, RuntimeError):
            return False
        finally:
            if current_fd >= 0:
                os.close(current_fd)

    def _head_storage_matches(self) -> bool:
        if self.head_ref is None:
            return True
        if not self._ref_parent_matches():
            return False
        if self.ref_info is not None:
            if self.ref_parent_fd < 0 or self.ref_name is None or self.ref_fd < 0:
                return False
            current = os.stat(self.ref_name, dir_fd=self.ref_parent_fd, follow_symlinks=False)
            opened = os.fstat(self.ref_fd)
            expected = self.ref_info
            identity = (
                current.st_dev, current.st_ino, current.st_mode, current.st_size,
                current.st_mtime_ns, current.st_ctime_ns,
            ) == (
                opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size,
                opened.st_mtime_ns, opened.st_ctime_ns,
            ) == (
                expected.st_dev, expected.st_ino, expected.st_mode, expected.st_size,
                expected.st_mtime_ns, expected.st_ctime_ns,
            )
            if not identity:
                return False
            return _validate_object_id(
                os.fsdecode(os.pread(self.ref_fd, 4096, 0)).strip()
            ) == self.head_oid
        return self.packed_refs_fd >= 0 and self._file_matches(
            self.common_fd, "packed-refs", self.packed_refs_fd
        )

    def close(self) -> None:
        for name in (
            "packed_refs_fd", "ref_fd", "ref_parent_fd", "head_fd", "config_fd", "index_fd",
            "objects_fd", "refs_fd", "common_fd", "git_fd",
        ):
            fd = getattr(self, name, -1)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)
        if self._git_tmp is not None:
            self._git_tmp.cleanup()
            self._git_tmp = None


@contextmanager
def _pinned_git_context(root: Path, root_fd: int) -> Iterator[_PinnedGitContext]:
    contexts = getattr(_PINNED_GIT, "contexts", None)
    if contexts is None:
        contexts = {}
        _PINNED_GIT.contexts = contexts
    existing = contexts.get(root_fd)
    if existing is not None:
        yield existing
        return
    context = _PinnedGitContext(root, root_fd)
    contexts[root_fd] = context
    try:
        yield context
        if not context.allow_final_mismatch and not context.matches():
            raise RuntimeError("Git control directory changed during trusted operation")
    finally:
        contexts.pop(root_fd, None)
        context.close()


def _current_git_context(root_fd: Optional[int]) -> Optional[_PinnedGitContext]:
    if root_fd is None:
        return None
    return getattr(_PINNED_GIT, "contexts", {}).get(root_fd)


def _pinned_config_command(
    context: _PinnedGitContext, *args: str,
) -> subprocess.CompletedProcess[str]:
    if context.config_fd < 0:
        return subprocess.CompletedProcess([], 1, "")
    env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        [
            _system_git(), "config", "--file", _workspace_fd_path(context.config_fd),
            "--no-includes", *args,
        ],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        check=False, env=env, pass_fds=(context.config_fd,),
    )


def _validate_object_id(value: str) -> str:
    if len(value) not in {40, 64} or not re.fullmatch(r"[0-9a-f]+", value):
        raise RuntimeError("Git reference contains an invalid object id")
    return value


def _pinned_ref_oid(context: _PinnedGitContext, ref: str) -> Optional[str]:
    if ref != context.head_ref:
        raise RuntimeError("only the pinned HEAD branch may be accessed")
    return context.head_oid


def _pinned_head(context: _PinnedGitContext) -> tuple[str, Optional[str]]:
    return context.head_oid, context.head_ref


def _pinned_head_oid(root_fd: int) -> str:
    context = _current_git_context(root_fd)
    if context is None:
        raise RuntimeError("Git control context is not pinned")
    return _pinned_head(context)[0]


def _pinned_replacement_refs(context: _PinnedGitContext) -> bool:
    try:
        replace_fd = os.open(
            "replace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=context.refs_fd,
        )
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError("could not inspect replacement refs safely") from exc
    try:
        return bool(os.listdir(replace_fd))
    finally:
        os.close(replace_fd)


def _snapshot_git_run(
    root: Path, *args: str, root_fd: Optional[int] = None,
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("GIT_CONFIG_") or key in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_ASKPASS", "GIT_CEILING_DIRECTORIES",
            "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_DIR", "GIT_GRAFT_FILE",
            "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF", "GIT_INDEX_FILE",
            "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY", "GIT_REPLACE_REF_BASE",
            "GIT_SHALLOW_FILE", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_WORK_TREE",
            "SSH_ASKPASS",
        }:
            env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["LC_ALL"] = "C"
    cwd = str(root) if root_fd is None else _workspace_fd_path(root_fd)
    pass_fds: tuple[int, ...] = () if root_fd is None else (root_fd,)
    git_context = _current_git_context(root_fd)
    if git_context is not None:
        env["GIT_DIR"] = git_context.shim_git_dir
        env["GIT_COMMON_DIR"] = git_context.shim_git_dir
        env["GIT_WORK_TREE"] = cwd
        env["GIT_OBJECT_DIRECTORY"] = _workspace_fd_path(git_context.objects_fd)
        env.setdefault("GIT_INDEX_FILE", _workspace_fd_path(git_context.index_fd))
        pass_fds = tuple(dict.fromkeys((
            *pass_fds, git_context.objects_fd, git_context.index_fd,
        )))
    return subprocess.run(
        [_system_git(), "-c", "core.fsmonitor=false", "-C", cwd, *args],
        capture_output=True,
        check=False,
        env=env,
        pass_fds=pass_fds,
    )


def _snapshot_path_fingerprint(
    path: Path, *, hash_content: bool = True, stable_metadata: bool = True
) -> tuple[str, int]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return hashlib.sha256(b"missing").hexdigest(), 0
    except OSError as exc:
        raise RuntimeError(f"could not inspect snapshot path: {path}") from exc
    material = [str(info.st_mode), str(info.st_size)]
    if stable_metadata:
        material.extend(
            (str(info.st_mtime_ns), str(info.st_ctime_ns), str(info.st_dev), str(info.st_ino))
        )
    consumed = 0
    if stat.S_ISLNK(info.st_mode):
        material.extend(("symlink", os.readlink(path)))
    elif stat.S_ISREG(info.st_mode):
        material.append("file")
        if hash_content:
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                        consumed += len(chunk)
            except OSError as exc:
                raise RuntimeError(f"could not hash snapshot path: {path}") from exc
            material.append(digest.hexdigest())
    elif stat.S_ISDIR(info.st_mode):
        material.append("directory")
    else:
        material.append("other")
    return hashlib.sha256("\0".join(material).encode("utf-8", "surrogateescape")).hexdigest(), consumed


def _control_entry_fingerprint(
    kind: str, info: os.stat_result, content: bytes = b"",
) -> tuple[str, int]:
    material = [str(info.st_mode), str(info.st_size), kind]
    consumed = 0
    if kind in {"file", "symlink"}:
        material.append(hashlib.sha256(content).hexdigest())
        consumed = len(content) if kind == "file" else 0
    return (
        hashlib.sha256("\0".join(material).encode("utf-8", "surrogateescape")).hexdigest(),
        consumed,
    )


def _control_directory_manifest(
    label: str, directory_fd: int, *, pinned_refs_fd: Optional[int] = None,
) -> tuple[list[tuple[str, str]], int]:
    manifest: list[tuple[str, str]] = []
    consumed = 0

    def add_regular(parent_fd: int, name: str, relative: str, *, required: bool) -> None:
        nonlocal consumed
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if required:
                manifest.append((f"{label}/{relative}", hashlib.sha256(b"missing").hexdigest()))
            return
        if stat.S_ISLNK(before.st_mode):
            target = os.fsencode(os.readlink(name, dir_fd=parent_fd))
            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_mode) != (
                after.st_dev, after.st_ino, after.st_mode,
            ):
                raise RuntimeError("Git control path changed during snapshot")
            kind, info, content = "symlink", after, target
        elif stat.S_ISREG(before.st_mode):
            content = _read_control_file(parent_fd, name, _MAX_CONTROL_BYTES - consumed)
            if content is None:
                raise RuntimeError("Git control path disappeared during snapshot")
            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_mode) != (
                after.st_dev, after.st_ino, after.st_mode,
            ):
                raise RuntimeError("Git control path changed during snapshot")
            kind, info = "file", after
        else:
            raise RuntimeError("Git control path has an unsupported type")
        fingerprint, size = _control_entry_fingerprint(kind, info, content)
        consumed += size
        if consumed > _MAX_CONTROL_BYTES:
            raise RuntimeError("Git control snapshot exceeds the bounded byte limit")
        manifest.append((f"{label}/{relative}", fingerprint))

    def walk(parent_fd: int, prefix: str) -> None:
        nonlocal consumed
        try:
            names = sorted(os.listdir(parent_fd), key=os.fsencode)
        except OSError as exc:
            raise RuntimeError("could not enumerate Git control directory") from exc
        for name in names:
            if not name or name in {".", ".."} or "/" in name:
                raise RuntimeError("unsafe Git control directory entry")
            relative = f"{prefix}/{name}" if prefix else name
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError("Git control directory changed during snapshot") from exc
            if stat.S_ISDIR(info.st_mode):
                try:
                    child_fd = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                    )
                except OSError as exc:
                    raise RuntimeError("Git control directory changed during snapshot") from exc
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise RuntimeError("Git control directory changed during snapshot")
                    fingerprint, _size = _control_entry_fingerprint("directory", opened)
                    manifest.append((f"{label}/{relative}", fingerprint))
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            else:
                add_regular(parent_fd, name, relative, required=False)
            if len(manifest) > _MAX_CONTROL_FILES:
                raise RuntimeError("Git control snapshot exceeds the bounded file limit")

    for name in sorted(_GIT_CONTROL_FILES):
        add_regular(directory_fd, name, name, required=True)
    for dirname in sorted(_GIT_CONTROL_DIRS):
        if dirname == "refs" and pinned_refs_fd is not None:
            child_fd = os.dup(pinned_refs_fd)
            try:
                opened = os.fstat(child_fd)
                fingerprint, _size = _control_entry_fingerprint("directory", opened)
                manifest.append((f"{label}/{dirname}", fingerprint))
                walk(child_fd, dirname)
            finally:
                os.close(child_fd)
            continue
        try:
            info = os.stat(dirname, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            add_regular(directory_fd, dirname, dirname, required=False)
            continue
        child_fd = os.open(
            dirname, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        try:
            opened = os.fstat(child_fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError("Git control directory changed during snapshot")
            fingerprint, _size = _control_entry_fingerprint("directory", opened)
            manifest.append((f"{label}/{dirname}", fingerprint))
            walk(child_fd, dirname)
        finally:
            os.close(child_fd)
    return manifest, consumed


def _git_control_snapshot(root: Path, *, root_fd: Optional[int] = None) -> tuple[str, str]:
    if root_fd is None:
        opened_root = _open_workspace_root(root)
        try:
            with _pinned_git_context(root, opened_root):
                return _git_control_snapshot(root, root_fd=opened_root)
        finally:
            os.close(opened_root)
    assert root_fd is not None
    if _current_git_context(root_fd) is None:
        with _pinned_git_context(root, root_fd):
            return _git_control_snapshot(root, root_fd=root_fd)
    context = _current_git_context(root_fd)
    assert context is not None
    locations = [("git", context.git_fd)]
    if os.fstat(context.git_fd).st_ino != os.fstat(context.common_fd).st_ino or os.fstat(
        context.git_fd
    ).st_dev != os.fstat(context.common_fd).st_dev:
        locations.append(("common", context.common_fd))
    manifest: list[tuple[str, str]] = []
    consumed = 0
    common_identity = (os.fstat(context.common_fd).st_dev, os.fstat(context.common_fd).st_ino)
    for label, directory_fd in locations:
        directory_identity = (os.fstat(directory_fd).st_dev, os.fstat(directory_fd).st_ino)
        entries, size = _control_directory_manifest(
            label, directory_fd,
            pinned_refs_fd=context.refs_fd if directory_identity == common_identity else None,
        )
        manifest.extend(entries)
        consumed += size
        if consumed > _MAX_CONTROL_BYTES or len(manifest) > _MAX_CONTROL_FILES:
            raise RuntimeError("Git control snapshot exceeds its bounded limits")
    index_entry = _read_workspace_entry(
        Path(os.path.sep), "index", content_limit=_MAX_CONTROL_BYTES,
        root_fd=context.git_fd,
    )
    if index_entry is None:
        index_fingerprint = hashlib.sha256(b"missing").hexdigest()
    else:
        index_fingerprint, _size = _control_entry_fingerprint(*index_entry)
    control = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return index_fingerprint, control


def _ignored_snapshot(root: Path, *, root_fd: Optional[int] = None) -> str:
    if root_fd is None:
        opened_root = _open_workspace_root(root)
        try:
            with _pinned_git_context(root, opened_root):
                result = _ignored_snapshot(root, root_fd=opened_root)
                if not _workspace_root_handle_matches(opened_root, root):
                    raise RuntimeError("workspace root changed during ignored snapshot")
                return result
        finally:
            os.close(opened_root)
    result = _snapshot_git_run(
        root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--full-name",
        root_fd=root_fd,
    )
    if result.returncode != 0:
        raise RuntimeError("could not enumerate ignored workspace paths")
    raw_paths = [value for value in result.stdout.split(b"\0") if value]
    if len(raw_paths) > _MAX_IGNORED_PATHS or sum(map(len, raw_paths)) > _MAX_IGNORED_PATH_BYTES:
        raise RuntimeError("ignored workspace snapshot exceeds its bounded path limit")
    manifest: list[tuple[str, str]] = []
    hashed_bytes = 0
    for raw in sorted(raw_paths):
        relative = _safe_git_relative_path(raw)
        remaining = max(0, _MAX_IGNORED_HASH_BYTES - hashed_bytes)
        entry = _read_workspace_entry(
            root, relative, content_limit=remaining, root_fd=root_fd,
            metadata_only_if_oversized=True,
        )
        if entry is None:
            raise RuntimeError("ignored workspace changed during snapshot")
        kind, info, content = entry
        material = [
            str(info.st_mode), str(info.st_size), str(info.st_mtime_ns),
            str(info.st_ctime_ns), str(info.st_dev), str(info.st_ino), kind,
        ]
        if kind == "symlink":
            material.append(os.fsdecode(content))
        elif info.st_size <= remaining and len(content) == info.st_size:
            material.append(hashlib.sha256(content).hexdigest())
            hashed_bytes += info.st_size
        fingerprint = hashlib.sha256(
            "\0".join(material).encode("utf-8", "surrogateescape")
        ).hexdigest()
        manifest.append((relative, fingerprint))
    return hashlib.sha256(
        json.dumps(manifest, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _safe_git_relative_path(raw_path: bytes) -> str:
    path = raw_path.decode("utf-8", errors="surrogateescape")
    if (
        not path
        or path.startswith(("/", "\\"))
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise RuntimeError("Git returned an unsafe workspace path")
    return path


def _safe_workspace_candidate(root: Path, path: str) -> Path:
    normalized = _safe_git_relative_path(os.fsencode(path))
    candidate = root / normalized
    resolved_parent = candidate.parent.resolve(strict=False)
    if resolved_parent != root and root not in resolved_parent.parents:
        raise RuntimeError("workspace path escapes the canonical root")
    return candidate


def _open_workspace_root(root: Path) -> int:
    """Open an absolute workspace through a no-follow component walk."""
    if not root.is_absolute() or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("secure descriptor-relative workspace access is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parts = root.parts
    if not parts or parts[0] != os.path.sep:
        raise RuntimeError("secure workspace roots require an absolute POSIX path")
    current = os.open(os.path.sep, flags)
    try:
        for component in parts[1:]:
            child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except Exception:
        os.close(current)
        raise


def _workspace_root_handle_matches(root_fd: int, root: Path) -> bool:
    current_fd = -1
    try:
        opened = os.fstat(root_fd)
        current_fd = _open_workspace_root(root)
        current = os.fstat(current_fd)
        return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
    except OSError:
        return False
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _open_workspace_parent(
    root: Path, path: str, *, root_fd: Optional[int] = None,
) -> tuple[int, str]:
    """Open a stable no-follow descriptor chain to a workspace path's parent."""
    normalized = _safe_git_relative_path(os.fsencode(path))
    components = normalized.split("/")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.dup(root_fd) if root_fd is not None else _open_workspace_root(root)
    try:
        for component in components[:-1]:
            child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
        return current, components[-1]
    except Exception:
        os.close(current)
        raise


def _read_workspace_entry(
    root: Path, path: str, *, content_limit: Optional[int] = None,
    root_fd: Optional[int] = None, metadata_only_if_oversized: bool = False,
) -> tuple[str, os.stat_result, bytes] | None:
    """Read one final component from a stable parent fd without following symlinks."""
    for _attempt in range(3):
        try:
            parent_fd, name = _open_workspace_parent(root, path, root_fd=root_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeError("could not securely open workspace parent") from exc
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            try:
                file_fd = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno != errno.ELOOP:
                    raise RuntimeError("could not securely open workspace path") from exc
                before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISLNK(before.st_mode):
                    continue
                target = os.readlink(name, dir_fd=parent_fd)
                after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (before.st_dev, before.st_ino, before.st_mode) != (
                    after.st_dev, after.st_ino, after.st_mode,
                ):
                    continue
                return "symlink", before, os.fsencode(target)
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeError("workspace path is not a regular file or symlink")
                if content_limit is not None and info.st_size > content_limit:
                    if metadata_only_if_oversized:
                        return "file", info, b""
                    raise RuntimeError("workspace file exceeds its bounded read limit")
                chunks: list[bytes] = []
                consumed = 0
                while True:
                    read_size = 1024 * 1024
                    if content_limit is not None:
                        read_size = min(read_size, content_limit - consumed + 1)
                    chunk = os.read(file_fd, max(1, read_size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    consumed += len(chunk)
                    if content_limit is not None and consumed > content_limit:
                        raise RuntimeError("workspace file exceeds its bounded read limit")
                return "file", os.fstat(file_fd), b"".join(chunks)
            finally:
                os.close(file_fd)
        finally:
            os.close(parent_fd)
    raise RuntimeError("workspace path changed during secure read")


def _fixed_runtime_version(executable: str, root_fd: int) -> str:
    path = Path(executable)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"required project runtime is unavailable: {executable}")
    result = subprocess.run(
        [executable, "--version"],
        cwd=f"/proc/self/fd/{root_fd}",
        pass_fds=(root_fd,),
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    value = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not value:
        raise ValueError(f"could not identify project runtime: {executable}")
    return value[0][:200]


def _tracked_manifest_bytes(
    root: Path, path: str, *, content_limit: int, root_fd: int,
) -> Optional[bytes]:
    head = _pinned_head_oid(root_fd)
    tree = _snapshot_git_run(
        root, "ls-tree", "-z", "--full-tree", head, "--", path, root_fd=root_fd
    )
    object_format = _snapshot_git_run(
        root, "rev-parse", "--show-object-format", root_fd=root_fd
    )
    if tree.returncode != 0 or object_format.returncode != 0:
        raise ValueError(f"could not authenticate discovery manifest: {path}")
    entries = _parse_tree_entries(tree.stdout)
    expected = entries.get(path)
    if expected is None:
        return None
    if set(entries) != {path} or expected[0] not in {"100644", "100755"}:
        raise ValueError(f"discovery manifest must be a tracked regular file: {path}")
    algorithm = os.fsdecode(object_format.stdout).strip()
    if algorithm not in {"sha1", "sha256"}:
        raise ValueError("unsupported Git object format during project discovery")
    actual = _raw_worktree_entry(root, path, algorithm, root_fd=root_fd)
    if actual != expected:
        raise ValueError(f"discovery manifest differs from authenticated HEAD: {path}")
    entry = _read_workspace_entry(root, path, content_limit=content_limit, root_fd=root_fd)
    if entry is None or entry[0] != "file":
        raise ValueError(f"discovery manifest is not a regular file: {path}")
    kind, info, content = entry
    mode = "100755" if info.st_mode & 0o111 else "100644"
    digest = hashlib.new(algorithm)
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    if kind != "file" or (mode, digest.hexdigest()) != expected:
        raise ValueError(f"discovery manifest changed during authenticated read: {path}")
    return content


def _discover_project_contract(root: Path, root_fd: int) -> dict[str, Any]:
    package_bytes = _tracked_manifest_bytes(
        root, "package.json", content_limit=1024 * 1024, root_fd=root_fd
    )
    if package_bytes is not None:
        try:
            package = json.loads(package_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("package.json is not valid bounded UTF-8 JSON") from exc
        if not isinstance(package, dict):
            raise ValueError("package.json must contain a JSON object")
        manifests = ["package.json"]
        lock_bytes = _tracked_manifest_bytes(
            root, "package-lock.json", content_limit=16 * 1024 * 1024, root_fd=root_fd
        )
        build_candidates: list[str] = []
        if lock_bytes is not None:
            manifests.append("package-lock.json")
            build_candidates.append("PATH=/usr/bin:/bin /usr/bin/npm ci")
        scripts_raw = package.get("scripts")
        scripts = scripts_raw if isinstance(scripts_raw, dict) else {}
        test_script = scripts.get("test")
        test_candidates = (
            ["PATH=/usr/bin:/bin /usr/bin/npm test"]
            if isinstance(test_script, str) and test_script.strip()
            else []
        )
        if not build_candidates and not test_candidates:
            raise ValueError("Node project has no package-lock.json or package.json test script")
        return {
            "project_kind": "node",
            "languages": ["JavaScript"],
            "manifests": manifests,
            "build_candidates": build_candidates,
            "test_candidates": test_candidates,
            "repair_candidates": [0, 1, 2, 3],
            "runtime_evidence": [
                "/usr/bin/node --version => " + _fixed_runtime_version("/usr/bin/node", root_fd),
                "/usr/bin/npm --version => " + _fixed_runtime_version("/usr/bin/npm", root_fd),
            ],
        }
    raise ValueError(
        "automatic project discovery currently requires a tracked package.json; "
        "provide explicit fixed build/test commands for another project type"
    )


def _parse_tree_entries(raw: bytes) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for record in (value for value in raw.split(b"\0") if value):
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, kind, oid = header.split(b" ", 2)
        except ValueError as exc:
            raise RuntimeError("Git returned a malformed tree entry") from exc
        if kind != b"blob":
            raise RuntimeError("trusted workspace snapshots do not support Git submodules")
        path = _safe_git_relative_path(raw_path)
        if path in entries:
            raise RuntimeError("Git returned a duplicate tree path")
        entries[path] = (mode.decode("ascii"), oid.decode("ascii"))
    return entries


def _parse_index_entries(raw: bytes) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for record in (value for value in raw.split(b"\0") if value):
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, oid, stage = header.split(b" ", 2)
        except ValueError as exc:
            raise RuntimeError("Git returned a malformed index entry") from exc
        if stage != b"0":
            raise RuntimeError("trusted workspace snapshots do not support conflicted indexes")
        if mode == b"160000":
            raise RuntimeError("trusted workspace snapshots do not support Git submodules")
        path = _safe_git_relative_path(raw_path)
        if path in entries:
            raise RuntimeError("Git returned a duplicate index path")
        entries[path] = (mode.decode("ascii"), oid.decode("ascii"))
    return entries


def _raw_worktree_entry(
    root: Path, path: str, object_format: str, *, root_fd: Optional[int] = None,
) -> tuple[str, str] | None:
    entry = _read_workspace_entry(root, path, root_fd=root_fd)
    if entry is None:
        return None
    kind, info, content = entry
    if kind == "symlink":
        mode = "120000"
    else:
        mode = "100755" if info.st_mode & 0o111 else "100644"
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return mode, digest.hexdigest()


def _raw_untracked_paths(root: Path, *, root_fd: Optional[int] = None) -> set[str]:
    result = _snapshot_git_run(
        root, "ls-files", "--others", "--exclude-standard", "-z", root_fd=root_fd
    )
    if result.returncode != 0:
        raise RuntimeError("could not enumerate raw untracked workspace state")
    raw_paths = [value for value in result.stdout.split(b"\0") if value]
    if len(raw_paths) > _MAX_IGNORED_PATHS or sum(map(len, raw_paths)) > _MAX_IGNORED_PATH_BYTES:
        raise RuntimeError("untracked workspace snapshot exceeds its bounded path limit")
    return {_safe_git_relative_path(raw_path) for raw_path in raw_paths}


def _raw_dirty_paths(root: Path, *, root_fd: Optional[int] = None) -> set[str]:
    """Find tracked changes without invoking attributes, filters, or diff drivers."""
    if root_fd is None:
        opened_root = _open_workspace_root(root)
        try:
            with _pinned_git_context(root, opened_root):
                result = _raw_dirty_paths(root, root_fd=opened_root)
                if not _workspace_root_handle_matches(opened_root, root):
                    raise RuntimeError("workspace root changed during raw snapshot")
                return result
        finally:
            os.close(opened_root)
    assert root_fd is not None
    head_oid = _pinned_head_oid(root_fd)
    tree = _snapshot_git_run(
        root, "ls-tree", "-r", "-z", "--full-tree", head_oid, root_fd=root_fd
    )
    index = _snapshot_git_run(root, "ls-files", "--stage", "-z", root_fd=root_fd)
    object_format = _snapshot_git_run(
        root, "rev-parse", "--show-object-format", root_fd=root_fd
    )
    if tree.returncode != 0 or index.returncode != 0 or object_format.returncode != 0:
        raise RuntimeError("could not enumerate raw trusted workspace state")
    algorithm = os.fsdecode(object_format.stdout).strip()
    if algorithm not in {"sha1", "sha256"}:
        raise RuntimeError("unsupported Git object format")
    head_entries = _parse_tree_entries(tree.stdout)
    index_entries = _parse_index_entries(index.stdout)
    paths = set(head_entries) | set(index_entries)
    if len(paths) > _MAX_IGNORED_PATHS or sum(len(os.fsencode(path)) for path in paths) > _MAX_IGNORED_PATH_BYTES:
        raise RuntimeError("tracked workspace snapshot exceeds its bounded path limit")
    dirty: set[str] = set()
    for path in paths:
        staged = index_entries.get(path)
        if staged != head_entries.get(path) or _raw_worktree_entry(
            root, path, algorithm, root_fd=root_fd
        ) != staged:
            dirty.add(path)
    return dirty


def _git_state_observation(
    workspace: str, relevant_files: Optional[list[str]] = None
) -> dict[str, Any]:
    """Observe HEAD and direct fingerprints for every dirty and explicitly allowed path."""
    root = Path(workspace)
    root_fd = _open_workspace_root(root)
    try:
        with _pinned_git_context(root, root_fd):
            result = _git_state_observation_with_root(root, relevant_files, root_fd)
            if not _workspace_root_handle_matches(root_fd, root):
                raise RuntimeError("workspace root changed during trusted snapshot")
            return result
    finally:
        os.close(root_fd)


def _git_state_observation_with_root(
    root: Path, relevant_files: Optional[list[str]], root_fd: int,
) -> dict[str, Any]:
    relevant_files = relevant_files or []
    relevant_error = (
        _relevant_files_error(root, relevant_files, root_fd=root_fd)
        if relevant_files else None
    )
    if relevant_error:
        raise RuntimeError(f"could not snapshot unsafe relevant_files: {relevant_error}")
    top = _snapshot_git_run(root, "rev-parse", "--show-toplevel", root_fd=root_fd)
    head = _pinned_head_oid(root_fd)
    tracked_paths = _raw_dirty_paths(root, root_fd=root_fd)
    untracked_paths = _raw_untracked_paths(root, root_fd=root_fd)
    index_flags = _snapshot_git_run(root, "ls-files", "-v", "-z", root_fd=root_fd)
    context = _current_git_context(root_fd)
    assert context is not None
    if (
        top.returncode != 0
        or Path(os.fsdecode(top.stdout).strip()).resolve() != root
        or index_flags.returncode != 0
    ):
        raise RuntimeError("could not snapshot trusted executor workspace")
    if _pinned_replacement_refs(context):
        raise RuntimeError("repository replacement refs are not allowed")
    graft_parent, graft_name = _open_workspace_parent(
        Path(os.path.sep), "info/grafts", root_fd=context.common_fd
    )
    try:
        try:
            os.stat(graft_name, dir_fd=graft_parent, follow_symlinks=False)
            graft_exists = True
        except FileNotFoundError:
            graft_exists = False
    finally:
        os.close(graft_parent)
    if graft_exists:
        raise RuntimeError("repository grafts are not allowed")
    unsafe_flags = [
        os.fsdecode(entry)
        for entry in index_flags.stdout.split(b"\0")
        if entry and not entry.startswith(b"H ")
    ]
    if unsafe_flags:
        raise RuntimeError(
            "repository uses hidden or unsupported index flags: "
            + ", ".join(unsafe_flags[:10])
        )
    paths = sorted(
        set(relevant_files)
        | tracked_paths
        | untracked_paths
    )
    fingerprints: dict[str, str] = {}
    for path in paths:
        index = _snapshot_git_run(
            root, "ls-files", "--stage", "-z", "--", f":(literal){path}",
            root_fd=root_fd,
        )
        entry = _read_workspace_entry(root, path, root_fd=root_fd)
        if entry is None:
            mode = b"missing"
            kind = b"missing"
            content = b""
        else:
            entry_kind, info, raw_content = entry
            mode = str(info.st_mode).encode("ascii")
            kind = entry_kind.encode("ascii")
            content = (
                hashlib.sha256(raw_content).digest()
                if entry_kind == "file"
                else raw_content
            )
        if index.returncode != 0:
            raise RuntimeError("could not fingerprint trusted workspace content")
        material = b"\0".join(
            (
                b"index",
                str(index.returncode).encode("ascii"),
                index.stdout,
                index.stderr,
                b"type",
                kind,
                b"mode",
                mode,
                b"content",
                content,
            )
        )
        fingerprints[path] = hashlib.sha256(material).hexdigest()
    ignored = _ignored_snapshot(root, root_fd=root_fd)
    # Capture repository control last: some read-only Git commands refresh index
    # stat data once. Two observations must compare the post-command index.
    index, control = _git_control_snapshot(root, root_fd=root_fd)
    return {
        "version": _SNAPSHOT_VERSION,
        "head": head,
        "index": index,
        "control": control,
        "ignored": ignored,
        "files": fingerprints,
    }


def _execution_git_state(
    workspace: str, relevant_files: Optional[list[str]] = None
) -> dict[str, Any]:
    """Require two identical full observations before trusting repository state."""
    root = Path(workspace)
    root_fd = _open_workspace_root(root)
    try:
        with _pinned_git_context(root, root_fd):
            state = _execution_git_state_with_root(root, relevant_files, root_fd)
            if not _workspace_root_handle_matches(root_fd, root):
                raise RuntimeError("workspace root changed during trusted snapshot")
            return state
    finally:
        os.close(root_fd)


def _execution_git_state_with_root(
    root: Path, relevant_files: Optional[list[str]], root_fd: int,
) -> dict[str, Any]:
    first = _git_state_observation_with_root(root, relevant_files, root_fd)
    second = _git_state_observation_with_root(root, relevant_files, root_fd)
    if first != second:
        raise RuntimeError("could not obtain a stable trusted workspace snapshot")
    return second


def _scope_snapshot_body(
    c: dict[str, Any], item: dict[str, Any], snapshot: Optional[dict[str, Any]] = None
) -> str:
    encoded = json.dumps(
        snapshot or _execution_git_state(str(c["workspace"]), item["relevant_files"]),
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"\nTRUSTED_SCOPE_SNAPSHOT: {encoded}\n"


def _read_only_snapshot_body(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return f"\nTRUSTED_READ_ONLY_SNAPSHOT: {encoded}\n"


def _read_only_snapshot_from_body(task_body: str) -> Optional[dict[str, Any]]:
    marker = "TRUSTED_READ_ONLY_SNAPSHOT: "
    lines = [line.removeprefix(marker) for line in task_body.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        return None
    try:
        snapshot = json.loads(lines[0])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "version", "head", "index", "control", "ignored", "files"
    }:
        return None
    if (
        snapshot.get("version") != _SNAPSHOT_VERSION
        or not isinstance(snapshot.get("files"), dict)
        or any(
            not isinstance(snapshot.get(key), str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", snapshot[key])
            for key in ("head", "index", "control", "ignored")
        )
        or any(
            not isinstance(path, str)
            or not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
            for path, value in snapshot["files"].items()
        )
    ):
        return None
    return snapshot


def _read_only_snapshot_unchanged(c: dict[str, Any], task_body: str) -> bool:
    expected = _read_only_snapshot_from_body(task_body)
    if expected is None:
        return False
    try:
        return expected == _execution_git_state(str(c["workspace"]))
    except RuntimeError:
        return False


def _trusted_scope_body(
    c: dict[str, Any], item: dict[str, Any], correction: str
) -> str:
    contract = json.dumps(
        {
            "allowed_files": item["relevant_files"],
            "commands": _campaign_commands(c),
            "correction": correction,
            "scope": item,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"TRUSTED_QUALITY_SCOPE: {contract}\n"


def _trusted_scope_from_body(task_body: str) -> Optional[dict[str, Any]]:
    marker = "TRUSTED_QUALITY_SCOPE: "
    lines = [line.removeprefix(marker) for line in task_body.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        return None
    try:
        contract = json.loads(lines[0])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(contract, dict) or set(contract) != {
        "allowed_files", "commands", "correction", "scope"
    }:
        return None
    if not isinstance(contract.get("scope"), dict) or not isinstance(contract.get("correction"), str):
        return None
    return contract


def _verified_scope_snapshot(
    c: dict[str, Any], task_body: str
) -> Optional[dict[str, Any]]:
    marker = "TRUSTED_SCOPE_SNAPSHOT: "
    lines = [line.removeprefix(marker) for line in task_body.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        return None
    try:
        expected = json.loads(lines[0])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(expected, dict) or set(expected) != {
        "version", "head", "index", "control", "ignored", "files"
    }:
        return None
    trusted_scope = _trusted_scope_from_body(task_body)
    allowed_files = trusted_scope.get("allowed_files") if trusted_scope else None
    if not isinstance(allowed_files, list):
        return None
    try:
        current = _execution_git_state(str(c["workspace"]), allowed_files)
    except RuntimeError:
        return None
    return current if expected == current else None


def _scope_snapshot_unchanged(c: dict[str, Any], task_body: str) -> bool:
    return _verified_scope_snapshot(c, task_body) is not None


def _execute_item_body(c: dict[str, Any], item: dict[str, Any]) -> str:
    criteria = "\n".join(f"- {value}" for value in item["acceptance_criteria"])
    files = "\n".join(f"- {value}" for value in item.get("relevant_files", [])) or "- Determine the minimum files needed for this item."
    risks = "\n".join(f"- {value}" for value in item.get("risks", [])) or "- None supplied by the examiner."
    commands = "\n".join(f"- `{value}`" for value in _item_commands(c, item))
    exclusions = "\n".join(f"- {value}" for value in item.get("excluded_scope", [])) or "- Do not change any unlisted component or behavior."
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

EXACT VERIFICATION COMMANDS (run these literal foreground commands, then stop):
{commands}

Excluded scope:
{exclusions}

Known risks:
{risks}

Do not implement any other findings, recommendations, or lower-priority items from the parent
examination. They are context only and will be reconsidered after this item is independently
validated. Do not broaden the task while working.
"""


def _execution_contract_body(
    c: dict[str, Any],
    item: dict[str, Any],
    snapshot: Optional[dict[str, Any]] = None,
) -> str:
    snapshot = snapshot or _execution_git_state(
        str(c["workspace"]), item["relevant_files"]
    )
    contract = json.dumps(
        {
            "allowed_files": item["relevant_files"],
            "baseline": snapshot["files"],
            "repository": {
                key: snapshot[key]
                for key in ("version", "head", "index", "control", "ignored")
            },
            "commands": _campaign_commands(c),
            "pre_execution_head": snapshot["head"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"\nTRUSTED_EXECUTION_CONTRACT: {contract}\n"


def _examine_contract_body(c: dict[str, Any]) -> str:
    contract = json.dumps(
        {
            "commands": _campaign_commands(c),
            "score_required": c.get("target_average") is not None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"\nTRUSTED_EXAMINE_CONTRACT: {contract}\n"


def _discovery_contract_body(c: dict[str, Any]) -> str:
    value = c.get("discovery_contract")
    try:
        contract = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise RuntimeError("stored project discovery contract is malformed") from exc
    if not isinstance(contract, dict):
        raise RuntimeError("project discovery card has no trusted contract")
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return f"\nTRUSTED_DISCOVERY_CONTRACT: {encoded}\n"


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
    return stored if isinstance(stored, dict) else None


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
    execution_snapshot: Optional[dict[str, Any]] = None,
) -> str:
    read_only_snapshot = (
        _execution_git_state(str(c["workspace"]))
        if stage in {
            "discover", "examine", "scope_validate", "plan", "validate",
            "integrate_validate", "final_validate",
        }
        else None
    )
    model_key = {
        "discover": "examiner_model",
        "examine": "examiner_model",
        "scope_validate": "validator_model",
        "plan": "examiner_model",
        "execute": "executor_model",
        "validate": "validator_model",
        "integrate_validate": "validator_model",
        "final_validate": "validator_model",
    }[stage]
    label = {
        "discover": "Discover project configuration",
        "examine": "Examine current codebase and select one defect",
        "scope_validate": "Convert selected defect into bounded scope",
        "plan": "Plan bounded execution slices if required",
        "execute": "Execute one bounded implementation slice",
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
    elif stage in {"scope_validate", "execute"} and c.get("repair_no", 0):
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
        return _require_controller_task_api()(
            conn,
            controller=PLUGIN_ID,
            title=f"[{c['name']}] {label} ({suffix})",
            body=(
                _task_body(c, stage)
                + (_read_only_snapshot_body(read_only_snapshot) if read_only_snapshot else "")
                + (_discovery_contract_body(c) if stage == "discover" else "")
                + (_examine_contract_body(c) if stage == "examine" else "")
                + (_scope_input_body(c, improvement, correction) if stage == "scope_validate" and improvement else "")
                + (_planning_item_body(c, improvement) if stage == "plan" and improvement else "")
                + (
                    _execution_contract_body(c, improvement, execution_snapshot)
                    if stage == "execute" and improvement
                    else ""
                )
                + (_execute_item_body(c, improvement) if stage == "execute" and improvement else "")
                + (_repair_body(correction) if stage == "execute" and correction else "")
                + (_validation_item_body(improvement) if stage == "validate" and improvement else "")
                + (_integration_validation_body(improvement) if integration_validation and improvement else "")
            ),
            assignee=c["assignee"],
            workspace_kind="dir",
            workspace_path=c["workspace"],
            tenant=c["id"],
            priority=10,
            parents=parents,
            idempotency_key=key,
            max_runtime_seconds=1200 if stage == "examine" else 7200,
            skills=[],
            max_retries=1 if stage == "execute" else 2,
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

    if expected_role in {"examine", "scope_validate", "plan"}:
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


def _handoff(
    run: Any,
    *,
    expected_role: str | None = None,
    allow_pre_hardening_migration: bool = False,
) -> Optional[dict[str, Any]]:
    """Read the namespaced persisted handoff.

    Legacy prose, bare JSON, top-level metadata, and comment recovery are isolated behind an
    explicit migration-only flag. Hardened v2 cards never set that flag and therefore cannot
    advance from inferred or legacy payloads.
    """
    if not run:
        return None
    meta = run.metadata if isinstance(run.metadata, dict) else None
    if meta:
        candidate = meta.get("quality_loop")
        if isinstance(candidate, dict):
            return candidate
    if not allow_pre_hardening_migration:
        return None
    if meta:
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


def _trusted_card_error(c: dict[str, Any], task: Any, stage: str) -> Optional[str]:
    """Authenticate a generated v2 card independently of the completion tool."""
    if getattr(task, "created_by", None) != "quality-loop":
        return "active card was not created by the Quality Loop controller"
    body = str(getattr(task, "body", "") or "")
    board_conn = kbc.connect(board=c["board"])
    try:
        events = kb.list_events(board_conn, task.id)
        created = [event for event in events if event.kind == "created"]
        provenance = [
            event for event in events
            if event.kind == "controller_provenance"
        ]
    finally:
        board_conn.close()
    expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    legacy_match = (
        len(created) == 1
        and isinstance(created[0].payload, dict)
        and created[0].payload.get("body_sha256") == expected_hash
    )
    controller_match = (
        len(provenance) == 1
        and provenance[0].payload == {
            "schema": "hermes/controller-task/v1",
            "controller": PLUGIN_ID,
            "body_sha256": expected_hash,
        }
    )
    if not legacy_match and not controller_match:
        return "active card body does not match immutable controller creation provenance"
    lines = body.splitlines()
    expected_header = f"QUALITY LOOP CAMPAIGN: {c['id']}"
    expected_role = f"ROLE: {stage.upper()}"
    campaign_lines = [line for line in lines if line.startswith("QUALITY LOOP CAMPAIGN:")]
    role_lines = [line for line in lines if line.startswith("ROLE:")]
    if campaign_lines != [expected_header] or role_lines != [expected_role]:
        return "active card has malformed or role-mismatched anchored Quality Loop headers"
    marker = "TRUSTED_QUALITY_LOOP_CARD: " + json.dumps(
        {"schema": CARD_SCHEMA, "campaign_id": c["id"], "role": stage},
        sort_keys=True,
        separators=(",", ":"),
    )
    marker_lines = [line for line in lines if line.startswith("TRUSTED_QUALITY_LOOP_CARD:")]
    if not marker_lines:
        return (
            "pre-hardening Quality Loop card has no trusted v2 marker; create a fresh "
            f"{stage} card/campaign with the current controller"
        )
    if marker_lines != [marker]:
        return (
            "Quality Loop trusted card marker is malformed or role-mismatched; create a fresh "
            f"{stage} card with the current controller"
        )
    return None


def _execution_contract_from_body(task_body: str) -> Optional[dict[str, Any]]:
    prefix = "TRUSTED_EXECUTION_CONTRACT: "
    lines = [line.removeprefix(prefix) for line in task_body.splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        return None
    try:
        contract = json.loads(lines[0])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(contract, dict) or set(contract) != {
        "allowed_files", "baseline", "commands", "pre_execution_head", "repository"
    }:
        return None
    files = _strict_string_list(contract.get("allowed_files"))
    commands = _strict_string_list(contract.get("commands"))
    baseline = contract.get("baseline")
    repository = contract.get("repository")
    head = contract.get("pre_execution_head")
    if (
        files is None
        or not 1 <= len(files) <= 2
        or commands is None
        or not isinstance(baseline, dict)
        or not isinstance(repository, dict)
        or set(repository) != {"version", "head", "index", "control", "ignored"}
        or repository.get("version") != _SNAPSHOT_VERSION
        or any(
            not isinstance(repository.get(key), str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", repository[key])
            for key in ("head", "index", "control", "ignored")
        )
        or not set(files).issubset(baseline)
        or not isinstance(head, str)
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head)
        or any(
            not isinstance(path, str)
            or not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
            for path, value in baseline.items()
        )
    ):
        return None
    return {
        "allowed_files": files,
        "baseline": baseline,
        "repository": repository,
        "commands": commands,
        "pre_execution_head": head,
    }


def _execute_state_error(
    c: dict[str, Any],
    task: Any,
    payload: dict[str, Any],
    *,
    current: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    contract = _execution_contract_from_body(str(getattr(task, "body", "") or ""))
    if contract is None:
        return "execute card has no valid trusted execution contract"
    files_error = _relevant_files_error(c["workspace"], contract["allowed_files"])
    if files_error:
        return f"trusted relevant_files boundary is invalid: {files_error}"
    if current is None:
        try:
            current = _execution_git_state(c["workspace"], contract["allowed_files"])
        except RuntimeError as exc:
            return str(exc)
    if current["head"] != contract["pre_execution_head"]:
        return "Git HEAD changed after trusted executor dispatch"
    repository = contract["repository"]
    if current["index"] != repository["index"]:
        return "repository-control index changed after trusted executor dispatch"
    if current["control"] != repository["control"]:
        return "repository-control state changed after trusted executor dispatch"
    if current["ignored"] != repository["ignored"]:
        return "ignored workspace state changed after trusted executor dispatch"
    baseline = contract["baseline"]
    allowed = set(contract["allowed_files"])
    for path in set(baseline) - allowed:
        if current["files"].get(path) != baseline[path]:
            return "a pre-existing dirty file outside the trusted allowlist changed"
    changed = sorted(
        path
        for path in set(baseline) | set(current["files"])
        if baseline.get(path) != current["files"].get(path)
    )
    if payload.get("changed_files") != changed:
        return "changed_files must match the directly fingerprinted execution delta exactly"
    if not set(changed).issubset(allowed):
        return "execution delta contains a path outside the trusted relevant_files allowlist"
    verification = payload.get("verification")
    commands = (
        [entry.get("command") for entry in verification]
        if isinstance(verification, list) and all(isinstance(entry, dict) for entry in verification)
        else None
    )
    if commands != contract["commands"]:
        return "verification commands must exactly match the trusted execution contract"
    return None


def _quality_payload_error(
    c: dict[str, Any], task: Any, stage: str, payload: Any
) -> Optional[str]:
    if not isinstance(payload, dict):
        return "missing namespaced metadata.quality_loop object"
    role = "validate" if stage in {"validate", "integrate_validate", "final_validate"} else stage
    allowed = {
        "discover": {
            "schema", "role", "verdict", "project_kind", "languages", "manifests",
            "build_command", "test_command", "max_repairs", "runtime_evidence",
        },
        "examine": {
            "schema", "role", "verdict", "score_breakdown", "score_rationale",
            "selected_defect",
        },
        "scope_validate": {
            "schema", "role", "verdict", "scoped_improvement", "findings",
            "correction_prompt",
        },
        "plan": {
            "schema", "role", "decomposition_required", "rationale", "execution_slices",
        },
        "execute": {"schema", "role", "changed_files", "verification", "residual_risk"},
        "validate": {
            "schema", "role", "verdict", "build_passed", "tests_passed",
            "critical_issues", "high_issues", "regressions", "findings",
            "correction_prompt",
        },
    }[role]
    required = {
        "discover": allowed,
        "examine": {
            "schema", "role", "verdict", "score_breakdown", "score_rationale",
        },
        "scope_validate": {"schema", "role", "verdict", "findings"},
        "plan": {"schema", "role", "decomposition_required", "rationale"},
        "execute": allowed,
        "validate": allowed - {"correction_prompt"},
    }[role]
    unknown = sorted(set(payload) - allowed)
    missing = sorted(required - set(payload))
    if unknown:
        return "unexpected quality_loop fields: " + ", ".join(unknown)
    if missing:
        return "missing required quality_loop fields: " + ", ".join(missing)
    if payload.get("schema") != SCHEMA or payload.get("role") != role:
        return f"quality_loop must use schema {SCHEMA!r} and role {role!r}"
    if role == "discover":
        value = c.get("discovery_contract")
        try:
            contract = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            return "stored project discovery contract is malformed"
        if not isinstance(contract, dict) or payload.get("verdict") != "configured":
            return "discovery verdict must be configured against a trusted contract"
        for key in ("project_kind", "languages", "manifests", "runtime_evidence"):
            if payload.get(key) != contract.get(key):
                return f"discovery {key} must exactly match the trusted contract"
        build = payload.get("build_command")
        test = payload.get("test_command")
        if not isinstance(build, str) or build not in ["", *contract.get("build_candidates", [])]:
            return "discovery build_command is not a trusted candidate"
        if not isinstance(test, str) or test not in ["", *contract.get("test_candidates", [])]:
            return "discovery test_command is not a trusted candidate"
        if not build and not test:
            return "discovery must select at least one build or test command"
        repairs = payload.get("max_repairs")
        if type(repairs) is not int or repairs not in contract.get("repair_candidates", []):
            return "discovery max_repairs is not a trusted bounded candidate"
        return None
    if role == "examine":
        verdict = payload.get("verdict")
        if verdict not in {"proposal", "candidate_complete"}:
            return "examiner verdict must be proposal or candidate_complete"
        average, _ranking, error = _ranking_average(payload)
        if error or average is None:
            return error or "invalid score_breakdown"
        if not isinstance(payload.get("score_rationale"), str) or not payload["score_rationale"].strip():
            return "score_rationale must be non-empty"
        if verdict == "proposal" and _selected_defect(payload) is None:
            return "proposal requires exactly one valid selected_defect"
        if verdict == "candidate_complete" and "selected_defect" in payload:
            return "candidate_complete cannot include selected_defect"
        return None
    if role == "scope_validate":
        if _strict_string_list(payload.get("findings"), allow_empty=True) is None:
            return "findings must be a unique string array"
        verdict = payload.get("verdict")
        if verdict not in {"pass", "fail"}:
            return "scope validator verdict must be pass or fail"
        if verdict == "pass":
            if "correction_prompt" in payload:
                return "scope PASS cannot include correction_prompt"
            if _scoped_improvement(payload, c) is None:
                return "scope PASS requires one valid scoped_improvement"
        else:
            if "scoped_improvement" in payload:
                return "scope FAIL cannot include scoped_improvement"
            if (
                not isinstance(payload.get("correction_prompt"), str)
                or not payload["correction_prompt"].strip()
            ):
                return "scope FAIL requires a non-empty correction_prompt"
        return None
    if role == "plan":
        parent = _selected_campaign_improvement(c)
        if parent is None:
            return "planning requires one persisted scoped improvement"
        if _planned_improvement(payload, c, parent) is None:
            return "planning payload is not a valid bounded decomposition"
        return None
    if role == "execute":
        if _strict_string_list(payload.get("changed_files")) is None:
            return "changed_files must be a non-empty unique string array"
        verification = payload.get("verification")
        if not isinstance(verification, list) or not verification:
            return "verification must be a non-empty array"
        if any(
            not isinstance(entry, dict)
            or set(entry) != {"command", "exit_code"}
            or not isinstance(entry.get("command"), str)
            or not entry["command"].strip()
            or type(entry.get("exit_code")) is not int
            or entry["exit_code"] != 0
            for entry in verification
        ):
            return "verification entries require a command and exit_code 0"
        if _strict_string_list(payload.get("residual_risk"), allow_empty=True) is None:
            return "residual_risk must be a unique string array"
        return _execute_state_error(c, task, payload)
    if payload.get("verdict") not in {"pass", "fail"}:
        return "validator verdict must be pass or fail"
    if "correction_prompt" in payload and (
        not isinstance(payload["correction_prompt"], str)
        or not payload["correction_prompt"].strip()
    ):
        return "correction_prompt must be non-empty when provided"
    if any(type(payload.get(name)) is not bool for name in ("build_passed", "tests_passed")):
        return "build_passed and tests_passed must be booleans"
    if any(
        type(payload.get(name)) is not int or payload[name] < 0
        for name in ("critical_issues", "high_issues", "regressions")
    ):
        return "issue counts must be non-negative integers"
    if _strict_string_list(payload.get("findings"), allow_empty=True) is None:
        return "findings must be a unique string array"
    if payload["verdict"] == "pass" and (
        payload["build_passed"] is not True
        or payload["tests_passed"] is not True
        or any(payload[name] != 0 for name in ("critical_issues", "high_issues", "regressions"))
    ):
        return "validator PASS requires passing build/tests and zero issue counts"
    if payload["verdict"] == "fail" and (
        not isinstance(payload.get("correction_prompt"), str)
        or not payload["correction_prompt"].strip()
    ):
        return "validator FAIL requires a non-empty correction_prompt"
    return None


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
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, None, f"score_breakdown.{name} must be numeric from 0 through 10"
        score = float(value)
        if not math.isfinite(score) or not 0 <= score <= 10:
            return None, None, f"score_breakdown.{name} must be numeric from 0 through 10"
        scores[name] = score
    average = round(sum(scores.values()) / len(RANKING_CATEGORIES), 2)
    return average, scores, None


def _system_git() -> str:
    for candidate in (
        Path("/usr/bin/git"), Path("/bin/git"), Path("/usr/local/bin/git"),
        Path("/opt/homebrew/bin/git"), Path("C:/Program Files/Git/cmd/git.exe"),
    ):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
    raise RuntimeError("trusted system Git executable is unavailable")


def _git_command(
    workspace: str,
    *args: str,
    timeout: int = 300,
    env: Optional[dict[str, str]] = None,
    input_data: Optional[bytes] = None,
    root_fd: Optional[int] = None,
) -> subprocess.CompletedProcess[str]:
    command_env = dict(os.environ) if env is None else dict(env)
    if env is None:
        for key in tuple(command_env):
            if key.startswith("GIT_CONFIG_") or key in {
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_ASKPASS",
                "GIT_CEILING_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CONFIG",
                "GIT_DIR", "GIT_EDITOR", "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF",
                "GIT_GRAFT_FILE", "GIT_INDEX_FILE", "GIT_NAMESPACE",
                "GIT_OBJECT_DIRECTORY", "GIT_PAGER", "GIT_REPLACE_REF_BASE",
                "GIT_SHALLOW_FILE", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_WORK_TREE",
                "SSH_ASKPASS",
            }:
                command_env.pop(key, None)
    command_env["GIT_NO_REPLACE_OBJECTS"] = "1"
    command_env["GIT_CONFIG_NOSYSTEM"] = "1"
    command_env["GIT_CONFIG_GLOBAL"] = os.devnull
    command = [_system_git(), *args]
    cwd = workspace if root_fd is None else _workspace_fd_path(root_fd)
    pass_fds: tuple[int, ...] = () if root_fd is None else (root_fd,)
    inherited_object_fd = command_env.pop("HERMES_PINNED_OBJECT_FD", None)
    if inherited_object_fd is not None:
        pass_fds = (*pass_fds, int(inherited_object_fd))
    git_context = _current_git_context(root_fd)
    if git_context is not None:
        command_env["GIT_DIR"] = git_context.shim_git_dir
        command_env["GIT_COMMON_DIR"] = git_context.shim_git_dir
        command_env["GIT_WORK_TREE"] = cwd
        command_env["GIT_OBJECT_DIRECTORY"] = _workspace_fd_path(git_context.objects_fd)
        command_env.setdefault("GIT_INDEX_FILE", _workspace_fd_path(git_context.index_fd))
        pass_fds = tuple(dict.fromkeys((
            *pass_fds, git_context.objects_fd, git_context.index_fd,
        )))
    if input_data is None:
        return subprocess.run(
            command, cwd=cwd, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout, check=False, env=command_env,
            pass_fds=pass_fds,
        )
    completed = subprocess.run(
        command, cwd=cwd, input=input_data, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=timeout, check=False, env=command_env,
        pass_fds=pass_fds,
    )
    return subprocess.CompletedProcess(
        completed.args, completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"), None,
    )


def _git_without_hooks(
    workspace: str,
    *args: str,
    timeout: int = 300,
    env_overrides: Optional[dict[str, str]] = None,
    input_data: Optional[bytes] = None,
    root_fd: Optional[int] = None,
) -> subprocess.CompletedProcess[str]:
    """Run Git with hooks, signing, helpers, prompts, and injected config disabled."""
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("GIT_CONFIG_") or key in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_ASKPASS",
            "GIT_CEILING_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_DIR",
            "GIT_EDITOR", "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF", "GIT_GRAFT_FILE",
            "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY", "GIT_PAGER",
            "GIT_REPLACE_REF_BASE", "GIT_SHALLOW_FILE", "GIT_SSH",
            "GIT_SSH_COMMAND", "GIT_WORK_TREE", "SSH_ASKPASS",
        }:
            env.pop(key, None)
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_SSH_COMMAND="/usr/bin/ssh -oBatchMode=yes",
    )
    if env_overrides:
        env.update(env_overrides)
    with tempfile.TemporaryDirectory(prefix="quality-loop-empty-hooks-") as hooks:
        return _git_command(
            workspace,
            "-c",
            f"core.hooksPath={hooks}",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "-c",
            "credential.helper=",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.sshCommand=/usr/bin/ssh -oBatchMode=yes",
            *args,
            timeout=timeout,
            env=env,
            input_data=input_data,
            root_fd=root_fd,
        )


def _unsafe_publication_git_config(
    workspace: str, *, root_fd: Optional[int] = None,
) -> list[str]:
    pattern = re.compile(
        r"^(alias\..*|include\..*|includeif\..*|core\.(hookspath|sshcommand|fsmonitor|editor|pager|askpass)|"
        r"credential\..*helper|filter\..*\.(clean|smudge|process)|"
        r"diff\.external|diff\..*\.(command|textconv)|merge\..*\.driver|gpg\..*\.program|"
        r"(commit|tag)\.gpgsign|remote\..*\.(receivepack|uploadpack)|protocol\..*\.allow|"
        r"url\..*\.(insteadof|pushinsteadof)|sequence\.editor|interactive\.difffilter)$",
        re.IGNORECASE,
    )
    context = _current_git_context(root_fd)
    if context is not None:
        found = _pinned_config_command(context, "--name-only", "--null", "--list")
    else:
        found = _git_command(
            workspace, "config", "--local", "--name-only", "--null", "--list",
            root_fd=root_fd,
        )
    if found.returncode != 0:
        raise RuntimeError("could not inspect repository-local Git configuration")
    return sorted(
        value for value in found.stdout.split("\0") if value and pattern.fullmatch(value)
    )


@contextmanager
def _isolated_transport_repository(
    workspace: str, *, root_fd: Optional[int] = None,
) -> Iterator[tuple[Path, dict[str, str]]]:
    """Expose authenticated objects through a controller-owned repo with no mutable local config."""
    context = _current_git_context(root_fd)
    if context is None:
        raise RuntimeError("Git control context is not pinned for publication")
    object_format = _git_without_hooks(
        workspace, "rev-parse", "--show-object-format", root_fd=root_fd
    )
    format_name = object_format.stdout.strip()
    if object_format.returncode != 0 or format_name not in {"sha1", "sha256"}:
        raise RuntimeError("could not resolve trusted Git object storage for publication")
    with tempfile.TemporaryDirectory(prefix="quality-loop-transport-") as directory:
        repository = Path(directory) / "transport.git"
        initialized = _git_without_hooks(
            directory,
            "init",
            "--bare",
            f"--object-format={format_name}",
            str(repository),
        )
        if initialized.returncode != 0:
            raise RuntimeError("could not initialize isolated Git transport repository")
        yield repository, {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": _workspace_fd_path(context.objects_fd),
            "HERMES_PINNED_OBJECT_FD": str(context.objects_fd),
        }


def _safe_publication_remote_url(workspace: str, value: str) -> str:
    """Accept only built-in Git transports; reject executable/custom helpers."""
    value = value.strip()
    if not value or any(char in value for char in "\r\n\0") or value.startswith("-"):
        raise ValueError("invalid publication remote URL")
    if value.startswith(("/", "./", "../")):
        raise ValueError("local publication remotes are not allowed")
    if value.startswith("file://"):
        raise ValueError("file publication remotes are not allowed")
    if re.fullmatch(r"https?://[^\s/@]+(?:\.[^\s/@]+|:\d+|/).*", value):
        if "@" in value.split("//", 1)[1].split("/", 1)[0]:
            raise ValueError("credentials are not allowed in publication remote URLs")
        return value
    if re.fullmatch(r"ssh://(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+(?::\d+)?/.+", value):
        return value
    if re.fullmatch(
        r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+:[^:\s][^\s]*", value
    ) and not value.lower().startswith("ext:"):
        return value
    raise ValueError("publication remote uses an unsupported or executable transport")


def _publication_manifest(workspace: str, paths: list[str]) -> dict[str, str | None]:
    """Fingerprint publishable worktree bytes through stable no-follow descriptors."""
    root = Path(workspace)
    root_fd = _open_workspace_root(root)
    try:
        with _pinned_git_context(root, root_fd):
            manifest = _publication_manifest_with_root(workspace, paths, root, root_fd)
            if not _workspace_root_handle_matches(root_fd, root):
                raise RuntimeError("workspace root changed during publication manifest")
            return manifest
    finally:
        os.close(root_fd)


def _publication_manifest_with_root(
    workspace: str, paths: list[str], root: Path, root_fd: int,
) -> dict[str, str | None]:
    manifest: dict[str, str | None] = {}
    format_result = _git_command(
        workspace, "rev-parse", "--show-object-format", root_fd=root_fd
    )
    object_format = format_result.stdout.strip()
    if format_result.returncode != 0 or object_format not in {"sha1", "sha256"}:
        raise RuntimeError("could not resolve publication object format")
    for path in paths:
        entry = _read_workspace_entry(root, path, root_fd=root_fd)
        if entry is None:
            manifest[path] = None
            continue
        kind, info, content = entry
        if kind != "file":
            raise RuntimeError(f"publication path is not a regular file: {path}")
        digest = hashlib.new(object_format)
        digest.update(f"blob {len(content)}\0".encode("ascii"))
        digest.update(content)
        mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
        manifest[path] = f"{mode}:{digest.hexdigest()}"
    return manifest


def _staged_publication_manifest(
    workspace: str, paths: list[str]
) -> dict[str, str | None]:
    """Read the exact staged mode/blob pair for every authenticated path."""
    manifest: dict[str, str | None] = {}
    for path in paths:
        listed = _git_command(
            workspace, "ls-files", "--stage", "-z", "--", f":(literal){path}"
        )
        if listed.returncode != 0:
            raise RuntimeError(f"could not inspect staged publication path: {path}")
        entries = [entry for entry in listed.stdout.split("\0") if entry]
        if not entries:
            manifest[path] = None
            continue
        if len(entries) != 1 or "\t" not in entries[0]:
            raise RuntimeError(f"ambiguous staged publication path: {path}")
        metadata, staged_path = entries[0].split("\t", 1)
        fields = metadata.split()
        if staged_path != path or len(fields) != 3 or fields[2] != "0":
            raise RuntimeError(f"invalid staged publication entry: {path}")
        mode, object_id, _stage = fields
        if mode not in {"100644", "100755"} or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", object_id
        ):
            raise RuntimeError(f"invalid staged publication object: {path}")
        manifest[path] = f"{mode}:{object_id}"
    return manifest


def _tree_publication_manifest(
    workspace: str, tree: str, paths: list[str], *, root_fd: Optional[int] = None,
) -> dict[str, str | None]:
    manifest: dict[str, str | None] = {}
    for path in paths:
        listed = _git_command(
            workspace, "ls-tree", "-z", tree, "--", f":(literal){path}",
            root_fd=root_fd,
        )
        if listed.returncode != 0:
            raise RuntimeError(f"could not inspect publication tree path: {path}")
        entries = [entry for entry in listed.stdout.split("\0") if entry]
        if not entries:
            manifest[path] = None
            continue
        if len(entries) != 1 or "\t" not in entries[0]:
            raise RuntimeError(f"ambiguous publication tree path: {path}")
        metadata, tree_path = entries[0].split("\t", 1)
        fields = metadata.split()
        if tree_path != path or len(fields) != 3 or fields[1] != "blob":
            raise RuntimeError(f"invalid publication tree entry: {path}")
        mode, _kind, object_id = fields
        if mode not in {"100644", "100755"} or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", object_id
        ):
            raise RuntimeError(f"invalid publication tree object: {path}")
        manifest[path] = f"{mode}:{object_id}"
    return manifest


def _authenticated_publication_tree(
    workspace: str,
    base_revision: str,
    publication: dict[str, str | None],
) -> str:
    """Build and verify a tree from authenticated blob IDs in an isolated index."""
    root = Path(workspace)
    root_fd = _open_workspace_root(root)
    try:
        with _pinned_git_context(root, root_fd):
            tree = _authenticated_publication_tree_with_root(
                workspace, base_revision, publication, root, root_fd
            )
            if not _workspace_root_handle_matches(root_fd, root):
                raise RuntimeError("workspace root changed during isolated publication staging")
            return tree
    finally:
        os.close(root_fd)


def _authenticated_publication_tree_with_root(
    workspace: str,
    base_revision: str,
    publication: dict[str, str | None],
    root: Path,
    root_fd: int,
) -> str:
    with tempfile.TemporaryDirectory(prefix="quality-loop-index-") as directory:
        env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        seeded = _git_without_hooks(
            workspace, "read-tree", base_revision, env_overrides=env, root_fd=root_fd
        )
        if seeded.returncode != 0:
            raise RuntimeError("could not seed isolated publication index")
        for path, value in sorted(publication.items()):
            if value is None:
                updated = _git_without_hooks(
                    workspace,
                    "update-index",
                    "--force-remove",
                    "--",
                    path,
                    env_overrides=env,
                    root_fd=root_fd,
                )
            else:
                mode, object_id = value.split(":", 1)
                entry = _read_workspace_entry(root, path, root_fd=root_fd)
                if entry is None or entry[0] != "file":
                    raise RuntimeError(
                        f"publication path changed before isolated staging: {path}"
                    )
                _kind, info, content = entry
                current_mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
                if current_mode != mode:
                    raise RuntimeError(
                        f"publication path mode changed before isolated staging: {path}"
                    )
                stored = _git_without_hooks(
                    workspace,
                    "hash-object",
                    "-w",
                    "--no-filters",
                    "--stdin",
                    input_data=content,
                    root_fd=root_fd,
                )
                if stored.returncode != 0 or stored.stdout.strip() != object_id:
                    raise RuntimeError(
                        f"publication path bytes changed before isolated staging: {path}"
                    )
                updated = _git_without_hooks(
                    workspace,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    mode,
                    object_id,
                    path,
                    env_overrides=env,
                    root_fd=root_fd,
                )
            if updated.returncode != 0:
                raise RuntimeError(f"could not stage authenticated path in isolated index: {path}")
        written = _git_without_hooks(
            workspace, "write-tree", env_overrides=env, root_fd=root_fd
        )
        tree = written.stdout.strip()
        if written.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
            raise RuntimeError("could not freeze isolated authenticated publication tree")
    changed = _git_command(
        workspace, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z",
        base_revision, tree, "--", root_fd=root_fd,
    )
    changed_paths = sorted(path for path in changed.stdout.split("\0") if path)
    if changed.returncode != 0 or changed_paths != sorted(publication):
        raise RuntimeError("isolated publication tree changed unauthenticated paths")
    if _tree_publication_manifest(
        workspace, tree, sorted(publication), root_fd=root_fd
    ) != publication:
        raise RuntimeError("isolated publication tree bytes do not match authentication")
    return tree


def _sensitive_staged_path(path: str) -> bool:
    lowered = path.lower()
    parts = tuple(part for part in Path(lowered).parts if part not in {"", "."})
    name = parts[-1] if parts else ""
    if name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample"}):
        return True
    if any(
        part in {
            ".ssh", ".aws", ".azure", ".gnupg", ".docker", ".kube",
            ".terraform.d", ".composer", ".bundle",
        }
        for part in parts
    ):
        return True
    if name == "auth.json":
        return True
    if ".config" in parts and any(part in {"gcloud", "gh"} for part in parts):
        return True
    if name in {
        ".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials",
        "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml",
        "service-account.json", ".vault-token", "kubeconfig", "credentials.tfrc.json",
    }:
        return True
    if re.fullmatch(r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?", name):
        return True
    return (
        Path(lowered).suffix in {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".tfvars"}
        or lowered.endswith(".tfvars.json")
    )


def _runtime_artifact_path(path: str) -> bool:
    return any(
        part.lower() in {".quality-loop", ".hermes"}
        for part in Path(path).parts
    )


def _publish_success(c: dict[str, Any]) -> dict[str, Any]:
    """Commit and push only after final model validation and hard gates passed."""
    workspace = str(c["workspace"])
    failure: dict[str, Any] = {"ok": False, "committed": False, "pushed": False}
    try:
        root_fd = _open_workspace_root(Path(workspace))
    except (OSError, RuntimeError) as exc:
        failure["error"] = f"could not securely open publication workspace: {exc}"
        return failure
    try:
        try:
            with _pinned_git_context(Path(workspace), root_fd):
                result = _publish_success_with_root(c, root_fd)
                if (
                    not result.get("pushed")
                    and not _workspace_root_handle_matches(root_fd, Path(workspace))
                ):
                    failure["error"] = "workspace root changed during publication"
                    return failure
                return result
        except RuntimeError as exc:
            failure["error"] = f"trusted Git control state changed during publication: {exc}"
            return failure
    finally:
        os.close(root_fd)


def _publish_success_with_root(c: dict[str, Any], root_fd: int) -> dict[str, Any]:
    workspace = str(c["workspace"])
    result: dict[str, Any] = {"ok": False, "committed": False, "pushed": False}

    def bound_git(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _git_command(workspace, *args, root_fd=root_fd, **kwargs)

    def bound_git_safe(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _git_without_hooks(workspace, *args, root_fd=root_fd, **kwargs)

    initial = c.get("initial_snapshot")
    authenticated = c.get("authenticated_snapshot")
    if not isinstance(initial, dict) or not isinstance(authenticated, dict):
        result["error"] = "missing authenticated executor workspace snapshots"
        return result
    try:
        current = _execution_git_state_with_root(Path(workspace), None, root_fd)
    except RuntimeError as exc:
        result["error"] = f"could not verify authenticated executor state: {exc}"
        return result
    authenticated_state = {
        field: authenticated.get(field)
        for field in ("version", "head", "index", "control", "ignored", "files")
    }
    if current != authenticated_state:
        result["error"] = "workspace does not match the authenticated executor snapshot"
        return result
    for field in ("version", "head", "index", "control", "ignored"):
        if initial.get(field) != authenticated.get(field):
            result["error"] = (
                f"authenticated executor state changed protected repository field {field}"
            )
            return result
    if initial.get("files"):
        result["error"] = "campaign initial snapshot was not a clean isolated workspace"
        return result
    authenticated_paths = sorted(authenticated.get("files", {}))
    publish_paths = [
        path for path in authenticated_paths
        if not _runtime_artifact_path(path)
    ]
    expected_publication = authenticated.get("publication")
    if expected_publication is None and not publish_paths:
        expected_publication = {}
    if (
        not isinstance(expected_publication, dict)
        or sorted(expected_publication) != publish_paths
        or any(
            value is not None
            and (
                not isinstance(value, str)
                or not re.fullmatch(
                    r"100(?:644|755):(?:[0-9a-f]{40}|[0-9a-f]{64})", value
                )
            )
            for value in expected_publication.values()
        )
    ):
        result["error"] = "missing or malformed authenticated publication manifest"
        return result
    if any(_sensitive_staged_path(path) for path in publish_paths):
        result["error"] = "refusing to commit a sensitive credential/key path"
        return result

    root = bound_git("rev-parse", "--show-toplevel")
    if root.returncode != 0 or Path(root.stdout.strip()).resolve() != Path(workspace).resolve():
        result["error"] = "workspace is not the root of a Git worktree"
        return result

    branch = str(c.get("publish_branch") or "").strip()
    if not branch:
        context = _current_git_context(root_fd)
        assert context is not None
        _head_oid, head_ref = _pinned_head(context)
        branch = head_ref[len("refs/heads/"):] if head_ref and head_ref.startswith("refs/heads/") else ""
    if not branch or not _GIT_NAME_RE.fullmatch(branch):
        result["error"] = "cannot publish from a detached or invalid branch"
        return result
    checked = bound_git("check-ref-format", "--branch", branch)
    if checked.returncode != 0:
        result["error"] = "publish branch failed Git ref validation"
        return result

    remote = str(c.get("publish_remote") or "origin").strip()
    context = _current_git_context(root_fd)
    assert context is not None
    resolved_remote = _pinned_config_command(context, "--get", f"remote.{remote}.url")
    if resolved_remote.returncode != 0:
        result["error"] = f"Git remote {remote!r} does not exist"
        return result
    try:
        unsafe_config = _unsafe_publication_git_config(workspace, root_fd=root_fd)
    except RuntimeError as exc:
        result["error"] = f"unsafe publication configuration: {exc}"
        return result
    if unsafe_config:
        result["error"] = (
            "refusing repository-controlled executable Git configuration: "
            + ", ".join(unsafe_config)
        )
        return result
    context = _current_git_context(root_fd)
    assert context is not None
    if not context.matches():
        result["error"] = "Git control state changed during publication preflight"
        return result
    try:
        remote_url = _safe_publication_remote_url(workspace, resolved_remote.stdout)
    except ValueError as exc:
        result["error"] = f"unsafe publication configuration: {exc}"
        return result

    base_revision_text = _pinned_head_oid(root_fd)
    try:
        if _publication_manifest_with_root(
            workspace, publish_paths, Path(workspace), root_fd
        ) != expected_publication:
            result["error"] = "worktree bytes do not match authenticated executor publication"
            return result
        publication_tree = _authenticated_publication_tree_with_root(
            workspace, base_revision_text, expected_publication, Path(workspace), root_fd
        )
    except RuntimeError as exc:
        result["error"] = f"could not construct authenticated publication tree: {exc}"
        return result
    context = _current_git_context(root_fd)
    assert context is not None
    if not context.matches():
        result["error"] = "Git control state changed during publication tree construction"
        return result

    diff = bound_git(
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--unified=0",
        base_revision_text,
        publication_tree,
        timeout=120,
    )
    secret_pattern = re.compile(
        r"(?im)^\+.*(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{20,})"
    )
    if diff.returncode != 0 or secret_pattern.search(diff.stdout or ""):
        result["error"] = "refusing to commit because authenticated-tree secret screening failed"
        return result

    if publish_paths:
        checked_diff = bound_git(
            "diff", "--no-ext-diff", "--no-textconv", "--check", base_revision_text, publication_tree
        )
        if checked_diff.returncode != 0:
            result["error"] = "authenticated changes fail git diff --check"
            return result
        committed = bound_git_safe(
            "commit-tree",
            publication_tree,
            "-p",
            base_revision_text,
            "-m",
            str(c["commit_message"]),
            timeout=300,
        )
        commit_id = committed.stdout.strip()
        if committed.returncode != 0 or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", commit_id
        ):
            result["error"] = "git commit-tree failed"
            return result
        committed_tree = bound_git("rev-parse", f"{commit_id}^{{tree}}")
        committed_paths = bound_git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--name-only",
            "-z",
            base_revision_text,
            commit_id,
            "--",
        )
        if (
            committed_tree.returncode != 0
            or committed_tree.stdout.strip() != publication_tree
            or committed_paths.returncode != 0
            or sorted(path for path in committed_paths.stdout.split("\0") if path)
            != publish_paths
            or _tree_publication_manifest(
                workspace, publication_tree, publish_paths, root_fd=root_fd
            )
            != expected_publication
        ):
            result["error"] = (
                "committed tree does not exactly match authenticated publication"
            )
            return result
        if not context.matches():
            result["error"] = "Git control state changed before publication"
            return result
        # The exact commit object is pushed directly. Deliberately leave the
        # mutable workspace ref and index untouched: no userspace compare-and-
        # swap can be atomic against a same-user writer that ignores Git locks.
        result.update(committed=True, commit=commit_id, local_ref_updated=False)
    else:
        result.update(
            committed=True, commit=base_revision_text, local_ref_updated=False
        )
    result["branch"] = branch
    result["remote"] = remote

    try:
        with _isolated_transport_repository(
            workspace, root_fd=root_fd
        ) as (transport_repo, transport_env):
            pushed = _git_without_hooks(
                str(transport_repo),
                "push",
                "--no-verify",
                remote_url,
                f"{result['commit']}:refs/heads/{branch}",
                timeout=600,
                env_overrides=transport_env,
            )
            if pushed.returncode != 0:
                result["error"] = f"git push failed with exit code {pushed.returncode}"
                return result
            remote_ref = _git_without_hooks(
                str(transport_repo),
                "ls-remote",
                "--refs",
                remote_url,
                f"refs/heads/{branch}",
                timeout=300,
                env_overrides=transport_env,
            )
    except RuntimeError as exc:
        result["error"] = f"could not isolate Git transport: {exc}"
        return result
    remote_lines = [line.split() for line in remote_ref.stdout.splitlines() if line.strip()]
    if (
        remote_ref.returncode != 0
        or len(remote_lines) != 1
        or len(remote_lines[0]) != 2
        or remote_lines[0][0] != result["commit"]
        or remote_lines[0][1] != f"refs/heads/{branch}"
    ):
        result["error"] = "remote branch does not resolve to the exact published commit"
        return result
    context.allow_final_mismatch = True
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
        pending_correction=None,
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
        trust_error = _trusted_card_error(c, task, stage)
        if trust_error:
            _pause(
                c,
                f"Trusted card validation failed: {trust_error}",
                run_id=run.id if run else None,
            )
            return get_campaign(campaign_id)
        if stage in {
            "discover", "examine", "scope_validate", "plan", "validate",
            "integrate_validate", "final_validate",
        } and not _read_only_snapshot_unchanged(c, str(task.body or "")):
            _pause(
                c,
                f"Read-only {stage} worker mutated HEAD, index, worktree, ignored, or "
                "repository-control state; refusing to advance",
                run_id=run.id if run else None,
            )
            return get_campaign(campaign_id)
        payload = _handoff(
            run,
            expected_role=(
                "validate"
                if stage in {"validate", "integrate_validate", "final_validate"}
                else stage
            ),
        )
        payload_error = _quality_payload_error(c, task, stage, payload)
        if payload_error:
            _pause(
                c,
                f"Persisted {stage} handoff is invalid: {payload_error}. "
                "Create a fresh hardened card rather than migrating this result.",
                run_id=run.id if run else None,
            )
            return get_campaign(campaign_id)
        payload = payload or {}

        if stage == "discover":
            _update(
                campaign_id,
                build_command=payload["build_command"],
                test_command=payload["test_command"],
                max_repairs=payload["max_repairs"],
                message=(
                    "Project configuration persisted; creating examination card with "
                    f"max_repairs={payload['max_repairs']}"
                ),
            )
            persisted = get_campaign(campaign_id)
            if persisted is None:
                raise RuntimeError("campaign disappeared after discovery configuration persistence")
            selected = dict(persisted, stage="examine")
            task_id = _create_task(selected, "examine", [task.id])
            _update(
                campaign_id,
                stage="examine",
                active_task_id=task_id,
                processed_run_id=run.id if run else None,
                message=(
                    "Project configuration accepted; examination ready with "
                    f"max_repairs={payload['max_repairs']}"
                ),
            )
            return get_campaign(campaign_id)

        if stage == "examine":
            verdict = str((payload or {}).get("verdict", "")).lower()
            average, ranking, ranking_error = _ranking_average(payload or {})
            if ranking_error or average is None:
                _pause(
                    c,
                    f"Examiner handoff is invalid: {ranking_error or 'score_breakdown is missing'}",
                    run_id=run.id if run else None,
                )
                return get_campaign(campaign_id)
            target_average = c.get("target_average")

            if target_average is not None and average >= float(target_average):
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
                        f"{float(target_average):g}/10; final validation ready"
                    ),
                )
            elif verdict == "candidate_complete":
                if target_average is not None:
                    _pause(
                        c,
                        f"Examiner declared candidate_complete at average {average:g}/10, below target "
                        f"{float(target_average):g}/10",
                        run_id=run.id if run else None,
                    )
                else:
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
                        message="Examiner reports candidate complete; final audit ready",
                    )
            else:
                defect = _selected_defect(payload or {})
                if verdict != "proposal" or defect is None:
                    _pause(
                        c,
                        "Examiner did not return one valid selected_defect",
                        run_id=run.id if run else None,
                    )
                    return get_campaign(campaign_id)
                task_id = _create_task(c, "scope_validate", [task.id], improvement=defect)
                _update(
                    campaign_id,
                    stage="scope_validate", active_task_id=task_id, proposal_task_id=task.id,
                    processed_run_id=run.id if run else None, final_mode=0,
                    selected_improvement=None, slice_index=0, slice_count=0, repair_no=0,
                    last_average=average, last_ranking=json.dumps(ranking, sort_keys=True),
                    message=(
                        f"Examiner selected one defect at average {average:g}/10; "
                        "scope validation ready"
                    ),
                )

        elif stage == "scope_validate":
            if str((payload or {}).get("verdict", "")).lower() != "pass":
                findings = _string_list((payload or {}).get("findings"))
                detail = "; ".join(findings) or str(
                    (payload or {}).get("correction_prompt") or "scope contract was not satisfied"
                )
                _pause(
                    c,
                    f"Scope validation failed; planner was not created: {detail[:500]}",
                    run_id=run.id if run else None,
                )
                return get_campaign(campaign_id)

            repair_no = int(c.get("repair_no") or 0)
            selected = _selected_campaign_improvement(c)
            parent_scope = _slice_item(selected, int(c.get("slice_index") or 0)) if repair_no and selected else None
            chain_slices = _execution_slices(selected) if selected else []
            mid_chain = not repair_no and len(chain_slices) > 1
            if mid_chain:
                parent_scope = _slice_item(selected, int(c.get("slice_index") or 0))
            improvement = _scoped_improvement(
                payload or {}, c,
                parent=parent_scope,
                max_files=2 if (repair_no or mid_chain) else 5,
            )
            if improvement is None:
                _pause(
                    c,
                    "Scope validation passed but did not return one valid bounded scoped_improvement",
                    run_id=run.id if run else None,
                )
                return get_campaign(campaign_id)

            if repair_no:
                if selected is None:
                    _pause(
                        c,
                        "Repair scope passed but the persisted selected improvement is missing",
                        run_id=run.id if run else None,
                    )
                    return get_campaign(campaign_id)
                slices = _execution_slices(selected)
                index = int(c.get("slice_index") or 0)
                if not (0 <= index < len(slices)):
                    _pause(
                        c,
                        "Repair scope passed but the active slice index is invalid",
                        run_id=run.id if run else None,
                    )
                    return get_campaign(campaign_id)
                updated = dict(selected)
                if "execution_slices" in updated:
                    updated_slices = [dict(item) for item in slices]
                    updated_slices[index] = improvement
                    updated["execution_slices"] = updated_slices
                else:
                    updated = improvement
                c["selected_improvement"] = updated
                execution_item = _slice_item(updated, index)
                if execution_item is None:
                    _pause(
                        c,
                        "Repair scope passed but the repaired slice is not addressable",
                        run_id=run.id if run else None,
                    )
                    return get_campaign(campaign_id)
                repair_correction = str(c.get("pending_correction") or "")
                task_id = _create_task(
                    c, "execute", [task.id], improvement=execution_item,
                    correction=repair_correction,
                )
                _update(
                    campaign_id,
                    stage="execute", active_task_id=task_id,
                    processed_run_id=run.id if run else None,
                    selected_improvement=json.dumps(updated, sort_keys=True),
                    pending_correction=None,
                    message=(
                        f"Repair scope for slice {index + 1}/{len(slices)} passed; execution ready"
                    ),
                )
            elif mid_chain:
                if selected is None or parent_scope is None:
                    _pause(
                        c,
                        "Slice scope passed but the persisted slice chain is missing",
                        run_id=run.id if run else None,
                    )
                    return get_campaign(campaign_id)
                index = int(c.get("slice_index") or 0)
                updated = dict(selected)
                updated_slices = [dict(item) for item in chain_slices]
                updated_slices[index] = improvement
                updated["execution_slices"] = updated_slices
                c["selected_improvement"] = updated
                execution_item = _slice_item(updated, index)
                assert execution_item is not None
                task_id = _create_task(c, "execute", [task.id], improvement=execution_item)
                _update(
                    campaign_id,
                    stage="execute", active_task_id=task_id,
                    processed_run_id=run.id if run else None,
                    selected_improvement=json.dumps(updated, sort_keys=True),
                    message=(
                        f"Scope validation for slice {index + 1}/{len(chain_slices)} passed; "
                        "execution ready"
                    ),
                )
            else:
                c["selected_improvement"] = improvement
                task_id = _create_task(c, "plan", [task.id], improvement=improvement)
                _update(
                    campaign_id,
                    stage="plan", active_task_id=task_id,
                    processed_run_id=run.id if run else None,
                    selected_improvement=json.dumps(improvement, sort_keys=True),
                    message="Scope validation produced one bounded improvement; planning ready",
                )

        elif stage == "plan":
            scoped = _selected_campaign_improvement(c)
            planned = _planned_improvement(payload or {}, c, scoped) if scoped else None
            if planned is None:
                _pause(
                    c,
                    "Planner did not return a valid bounded plan",
                    run_id=run.id if run else None,
                )
                return get_campaign(campaign_id)
            slices = _execution_slices(planned)
            current_slice = _slice_item(planned, 0)
            c["selected_improvement"] = planned
            c["slice_index"] = 0
            c["slice_count"] = len(slices)
            task_id = _create_task(c, "execute", [task.id], improvement=current_slice)
            _update(
                campaign_id,
                stage="execute", active_task_id=task_id,
                processed_run_id=run.id if run else None,
                selected_improvement=json.dumps(planned, sort_keys=True),
                slice_index=0, slice_count=len(slices), repair_no=0,
                message=f"Planning complete; slice 1/{len(slices)} execution ready",
            )

        elif stage == "execute":
            contract = _execution_contract_from_body(str(task.body or ""))
            if contract is None:
                _pause(
                    c,
                    "Completed executor has no valid trusted execution contract",
                    run_id=run.id if run else None,
                )
                return get_campaign(campaign_id)
            try:
                validated_snapshot = _execution_git_state(
                    str(c["workspace"]), contract["allowed_files"]
                )
                authenticated_snapshot = _execution_git_state(str(c["workspace"]))
            except RuntimeError as exc:
                _pause(
                    c,
                    f"Could not authenticate completed executor workspace: {exc}",
                    run_id=run.id if run else None,
                )
                return get_campaign(campaign_id)
            state_error = _execute_state_error(
                c, task, payload, current=validated_snapshot
            )
            protected_fields_match = all(
                validated_snapshot.get(field) == authenticated_snapshot.get(field)
                for field in ("version", "head", "index", "control", "ignored")
            )
            authenticated_files = authenticated_snapshot.get("files", {})
            validated_files = validated_snapshot.get("files", {})
            reported_files = set(payload.get("changed_files") or [])
            snapshots_agree = (
                protected_fields_match
                and isinstance(authenticated_files, dict)
                and isinstance(validated_files, dict)
                and reported_files.issubset(authenticated_files)
                and all(
                    validated_files.get(path) == fingerprint
                    for path, fingerprint in authenticated_files.items()
                )
            )
            if state_error or not snapshots_agree:
                _pause(
                    c,
                    "Completed executor workspace changed while authenticating its exact delta: "
                    + (state_error or "consecutive snapshots disagree"),
                    run_id=run.id if run else None,
                )
                return get_campaign(campaign_id)
            publish_paths = sorted(
                path for path in authenticated_files if not _runtime_artifact_path(path)
            )
            try:
                authenticated_snapshot["publication"] = _publication_manifest(
                    str(c["workspace"]), publish_paths
                )
            except RuntimeError as exc:
                _pause(
                    c,
                    f"Could not freeze executor publication bytes: {exc}",
                    run_id=run.id if run else None,
                )
                return get_campaign(campaign_id)
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
                authenticated_snapshot=json.dumps(authenticated_snapshot, sort_keys=True),
                message="Execution complete; validation card ready",
            )

        elif stage in {"validate", "integrate_validate", "final_validate"}:
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

            if model_claims_pass and not _read_only_snapshot_unchanged(c, str(task.body or "")):
                _pause(
                    c,
                    f"Read-only {stage} workspace changed while running hard gates; "
                    "refusing to advance or publish",
                    run_id=run.id if run else None,
                )
                return get_campaign(campaign_id)

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
                                    f"{score_text}; published exact commit "
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
                        next_stage = "scope_validate"
                        task_id = _create_task(c, next_stage, [task.id], improvement=next_slice)
                        _update(
                            campaign_id,
                            stage=next_stage,
                            active_task_id=task_id,
                            slice_index=next_index,
                            repair_no=0,
                            processed_run_id=run.id if run else None,
                            message=(
                                f"Slice {slice_index + 1}/{len(slices)} validated; "
                                f"slice {next_index + 1}/{len(slices)} "
                                f"{'scope validation' if next_stage == 'scope_validate' else 'execution'} ready"
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
                elif stage == "final_validate":
                    _pause(
                        c,
                        "Final validation failed; refusing to create an unscoped repair executor",
                        run_id=run.id if run else None,
                    )
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
                    if not repair_scope:
                        _pause(
                            c,
                            "Validation failed but no trusted repair scope is available",
                            run_id=run.id if run else None,
                        )
                        return get_campaign(campaign_id)
                    if len(repair_scope.get("relevant_files", [])) > 2:
                        _pause(
                            c,
                            "Integrated repair spans more than two files; refusing to create an "
                            "oversized executor. A fresh examiner must split it into a smaller task.",
                            run_id=run.id if run else None,
                        )
                        return get_campaign(campaign_id)
                    task_id = _create_task(
                        c, "scope_validate", [task.id], improvement=repair_scope,
                        correction=correction,
                    )
                    _update(
                        campaign_id, stage="scope_validate", active_task_id=task_id,
                        repair_no=next_repair, pending_correction=correction,
                        processed_run_id=run.id if run else None,
                        message=(
                            f"Validation failed; repair {next_repair} scope validation ready: "
                            f"{correction[:240]}"
                        ),
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

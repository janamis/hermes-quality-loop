"""Quality Loop plugin registration."""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any

from . import quality_loop_controller as controller

logger = logging.getLogger(__name__)


class _ReconcileWorker:
    """Serialize slow campaign reconciliation away from dispatcher ticks."""

    def __init__(self) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._pending: set[str | None] = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="quality-loop-reconcile",
            daemon=True,
        )
        self._thread.start()

    def submit(self, board: str | None) -> None:
        with self._lock:
            if board in self._pending:
                return
            self._pending.add(board)
        self._queue.put(board)

    def _run(self) -> None:
        while True:
            board = self._queue.get()
            try:
                controller.reconcile_all(board=board)
            except Exception:
                logger.exception("quality-loop background reconcile failed for board %s", board)
            finally:
                with self._lock:
                    self._pending.discard(board)
                self._queue.task_done()


_WORKER: _ReconcileWorker | None = None


def _worker() -> _ReconcileWorker:
    global _WORKER
    if _WORKER is None:
        _WORKER = _ReconcileWorker()
    return _WORKER


def _status_text() -> str:
    campaigns = controller.list_campaigns()
    if not campaigns:
        return "Quality Loop: no campaigns. Open the Quality Loop page in Hermes Desktop."
    lines = ["Quality Loop campaigns:"]
    for campaign in campaigns[:20]:
        lines.append(
            f"- {campaign['id']}  {campaign['state']}  "
            f"round={campaign['round_no']} stage={campaign['stage']}  {campaign['name']}"
        )
    return "\n".join(lines)


def register(ctx: Any) -> None:
    worker = _worker()

    def on_tick(board: str | None = None, dry_run: bool = False, **_: Any) -> None:
        if dry_run:
            return
        worker.submit(board)

    def slash(raw: str) -> str:
        arg = raw.strip().lower()
        if not arg or arg == "status":
            return _status_text()
        if arg == "json":
            return json.dumps(controller.list_campaigns(), indent=2)
        return "Usage: /quality-loop [status|json]"

    ctx.register_hook("on_kanban_dispatch_tick", on_tick)
    ctx.register_command(
        "quality-loop",
        handler=slash,
        description="Show durable code-examination quality-loop campaigns",
        args_hint="[status|json]",
    )
    logger.info("quality-loop controller registered for profile %s", ctx.profile_name)

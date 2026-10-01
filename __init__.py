"""Quality Loop plugin registration."""

from __future__ import annotations

import json
import logging
from typing import Any

from . import quality_loop_controller as controller

logger = logging.getLogger(__name__)


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
    def on_tick(board: str | None = None, dry_run: bool = False, **_: Any) -> None:
        if dry_run:
            return
        controller.reconcile_all(board=board)

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

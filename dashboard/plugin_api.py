"""FastAPI surface for the Quality Loop Desktop plugin."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_MODULE = "hermes_quality_loop_controller"


def _load_controller():
    existing = sys.modules.get(CONTROLLER_MODULE)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        CONTROLLER_MODULE,
        ROOT / "quality_loop_controller.py",
    )
    if spec is None or spec.loader is None:
        raise ImportError("could not create the Quality Loop controller module spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[CONTROLLER_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(CONTROLLER_MODULE, None)
        raise
    return module


controller = _load_controller()

router = APIRouter()


class CampaignCreate(BaseModel):
    name: str = "Codebase Quality Loop"
    board: str = "default"
    workspace: str
    assignee: str = ""
    examiner_model: str = ""
    executor_model: str = ""
    validator_model: str = ""
    provider_override: Optional[str] = None
    build_command: str = ""
    test_command: str = ""
    gate_timeout_seconds: int = Field(default=900, ge=10, le=3600)
    target_average: Optional[float] = Field(default=None, gt=0, le=10)
    publish_on_success: bool = False
    publish_remote: str = "origin"
    publish_branch: Optional[str] = None
    commit_message: str = "quality-loop: reach target quality average"
    max_rounds: int = Field(default=20, ge=1, le=100)
    max_repairs: int = Field(default=3, ge=0, le=20)


@router.get("/campaigns")
def campaigns():
    return {"campaigns": controller.list_campaigns()}


@router.post("/campaigns")
def create_campaign(body: CampaignCreate):
    try:
        return {"campaign": controller.create_campaign(body.model_dump())}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/campaigns/{campaign_id}")
def campaign(campaign_id: str):
    item = controller.get_campaign(campaign_id)
    if not item:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"campaign": item}


@router.post("/campaigns/{campaign_id}/{action}")
def act(campaign_id: str, action: str):
    if action not in {"pause", "resume", "stop", "reconcile"}:
        raise HTTPException(status_code=404, detail="unknown action")
    try:
        item = (
            controller.reconcile_campaign(campaign_id)
            if action == "reconcile"
            else controller.set_campaign_state(campaign_id, action)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="campaign not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"campaign": item}


@router.get("/health")
def health():
    return {"ok": True, "plugin": "quality-loop", "schema": controller.SCHEMA}

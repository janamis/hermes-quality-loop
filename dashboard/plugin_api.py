"""FastAPI surface for the Quality Loop Desktop plugin."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException
from hermes_cli.profiles import resolve_profile_env
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


@router.get("/model-catalog")
def model_catalog(profile: str, provider: str):
    """Return non-secret cached model IDs for a Hermes profile/provider."""
    try:
        profile_home = Path(resolve_profile_env(profile))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Hermes profile not found") from exc

    cache_path = profile_home / "provider_models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"profile": profile, "provider": provider, "models": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Could not read the cached model catalog") from exc

    row = payload.get(provider, {}) if isinstance(payload, dict) else {}
    if not row and provider.startswith("custom:") and isinstance(payload, dict):
        try:
            config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            config = {}
        model_config = config.get("model", {}) if isinstance(config, dict) else {}
        base_url = str(model_config.get("base_url", "")).rstrip("/") if isinstance(model_config, dict) else ""
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        prefix = f"custom:{base_url}#" if base_url else ""
        matches = [value for key, value in payload.items() if prefix and key.startswith(prefix)]
        if len(matches) != 1:
            matches = [value for key, value in payload.items() if key.startswith("custom:")]
        if len(matches) == 1 and isinstance(matches[0], dict):
            row = matches[0]
    raw_models = row.get("models", []) if isinstance(row, dict) else []
    models = list(dict.fromkeys(str(model) for model in raw_models if model))
    return {"profile": profile, "provider": provider, "models": models}


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

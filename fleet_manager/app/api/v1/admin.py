"""Admin endpoints for the hot-reloadable ban-safety config (FR-145)."""
from fastapi import APIRouter

from app.api import deps
from app.core import safety_config

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get("/safety")
async def get_safety(api_key: deps.VerifyAPIKey):
    """Return a summary of the currently-active warmup schedules + rate limits."""
    return safety_config.active_summary()


@router.post("/reload-safety")
async def reload_safety(api_key: deps.VerifyAPIKey):
    """Re-read config/safety.yaml and apply it WITHOUT a restart (FR-145)."""
    summary = safety_config.reload()
    return {"reloaded": True, **summary}

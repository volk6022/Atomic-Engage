"""Path/source helpers for the 002-fix-extend TDD suite (imported by name, not relative)."""
import pathlib

import app

APP_ROOT = pathlib.Path(list(app.__path__)[0]).resolve()   # .../fleet_manager/app
PROJECT_ROOT = APP_ROOT.parent                              # .../fleet_manager


def read_source(relative_to_app: str) -> str:
    return (APP_ROOT / relative_to_app).read_text(encoding="utf-8")

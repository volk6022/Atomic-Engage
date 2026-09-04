"""READ_LIMITS budgets match contract §5, in both the code defaults and the
hot-reloadable config/safety.yaml (§4a/§4b) that overlays them."""
import pathlib

import yaml

from app.core import safety_defaults

_EXPECTED = {
    "resolve_username": 100,
    "get_chat_info": 200,
    "get_chat_history": 2000,
    "get_chat_admins": 100,
    "get_dialogs": 50,
    # 05.09.2026: `search_public_chat` в единственном числе был потолком без
    # действия — воркера с таким именем не существовало никогда. Разведка каналов
    # добавила два настоящих чтения, и лимит достался тому, кто его тратит.
    "get_similar_channels": 50,
    "search_public_chats": 50,
}


def test_safety_defaults_read_limits_match_contract():
    for action, cap in _EXPECTED.items():
        assert safety_defaults.READ_LIMITS[action] == cap


def test_safety_yaml_read_limits_match_contract():
    path = pathlib.Path(__file__).resolve().parents[2] / "config" / "safety.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for action, cap in _EXPECTED.items():
        assert data["read_limits"][action] == cap


def test_safety_yaml_has_service_testing_use_case():
    """B4b: config/safety.yaml must carry service_testing (warmup + rate limits) with
    the same values as the code defaults, or a service_testing account loses its
    warmup schedule the moment the yaml (now actually loaded) overlays the defaults."""
    path = pathlib.Path(__file__).resolve().parents[2] / "config" / "safety.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert data["warmup_schedules"]["service_testing"] == (
        safety_defaults.WARMUP_SCHEDULES["service_testing"]
    )
    assert data["rate_limits"]["service_testing"] == (
        safety_defaults.RATE_LIMITS["service_testing"]
    )

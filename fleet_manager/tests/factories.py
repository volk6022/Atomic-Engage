import json
import random
from pathlib import Path

from factory import Faker
from factory.alchemy import SQLAlchemyModelFactory

from app.db.models import Account, ApiCredential, Proxy, Task


BASE_DIR = Path(__file__).parent.parent
DEVICE_COMBOS_PATH = BASE_DIR / "data" / "device_combos.json"


def load_device_combos() -> list[dict]:
    with open(DEVICE_COMBOS_PATH) as f:
        return json.load(f)


def get_random_fingerprint() -> dict:
    combos = load_device_combos()
    weights = [c.get("weight", 1) for c in combos]
    selected = random.choices(combos, weights=weights)[0]
    return {
        "device_model": selected["device_model"],
        "system_version": selected["system_version"],
        "app_version": selected["app_version"],
        "lang_code": selected["lang_code"],
        "system_lang_code": selected["system_lang_code"],
    }


class AccountFactory(SQLAlchemyModelFactory):
    class Meta:
        model = Account

    status = "active"
    warmup_tier = "fresh"
    use_case = "reactions"
    phone = Faker("phone_number")
    phone_country = "RU"
    session_string = Faker("sha256")
    api_credential_id = 1
    proxy_id = 1
    work_start = 9
    work_end = 22
    flood_until = None
    ban_reason = None
    warmup_day = 0


class ProxyFactory(SQLAlchemyModelFactory):
    class Meta:
        model = Proxy

    url = "http://proxy.example.com:8080"
    proxy_type = "residential"
    country = "RU"
    tz_offset = 10800
    state = "assigned"
    is_healthy = True


class TaskFactory(SQLAlchemyModelFactory):
    class Meta:
        model = Task

    external_id = Faker("uuid4")
    account_id = 1
    task_type = "send_message"
    payload = {"peer_id": 123456789, "text": "Hello"}
    status = "queued"
    priority = 5
    retry_count = 0


class ApiCredentialFactory(SQLAlchemyModelFactory):
    class Meta:
        model = ApiCredential

    api_id = Faker("random_int", min=1000000, max=9999999)
    api_hash = Faker("sha256")
    account_count = 0

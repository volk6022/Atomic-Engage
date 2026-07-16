import json
import random
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEVICE_COMBOS_PATH = BASE_DIR / "data" / "device_combos.json"


@dataclass
class DeviceFingerprint:
    device_model: str
    system_version: str
    app_version: str
    lang_code: str
    system_lang_code: str


class DeviceFingerprintGenerator:
    def __init__(self, combos_path: str = ""):
        path = combos_path or str(DEVICE_COMBOS_PATH)
        with open(path) as f:
            self.combos = json.load(f)

    def generate(self, excluded_combos: list[dict] = None) -> DeviceFingerprint:
        excluded = excluded_combos or []

        for _ in range(100):
            weights = [c.get("weight", 1) for c in self.combos]
            selected = random.choices(self.combos, weights=weights)[0]

            combo = {
                "device_model": selected["device_model"],
                "system_version": selected["system_version"],
                "app_version": selected["app_version"],
                "lang_code": selected["lang_code"],
                "system_lang_code": selected["system_lang_code"],
            }

            if combo not in excluded:
                return DeviceFingerprint(**combo)

        raise ValueError("Could not generate unique fingerprint after 100 attempts")

    def immutability_guard(self, account, proposed_changes: dict) -> None:
        fields = [
            "device_model",
            "system_version",
            "app_version",
            "lang_code",
            "system_lang_code",
        ]

        for field in fields:
            if field in proposed_changes and proposed_changes[field] != getattr(
                account, field
            ):
                raise ValueError(f"Cannot modify immutable field: {field}")

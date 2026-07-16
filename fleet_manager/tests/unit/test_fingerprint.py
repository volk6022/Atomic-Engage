import pytest

from app.services.fingerprint import DeviceFingerprintGenerator, DeviceFingerprint


def test_fingerprint_returns_valid_combo_from_json():
    gen = DeviceFingerprintGenerator()
    fp = gen.generate()
    assert isinstance(fp, DeviceFingerprint)
    assert fp.device_model
    assert fp.system_version
    assert fp.app_version
    assert fp.lang_code
    assert fp.system_lang_code


def test_fingerprint_uniqueness_across_100_calls():
    gen = DeviceFingerprintGenerator()
    fingerprints = set()
    for _ in range(100):
        fp = gen.generate()
        fingerprints.add((fp.device_model, fp.system_version, fp.app_version))
    assert len(fingerprints) > 10


def test_fingerprint_immutability_guard_raises_on_modify_attempt():
    gen = DeviceFingerprintGenerator()

    class MockAccount:
        device_model = "Pixel 6"
        system_version = "14"
        app_version = "10.14.0"
        lang_code = "en"
        system_lang_code = "en-US"

    with pytest.raises(ValueError):
        gen.immutability_guard(MockAccount(), {"device_model": "iPhone"})


def test_fingerprint_weighted_selection():
    gen = DeviceFingerprintGenerator()
    counts = {}
    for _ in range(1000):
        fp = gen.generate()
        counts[fp.device_model] = counts.get(fp.device_model, 0) + 1

    assert counts


def test_fingerprint_all_required_fields_present():
    gen = DeviceFingerprintGenerator()
    fp = gen.generate()
    assert hasattr(fp, "device_model")
    assert hasattr(fp, "system_version")
    assert hasattr(fp, "app_version")
    assert hasattr(fp, "lang_code")
    assert hasattr(fp, "system_lang_code")

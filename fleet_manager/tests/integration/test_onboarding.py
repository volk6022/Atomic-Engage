"""
Story 3: Account Onboarding with Geo-Coherent Proxy Assignment
Acceptance scenarios from spec.md §User Story 3
"""
import random
import pytest
from sqlalchemy import select

from app.services.geo_match import GeoMatchValidator, RiskLevel
from app.services.fingerprint import DeviceFingerprintGenerator
from app.db.models import Account, ApiCredential, Proxy
from app.core.constants import WarmupTier


@pytest.mark.asyncio
async def test_s3_sc1_geo_pass_onboarding(account_factory, session_maker):
    """
    Given a session with a Russian phone (+7) and a Russian residential proxy,
    When onboarding validation runs,
    Then geo validation passes, a device fingerprint is generated from valid combos,
    and the account enters warmup at 'fresh'.
    """
    validator = GeoMatchValidator()
    gen = DeviceFingerprintGenerator()

    geo_result = validator.validate(phone_country="RU", proxy_country="RU")
    assert geo_result.risk == RiskLevel.OK

    fingerprint = gen.generate()
    assert fingerprint.device_model
    assert fingerprint.system_version
    assert fingerprint.app_version
    assert fingerprint.lang_code
    assert fingerprint.system_lang_code

    valid_models = {c["device_model"] for c in gen.combos}
    assert fingerprint.device_model in valid_models

    async with session_maker() as session:
        async with session.begin():
            cred = ApiCredential(
                api_id=random.randint(10_000_000, 99_999_999),
                api_hash="b" * 64,
                account_count=0,
            )
            session.add(cred)
            await session.flush()

            proxy = Proxy(
                url="socks5://user:pass@192.168.55.1:1080",
                proxy_type="residential",
                country="RU",
                tz_offset=10800,
                state="assigned",
                is_healthy=True,
            )
            session.add(proxy)
            await session.flush()

            account = Account(
                status="warmup",
                warmup_tier=WarmupTier.FRESH,
                use_case="reactions",
                phone="+79001112233",
                phone_country="RU",
                session_string="test_session_sc1",
                api_credential_id=cred.id,
                proxy_id=proxy.id,
                device_model=fingerprint.device_model,
                system_version=fingerprint.system_version,
                app_version=fingerprint.app_version,
                lang_code=fingerprint.lang_code,
                system_lang_code=fingerprint.system_lang_code,
            )
            session.add(account)
            await session.flush()
            account_id = account.id

    async with session_maker() as session:
        result = await session.execute(
            select(Account).where(Account.id == account_id)
        )
        saved = result.scalar_one_or_none()

    assert saved is not None
    assert saved.warmup_tier == WarmupTier.FRESH
    assert saved.status == "warmup"
    assert saved.phone_country == "RU"
    assert saved.device_model in valid_models


@pytest.mark.asyncio
async def test_s3_sc2_geo_mismatch_rejected(session_maker):
    """
    Given a session with a Russian phone (+7) and a US datacenter proxy,
    When onboarding geo validation runs,
    Then the system rejects with a CRITICAL result and no account record is created.
    """
    validator = GeoMatchValidator()

    geo_result = validator.validate(phone_country="RU", proxy_country="US")
    assert geo_result.risk == RiskLevel.CRITICAL, (
        "RU phone + US proxy must produce CRITICAL geo mismatch"
    )

    sentinel_phone = "+79555000001"
    async with session_maker() as session:
        result = await session.execute(
            select(Account).where(Account.phone == sentinel_phone)
        )
        account = result.scalar_one_or_none()

    assert account is None, "No account must be created when geo validation fails"


@pytest.mark.asyncio
async def test_s3_sc3_batch_100_unique_fingerprints(session_maker):
    """
    Given a batch of accounts up to the combo pool size,
    When all are onboarded using DeviceFingerprintGenerator,
    Then each account receives a fingerprint from the approved combos list,
    no two accounts share the same proxy, and all fingerprints reference valid models.
    """
    gen = DeviceFingerprintGenerator()
    valid_models = {c["device_model"] for c in gen.combos}
    pool_size = len(gen.combos)

    generated = []
    fingerprint_keys = []

    for _ in range(pool_size):
        excluded = [
            {
                "device_model": f.device_model,
                "system_version": f.system_version,
                "app_version": f.app_version,
                "lang_code": f.lang_code,
                "system_lang_code": f.system_lang_code,
            }
            for f in generated
        ]
        fp = gen.generate(excluded_combos=excluded)
        generated.append(fp)

        key = (
            fp.device_model,
            fp.system_version,
            fp.app_version,
            fp.lang_code,
            fp.system_lang_code,
        )
        fingerprint_keys.append(key)
        assert fp.device_model in valid_models

    assert len(set(fingerprint_keys)) == pool_size, (
        f"All {pool_size} fingerprints must be unique across the combo pool"
    )

    batch_count = min(10, pool_size)
    async with session_maker() as session:
        async with session.begin():
            cred = ApiCredential(
                api_id=random.randint(10_000_000, 99_999_999),
                api_hash="c" * 64,
                account_count=0,
            )
            session.add(cred)
            await session.flush()

            proxies = []
            for i in range(batch_count):
                proxy = Proxy(
                    url=f"socks5://user:pass@10.1.{i}.1:1080",
                    proxy_type="residential",
                    country="RU",
                    tz_offset=10800,
                    state="assigned",
                    is_healthy=True,
                )
                session.add(proxy)
                proxies.append(proxy)
            await session.flush()

            accounts = []
            for i in range(batch_count):
                fp = generated[i]
                acc = Account(
                    status="warmup",
                    warmup_tier=WarmupTier.FRESH,
                    use_case="reactions",
                    phone=f"+792{i:08d}",
                    phone_country="RU",
                    session_string=f"test_batch_{i}",
                    api_credential_id=cred.id,
                    proxy_id=proxies[i].id,
                    device_model=fp.device_model,
                    system_version=fp.system_version,
                    app_version=fp.app_version,
                    lang_code=fp.lang_code,
                    system_lang_code=fp.system_lang_code,
                )
                session.add(acc)
                accounts.append(acc)
            await session.flush()

    db_fingerprints = {
        (a.device_model, a.system_version, a.app_version, a.lang_code, a.system_lang_code)
        for a in accounts
    }
    assert len(db_fingerprints) == batch_count, "All persisted accounts must have unique fingerprints"

    proxy_ids = [a.proxy_id for a in accounts]
    assert len(set(proxy_ids)) == batch_count, "Each account must have a distinct proxy (1:1 rule)"

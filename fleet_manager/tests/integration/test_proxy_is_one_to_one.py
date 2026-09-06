"""Один прокси — один аккаунт: конституция требует, схема не запрещала (план 3.4).

Связь аккаунта с прокси объявлена 1:1 во всей документации и во всех рассуждениях о
безопасности флота: два аккаунта за одним выходом — это два аккаунта, которых Telegram
может связать между собой одним признаком. Схема при этом не мешала поставить
`proxy_id` двум строкам, и заметить это можно было только по последствиям.

Авария 18.08 была именно про прокси, поэтому проверка здесь не «на будущее».

**NULL остаётся общим и это не послабление.** `proxy_id IS NULL` означает «работать с
собственного IP машины» — намеренный режим для коробки с жилым адресом, и таких
аккаунтов может быть сколько угодно. Postgres считает NULL-ы различными, поэтому
обычный UNIQUE выражает ровно нужное правило без частичного индекса.

Перед выкаткой проверено, что живые базы (`vertsanov`, `clienta`, `clientb`) правило
уже соблюдают — ограничение, которое не может примениться, хуже отсутствующего.

⚠️ **Зелёный прогон здесь НЕ доказывает, что миграция нужна или работает.**
`0001_initial` собирает таблицы из ЖИВОЙ метаданной ORM (`Base.metadata.create_all`),
поэтому на чистой базе индекс появляется уже от `unique=True` в модели, без всякой
`0006`. На СУЩЕСТВУЮЩЕЙ базе — наоборот: `create_all` не трогает уже созданную
таблицу вовсе, то есть индекс туда не придёт ни от модели, ни сам по себе.

И отдельно: на проде **нет таблицы `alembic_version`** — схема там создана
`create_all` на старте шлюза (`app/main.py`), а миграции никто никогда не запускал.
Поэтому `0006` — верный артефакт для тестов и будущего, но на живой базе индекс
ставится руками вместе с выкаткой — иначе правило есть в репозитории и нет там,
где оно должно работать.
"""
import pytest

pytest.importorskip("pyrogram")

from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.db.models import Account, ApiCredential, Proxy  # noqa: E402

MARK = "test-one-to-one"


async def _seed(session_maker) -> tuple[int, int]:
    """Свои учётные данные и два прокси — чтобы не зависеть от чужих строк."""
    async with session_maker() as db:
        cred = ApiCredential(api_id=999001, api_hash="h" * 32)
        db.add(cred)
        first = Proxy(url=f"socks5://{MARK}:1@example.invalid:11001",
                      proxy_type="residential", country="RU", tz_offset=10800,
                      state="assigned", is_healthy=True)
        second = Proxy(url=f"socks5://{MARK}:2@example.invalid:11002",
                       proxy_type="residential", country="RU", tz_offset=10800,
                       state="assigned", is_healthy=True)
        db.add_all([first, second])
        await db.flush()
        ids = (cred.id, first.id)
        await db.commit()
        return ids


def _account(cred_id: int, proxy_id, phone: str) -> Account:
    return Account(phone=phone, phone_country="RU", session_string="s" * 40,
                   api_credential_id=cred_id, proxy_id=proxy_id,
                   device_model="m", system_version="v", app_version="a",
                   lang_code="ru", system_lang_code="ru",
                   status="active", warmup_tier="fresh", use_case="reactions")


@pytest.fixture()
def cleanup(session_maker):
    async def _wipe():
        async with session_maker() as db:
            await db.execute(delete(Account).where(Account.phone.like(f"{MARK}%")))
            await db.execute(delete(Proxy).where(Proxy.url.like(f"%{MARK}%")))
            await db.execute(delete(ApiCredential).where(ApiCredential.api_id == 999001))
            await db.commit()
    return _wipe


@pytest.mark.asyncio
async def test_the_schema_refuses_a_second_account_on_the_same_proxy(session_maker,
                                                                     cleanup):
    await cleanup()
    cred_id, proxy_id = await _seed(session_maker)
    try:
        async with session_maker() as db:
            db.add(_account(cred_id, proxy_id, f"{MARK}-1"))
            await db.commit()

        with pytest.raises(IntegrityError):
            async with session_maker() as db:
                db.add(_account(cred_id, proxy_id, f"{MARK}-2"))
                await db.commit()
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_accounts_without_a_proxy_are_still_allowed_to_share_nothing(
        session_maker, cleanup):
    """Режим «свой IP машины» не должен пасть жертвой правила про прокси."""
    await cleanup()
    cred_id, _ = await _seed(session_maker)
    try:
        async with session_maker() as db:
            db.add(_account(cred_id, None, f"{MARK}-a"))
            db.add(_account(cred_id, None, f"{MARK}-b"))
            await db.commit()

        async with session_maker() as db:
            rows = (await db.execute(select(Account).where(
                Account.phone.like(f"{MARK}%")))).scalars().all()
        assert len(rows) == 2
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_the_constraint_is_in_the_database_and_not_only_in_the_model(
        session_maker):
    """Правило обязано жить в схеме.

    Уникальность, объявленная только в модели, не мешает ни `INSERT` мимо ORM, ни
    второму процессу: гонку ловит база или никто.
    """
    async with session_maker() as db:
        found = (await db.execute(text(
            "select count(*) from pg_indexes "
            "where tablename = 'accounts' and indexdef ilike '%unique%' "
            "and indexdef ilike '%(proxy_id)%'"))).scalar_one()
    assert found >= 1, "в схеме нет уникального индекса по accounts.proxy_id"

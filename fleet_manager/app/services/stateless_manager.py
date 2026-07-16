"""Stateless per-task Telegram client lifecycle (Constitution Principle I).

A fresh client is created per task from account state loaded out of PostgreSQL, the
action runs inside `async with client:`, and the connection closes immediately after.

The client *factory* is injectable so tests can substitute a fake kurigram client at
this boundary (the only sanctioned non-persistence mock, since real MTProto cannot run
in CI). Production uses the real kurigram `Client`.
"""
from sqlalchemy import select

from app.db.models import Account, ApiCredential, Proxy


def _default_client_factory(account: Account, credential: ApiCredential, proxy: Proxy):
    from pyrogram import Client

    return Client(
        name=f"acct_{account.id}",
        session_string=account.session_string,
        api_id=credential.api_id,
        api_hash=credential.api_hash,
        proxy=proxy.url if proxy else None,
        device_model=account.device_model,
        system_version=account.system_version,
        app_version=account.app_version,
        lang_code=account.lang_code,
        system_lang_code=account.system_lang_code,
        in_memory=True,
    )


# Overridable seam: callable(account, credential, proxy) -> async-context client.
_client_factory = _default_client_factory


def set_client_factory(factory) -> None:
    """Override the kurigram client factory (used by tests to inject a fake)."""
    global _client_factory
    _client_factory = factory


def reset_client_factory() -> None:
    global _client_factory
    _client_factory = _default_client_factory


class StatelessManager:
    async def execute(self, account_id: int, action_fn, db):
        account = (
            await db.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()
        if not account:
            raise ValueError(f"Account {account_id} not found")

        credential = (
            await db.execute(
                select(ApiCredential).where(ApiCredential.id == account.api_credential_id)
            )
        ).scalar_one_or_none()
        proxy = (
            await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
        ).scalar_one_or_none()

        async with _client_factory(account, credential, proxy) as client:
            return await action_fn(client)

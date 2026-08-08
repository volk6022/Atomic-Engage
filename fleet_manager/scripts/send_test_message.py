#!/usr/bin/env python3
"""One-off: send a test message from a fleet account straight through its stored session.

Standalone (does NOT go through the worker queue / warmup gate) — used to confirm an
onboarded account can actually reach Telegram and write, and to produce a presence blip.
Loads the account (session_string, device, proxy-or-none) from the DB exactly as the
StatelessManager factory would.

Run:
  .venv/Scripts/python.exe scripts/send_test_message.py --account-id 6 \
      --to volk6022 --text "hello"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import select

from app.db.models import Account, ApiCredential, Proxy
from app.db.session import get_session_maker


async def run(args) -> int:
    from pyrogram import Client

    session_maker = get_session_maker()
    async with session_maker() as db:
        account = (
            await db.execute(select(Account).where(Account.id == args.account_id))
        ).scalar_one_or_none()
        if account is None:
            print(f"no account id={args.account_id}")
            return 2
        cred = (
            await db.execute(
                select(ApiCredential).where(ApiCredential.id == account.api_credential_id)
            )
        ).scalar_one()
        proxy = None
        if account.proxy_id is not None:
            proxy = (
                await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
            ).scalar_one_or_none()

    client = Client(
        name=f"send_{account.id}",
        session_string=account.session_string,
        api_id=cred.api_id,
        api_hash=cred.api_hash,
        proxy=proxy.url if proxy else None,
        device_model=account.device_model,
        system_version=account.system_version,
        app_version=account.app_version,
        lang_code=account.lang_code,
        system_lang_code=account.system_lang_code,
        in_memory=True,
    )

    async with client:
        me = await client.get_me()
        print(f"[send] as id={me.id} @{me.username or '-'} proxy={'yes' if proxy else 'host-ip'}",
              flush=True)
        msg = await client.send_message(args.to, args.text)
        print(f"[send] OK -> chat={msg.chat.id} msg_id={msg.id} to={args.to!r}", flush=True)
    return 0


def main() -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--to", required=True, help="username or peer id to send to")
    ap.add_argument("--text", required=True)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

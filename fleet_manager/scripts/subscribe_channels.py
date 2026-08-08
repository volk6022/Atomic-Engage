#!/usr/bin/env python3
"""Subscribe a fleet account to public channels so the Watcher receives their posts.

Loads the account exactly like send_test_message.py (session, device, proxy-or-host-IP)
and calls join_chat for each username. Idempotent: an already-joined channel is a
no-op. Setup action for monitoring — kept separate from the warmup pipeline.

Run:
  .venv/Scripts/python.exe scripts/subscribe_channels.py --account-id 6 \
      --channels ai_rabota datascienceml_jobs Machinelearning_Jobs
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

    sm = get_session_maker()
    async with sm() as db:
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
        name=f"subscribe_{account.id}",
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
        print(f"[sub] as id={me.id} proxy={'yes' if proxy else 'host-ip'}", flush=True)
        for ch in args.channels:
            u = ch.lstrip("@")
            try:
                chat = await client.join_chat(u)
                print(f"[sub] OK @{u} -> id={chat.id} title={chat.title!r}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[sub] FAIL @{u}: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(args.gap)
    return 0


def main() -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--channels", nargs="+", required=True)
    ap.add_argument("--gap", type=float, default=20.0, help="seconds between joins")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

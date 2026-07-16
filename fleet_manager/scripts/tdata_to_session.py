#!/usr/bin/env python3
"""Convert a Telegram **Desktop** `tdata` folder into a kurigram/pyrogram session
string that the fleet (and scripts/smoke_session.py) can use.

No network is required: the auth key is read straight out of tdata via `opentele`
and repacked into kurigram's session-string format. Because we reuse the EXISTING
auth key (no re-login) and keep the original api_id, the account's session identity is
preserved (Constitution Principle III / FR-146). Run this on a trusted local machine.

Install (locally):  pip install opentele kurigram TgCrypto

Usage:
  # main account in the tdata folder
  python scripts/tdata_to_session.py --tdata /path/to/tdata --user-id 8774901937
  # all accounts in the tdata
  python scripts/tdata_to_session.py --tdata /path/to/tdata --all
  # verify the packing/format works on this machine (no tdata needed)
  python scripts/tdata_to_session.py --self-test

The api_id MUST match the one the session was created with (Telegram Desktop = 2040);
the auth key is bound to it. user_id is informational — take it from the seller
metadata JSON (the "id" field) or pass 0.
"""
import argparse
import asyncio
import base64
import os
import struct
import sys

# kurigram/pyrogram session-string format: dc_id, api_id, test_mode, auth_key(256),
# user_id, is_bot — base64url, '=' stripped (verified against pyrogram 2.2.x).
SESSION_STRING_FORMAT = ">BI?256sQ?"
TDESKTOP_API_ID = 2040


def make_kurigram_session_string(
    *, dc_id: int, api_id: int, auth_key: bytes, user_id: int,
    test_mode: bool = False, is_bot: bool = False,
) -> str:
    if len(auth_key) != 256:
        raise ValueError(f"auth_key must be 256 bytes, got {len(auth_key)}")
    packed = struct.pack(
        SESSION_STRING_FORMAT, dc_id, api_id, test_mode, auth_key, user_id, is_bot
    )
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


async def _verify_roundtrip(session_string: str, api_id: int) -> dict:
    """Load the string into a kurigram client (no connect) and read fields back."""
    from pyrogram import Client

    app = Client(
        name="verify", session_string=session_string, api_id=api_id,
        api_hash="0" * 32, in_memory=True,
    )
    await app.storage.open()
    out = {
        "dc_id": await app.storage.dc_id(),
        "api_id": await app.storage.api_id(),
        "user_id": await app.storage.user_id(),
        "auth_key_len": len(await app.storage.auth_key()),
    }
    await app.storage.close()
    return out


def _extract_accounts(tdata_path: str):
    """Yield (label, dc_id, auth_key_bytes, user_id) for each account in tdata."""
    from opentele.td import TDesktop

    tdesk = TDesktop(tdata_path)
    if not tdesk.isLoaded() or tdesk.accountsCount < 1:
        raise SystemExit(f"no accounts loaded from tdata: {tdata_path}")

    for idx, acc in enumerate(tdesk.accounts):
        ak = acc.authKey
        if ak is None:
            print(f"  account[{idx}]: no auth key (skipped)", file=sys.stderr)
            continue
        dc_id = getattr(ak, "dcId", None) or acc.MainDcId
        user_id = int(getattr(acc, "UserId", 0) or 0)
        yield (f"account[{idx}]", int(dc_id), bytes(ak.key), user_id)


async def run(args) -> int:
    if args.self_test:
        fake_key = os.urandom(256)
        s = make_kurigram_session_string(
            dc_id=2, api_id=TDESKTOP_API_ID, auth_key=fake_key, user_id=8774901937
        )
        info = await _verify_roundtrip(s, TDESKTOP_API_ID)
        ok = (
            info["dc_id"] == 2
            and info["api_id"] == TDESKTOP_API_ID
            and info["user_id"] == 8774901937
            and info["auth_key_len"] == 256
        )
        print(f"self-test: packed+parsed OK={ok} -> {info}")
        print(f"sample string (synthetic key): {s[:24]}...{s[-8:]} (len={len(s)})")
        return 0 if ok else 1

    if not args.tdata:
        print("provide --tdata PATH (or --self-test)", file=sys.stderr)
        return 2

    accounts = list(_extract_accounts(args.tdata))
    if not args.all:
        accounts = accounts[:1]

    for label, dc_id, auth_key, user_id in accounts:
        uid = args.user_id if args.user_id is not None else user_id
        s = make_kurigram_session_string(
            dc_id=dc_id, api_id=args.api_id, auth_key=auth_key, user_id=uid
        )
        info = await _verify_roundtrip(s, args.api_id)
        print(f"\n=== {label} dc={dc_id} user_id={uid} (verified={info}) ===")
        if args.out:
            path = args.out if not args.all else f"{args.out}.{label.strip('account[]')}"
            with open(path, "w") as f:
                f.write(s + "\n")
            print(f"  written to {path}")
        else:
            print(s)
    print(
        "\nKeep these strings SECRET (they are full account credentials). "
        "Use with scripts/smoke_session.py --session-string ... through the proxy."
    )
    return 0


def main():
    p = argparse.ArgumentParser(description="tdata -> kurigram session string (no network).")
    p.add_argument("--tdata", help="path to the Telegram Desktop tdata folder")
    p.add_argument("--api-id", type=int, default=TDESKTOP_API_ID, help="api_id the session was made with (Desktop=2040)")
    p.add_argument("--user-id", type=int, default=None, help="override user_id (else read from tdata; informational)")
    p.add_argument("--out", help="write the session string to this file instead of stdout")
    p.add_argument("--all", action="store_true", help="export every account in the tdata")
    p.add_argument("--self-test", action="store_true", help="verify packing/format without tdata")
    sys.exit(asyncio.run(run(p.parse_args())))


if __name__ == "__main__":
    main()

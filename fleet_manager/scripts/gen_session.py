#!/usr/bin/env python3
import asyncio
import sys
from pyrogram import Client


async def main():
    print("Telegram Session Generator")
    print("=" * 40)

    api_id = int(input("API ID: ").strip())
    api_hash = input("API Hash: ").strip()
    phone = input("Phone (with country code, e.g. +79001234567): ").strip()

    client = Client(api_id=api_id, api_hash=api_hash, phone_number=phone)

    await client.connect()

    code = await client.send_code(phone)
    print(f"Code sent to {phone}")

    otp = input("Enter OTP code: ").strip()

    await client.sign_in(phone, otp)

    session_string = await client.export_session_string()

    print("\n" + "=" * 40)
    print("SESSION STRING:")
    print(session_string)
    print("=" * 40)

    await client.disconnect()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

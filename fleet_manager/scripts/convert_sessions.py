"""
Convert old .session files to new format using Kurigram/Pyrogram.
Loads old sessions and exports them as proper session strings.
"""

import asyncio
import json
import sqlite3
from pathlib import Path
from pyrogram import Client


async def convert_session(session_file: Path, json_data: dict) -> str:
    """Convert a single .session file to new format session string."""
    # Extract API credentials from JSON
    api_id = json_data.get("app_id") or json_data.get("api_id")
    api_hash = json_data.get("app_hash") or json_data.get("api_hash")

    if not api_id or not api_hash:
        print(f"  Error: Missing API credentials in JSON")
        return None

    # Create a temporary client using the old session file
    # The .session file will be used directly by Pyrogram
    temp_name = f"temp_convert_{session_file.stem}"

    # Copy session to temp location with proper name
    import shutil

    temp_dir = Path("temp_convert_sessions")
    temp_dir.mkdir(exist_ok=True)
    temp_session = temp_dir / f"{temp_name}.session"
    shutil.copy2(session_file, temp_session)

    client = None
    try:
        client = Client(
            name=temp_name,
            api_id=int(api_id),
            api_hash=api_hash,
            workdir=str(temp_dir),
        )

        # Connect and export session string
        await client.connect()

        # Check if authorized
        try:
            me = await client.get_me()
            if not me:
                print(f"  Error: Session not authorized")
                return None
        except Exception as e:
            print(f"  Error: Session authorization failed: {e}")
            return None

        # Export as new format session string
        session_string = await client.export_session_string()

        await client.disconnect()

        return session_string

    except Exception as e:
        print(f"  Error: {e}")
        if client:
            try:
                await client.disconnect()
            except:
                pass
        return None
    finally:
        # Cleanup temp files
        try:
            if temp_session.exists():
                temp_session.unlink()
            # Also remove any journal files
            for f in temp_dir.glob(f"{temp_name}*"):
                f.unlink()
        except:
            pass


async def convert_all_sessions(source_dir="111", output_file="accounts.json"):
    """Convert all old sessions to new format."""
    source_path = Path(source_dir)
    output_path = Path(output_file)

    if not source_path.exists():
        print(f"Error: Source directory {source_dir} not found!")
        return

    # Find all JSON files
    json_files = list(source_path.glob("*.json"))

    if not json_files:
        print(f"No .json files found in {source_dir}")
        return

    print(f"Found {len(json_files)} accounts to convert")
    print("-" * 60)

    accounts = []
    converted = 0
    failed = 0

    for json_file in json_files:
        # Read account metadata
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        session_file_name = data.get("session_file") or json_file.stem
        session_file = source_path / f"{session_file_name}.session"

        print(f"Processing: {json_file.stem}")

        if not session_file.exists():
            # Try alternative naming with _pyrogram suffix
            alt_session_file = source_path / f"{session_file_name}_pyrogram.session"
            if alt_session_file.exists():
                session_file = alt_session_file
            else:
                print(f"  Error: Session file not found: {session_file.name}")
                failed += 1
                continue

        # Convert session
        session_string = await convert_session(session_file, data)

        if not session_string:
            failed += 1
            continue

        # Build account entry
        account = {
            "phone": data.get("phone") or json_file.stem,
            "session_string": session_string,
            "first_name": data.get("first_name") or "",
            "last_name": data.get("last_name") or "",
            "username": data.get("username") or "",
        }

        accounts.append(account)
        converted += 1
        print(f"  [OK] Converted successfully (length: {len(session_string)})")

    # Cleanup temp directory
    temp_dir = Path("temp_convert_sessions")
    if temp_dir.exists():
        try:
            temp_dir.rmdir()
        except:
            pass

    # Save accounts.json
    print("-" * 60)

    if accounts:
        # Update existing accounts.json if it exists
        if output_path.exists():
            with open(output_path, "r", encoding="utf-8") as f:
                try:
                    existing = json.load(f)
                    if isinstance(existing, list):
                        # Update existing accounts and add new ones
                        existing_phones = {acc.get("phone") for acc in existing}
                        updated_accounts = []

                        for acc in accounts:
                            phone = acc.get("phone")
                            # Check if this phone already exists in existing accounts
                            existing_acc = next(
                                (a for a in existing if a.get("phone") == phone), None
                            )
                            if existing_acc:
                                # Update the existing account with new session string
                                existing_acc["session_string"] = acc["session_string"]
                                updated_accounts.append(phone)
                            else:
                                # Add as new account
                                existing.append(acc)

                        accounts = existing
                        print(f"Updated existing accounts.json")
                        print(f"  - Accounts updated: {len(updated_accounts)}")
                        print(f"  - Total accounts: {len(accounts)}")
                except:
                    pass

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)

        print(f"\n[OK] Conversion complete!")
        print(f"  - Converted: {converted}")
        print(f"  - Failed: {failed}")
        print(f"  - Output: {output_path}")
        print(f"  - Total accounts in file: {len(accounts)}")
    else:
        print(f"\n[WARNING] No accounts were converted")
        if failed > 0:
            print(f"  - Failed attempts: {failed}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert old .session files to new format using Kurigram"
    )
    parser.add_argument(
        "--source",
        "-s",
        default="sessions_base_format",
        help="Source directory with .json and .session files (default: 111)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="accounts.json",
        help="Output accounts.json file path (default: accounts.json)",
    )

    args = parser.parse_args()
    asyncio.run(convert_all_sessions(args.source, args.output))

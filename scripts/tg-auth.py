#!/usr/bin/env python3
"""
tg-auth.py — One-shot interactive Telethon authentication helper.

Run this ONCE on the VPS to authenticate the Telegram userbot session.
It prompts for your phone number and 2FA password, then saves the session
file to the specified path.

Usage:
    python3 scripts/tg-auth.py \
        --api-id 12345678 \
        --api-hash abc123def456 \
        --session-path /var/lib/tbc/session

Or via environment variables:
    TG_API_ID=12345678 TG_API_HASH=abc123def456 \
        python3 scripts/tg-auth.py --session-path /var/lib/tbc/session

After successful auth, the script prints instructions for encrypting
the session file with age. Keep your age key outside the repo.
"""

import argparse
import os
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authenticate Telethon userbot and save session file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--api-id",
        type=int,
        default=int(os.environ.get("TG_API_ID", 0)) or None,
        help="Telegram API ID (from my.telegram.org). Also: TG_API_ID env var.",
    )
    parser.add_argument(
        "--api-hash",
        type=str,
        default=os.environ.get("TG_API_HASH"),
        help="Telegram API hash (from my.telegram.org). Also: TG_API_HASH env var.",
    )
    parser.add_argument(
        "--session-path",
        type=str,
        default=os.environ.get("TG_SESSION_PATH", "/var/lib/tbc/session"),
        help="Path where the Telethon session file will be written (default: /var/lib/tbc/session).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.api_id:
        print(
            "ERROR: --api-id is required (or set TG_API_ID env var).\n"
            "       Get your API credentials at https://my.telegram.org/apps",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.api_hash:
        print(
            "ERROR: --api-hash is required (or set TG_API_HASH env var).\n"
            "       Get your API credentials at https://my.telegram.org/apps",
            file=sys.stderr,
        )
        sys.exit(1)

    # Import Telethon lazily so the script gives a clear error if not installed
    try:
        from telethon import TelegramClient
        from telethon.errors import (
            FloodWaitError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )
    except ImportError:
        print(
            "ERROR: telethon is not installed.\n"
            "       Run: uv pip install telethon",
            file=sys.stderr,
        )
        sys.exit(1)

    session_path = args.session_path
    # Telethon appends .session automatically if not present
    display_path = session_path if session_path.endswith(".session") else f"{session_path}.session"

    print(f"Authenticating Telegram session → {display_path}")
    print("You will be prompted for your phone number and 2FA password.\n")

    client = TelegramClient(session_path, args.api_id, args.api_hash)

    try:
        client.start()
    except PhoneCodeInvalidError:
        print("\nERROR: The phone code you entered was invalid. Please try again.", file=sys.stderr)
        sys.exit(1)
    except SessionPasswordNeededError:
        # client.start() handles 2FA interactively; this should not normally be raised here
        print("\nERROR: 2FA password required but not handled. Ensure 2FA is enabled.", file=sys.stderr)
        sys.exit(1)
    except FloodWaitError as exc:
        print(
            f"\nERROR: Telegram flood wait. Try again in {exc.seconds} seconds.",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        client.disconnect()

    print(f"\nSession saved to {display_path}")
    print()
    print("NEXT STEP — encrypt the session file before leaving it on disk:")
    print()
    print(f"    age -e -r <YOUR_AGE_PUBLIC_KEY> {display_path} > {display_path}.age")
    print()
    print("Store your age private key OUTSIDE the repo (e.g. local machine or a secrets manager).")
    print("The unencrypted .session file grants full access to your Telegram account — protect it.")
    print()
    print("To load the session on each service restart, add a pre-start ExecStartPre= that decrypts")
    print(f"    {display_path}.age → {display_path}")
    print("using the age key injected via the EnvironmentFile /etc/tbc/env.")


if __name__ == "__main__":
    main()

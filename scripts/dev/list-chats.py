"""Dev utility: list your top 20 Telegram chats and dump the last 20 messages of one.

Reads TBC_TG_API_ID / TBC_TG_API_HASH from .env and stores the Telethon session
in ./test_session (gitignored via *.session). Useful for sanity-checking that
the userbot credentials in .env actually work, separate from the ingestion service.

    cd <repo root>
    uv run python scripts/dev/list-chats.py
"""
import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["TBC_TG_API_ID"])
API_HASH = os.environ["TBC_TG_API_HASH"]
SESSION = "./test_session"

async def main() -> None:
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()

    dialogs = await client.get_dialogs(limit=20)
    print("\nYour chats:")
    for i, d in enumerate(dialogs):
        print(f"  {i}: {d.name}")

    idx = int(input("\nEnter number to read messages from: "))
    chat = dialogs[idx]
    print(f"\nLast 5 messages from '{chat.name}':")
    async for msg in client.iter_messages(chat, limit=20):
        sender = getattr(msg.sender, 'first_name', None) or getattr(msg.sender, 'title', 'Unknown')
        print(f"  [{msg.date}] {sender}: {msg.text or '<media>'}")

    await client.disconnect()

asyncio.run(main())

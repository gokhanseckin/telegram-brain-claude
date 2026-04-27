"""Test reading messages from a chat."""
import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["TBC_TG_API_ID"])
API_HASH = os.environ["TBC_TG_API_HASH"]
SESSION = "./test_session"

async def main():
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

#!/usr/bin/env python3
"""Import a Telegram Desktop export (result.json) into the Supabase DB.

Usage:
    python scripts/import-tg-export.py /path/to/DataExport/result.json

Environment:
    DATABASE_URL  — PostgreSQL DSN (falls back to .env in repo root)

The script is idempotent: it uses ON CONFLICT DO NOTHING for messages and
ON CONFLICT DO UPDATE for chats/users so re-running is safe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Minimal env setup (load .env before importing tbc_common)
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Best-effort .env load without requiring python-dotenv."""
    repo_root = Path(__file__).resolve().parent.parent
    for candidate in [repo_root / ".env", repo_root / ".env.local"]:
        if candidate.exists():
            with candidate.open() as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
            break

_load_dotenv()

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from tbc_common.db.models import Chat, Message, User


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHAT_TYPE_MAP: dict[str, str] = {
    "personal_chat": "private",
    "bot_chat": "private",
    "saved_messages": "private",
    "private_group": "group",
    "private_supergroup": "supergroup",
    "public_supergroup": "supergroup",
    "private_channel": "channel",
    "public_channel": "channel",
}

BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(raw_text: Any) -> str | None:
    """Flatten Telegram's text field (str or list of str/entity dicts)."""
    if raw_text is None:
        return None
    if isinstance(raw_text, str):
        return raw_text or None
    if isinstance(raw_text, list):
        parts: list[str] = []
        for part in raw_text:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(part.get("text", ""))
        result = "".join(parts)
        return result or None
    return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_user_id(from_id: str | None) -> int | None:
    """Extract numeric user_id from 'user123456' or 'channel123456'."""
    if not from_id:
        return None
    for prefix in ("user", "channel"):
        if from_id.startswith(prefix):
            try:
                return int(from_id[len(prefix):])
            except ValueError:
                return None
    try:
        return int(from_id)
    except ValueError:
        return None


def _split_name(full_name: str | None) -> tuple[str | None, str | None]:
    if not full_name:
        return None, None
    parts = full_name.strip().split(" ", 1)
    first = parts[0] or None
    last = parts[1] if len(parts) > 1 else None
    return first, last


# ---------------------------------------------------------------------------
# Core import logic
# ---------------------------------------------------------------------------

def import_export(export_path: Path, database_url: str, dry_run: bool = False) -> None:
    print(f"Loading {export_path} …")
    with export_path.open(encoding="utf-8") as f:
        data = json.load(f)

    # Determine self user from personal_information block
    personal_info: dict[str, Any] = data.get("personal_information", {})
    self_user_id: int | None = None
    raw_self_id = personal_info.get("user_id")
    if raw_self_id:
        try:
            self_user_id = int(raw_self_id)
        except (ValueError, TypeError):
            pass

    chats_list: list[dict[str, Any]] = data.get("chats", {}).get("list", [])
    print(f"Found {len(chats_list)} chats")

    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    # Collect all unique users across all chats before inserting
    users: dict[int, dict[str, Any]] = {}
    if self_user_id:
        first, last = _split_name(
            f"{personal_info.get('first_name', '')} {personal_info.get('last_name', '')}".strip() or None
        )
        users[self_user_id] = {
            "user_id": self_user_id,
            "first_name": first,
            "last_name": last,
            "username": personal_info.get("username"),
            "is_self": True,
        }

    chat_rows: list[dict[str, Any]] = []
    message_rows: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []  # (chat_row, msgs)

    for chat in chats_list:
        chat_id: int = chat["id"]
        chat_type_raw: str = chat.get("type", "")
        chat_type = CHAT_TYPE_MAP.get(chat_type_raw, "group")

        chat_rows.append({
            "chat_id": chat_id,
            "type": chat_type,
            "title": chat.get("name"),
            "username": None,  # not exported by TG Desktop
        })

        messages: list[dict[str, Any]] = chat.get("messages", [])
        message_rows.append((chat_id, messages))

        # Collect users from messages
        for msg in messages:
            if msg.get("type") != "message":
                continue
            from_id_raw = msg.get("from_id")
            user_id = _parse_user_id(from_id_raw)
            if user_id is None:
                continue
            # Only track human users, not channels-as-senders
            from_id_str = str(from_id_raw or "")
            if from_id_str.startswith("channel"):
                continue
            if user_id not in users:
                first, last = _split_name(msg.get("from"))
                users[user_id] = {
                    "user_id": user_id,
                    "first_name": first,
                    "last_name": last,
                    "username": None,  # not available in export
                    "is_self": user_id == self_user_id,
                }

    print(f"Unique users: {len(users)}")
    total_msgs = sum(len(msgs) for _, msgs in message_rows)
    print(f"Total messages (including service): {total_msgs}")

    if dry_run:
        print("[dry-run] No changes written.")
        return

    with Session() as session:
        # --- Upsert users ---
        if users:
            stmt = pg_insert(User.__table__).values(list(users.values()))
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_id"],
                set_={
                    "first_name": stmt.excluded.first_name,
                    "last_name": stmt.excluded.last_name,
                    "is_self": stmt.excluded.is_self,
                },
            )
            session.execute(stmt)
            session.flush()
            print(f"Upserted {len(users)} users")

        # --- Upsert chats ---
        if chat_rows:
            stmt = pg_insert(Chat.__table__).values(chat_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["chat_id"],
                set_={
                    "type": stmt.excluded.type,
                    "title": stmt.excluded.title,
                },
            )
            session.execute(stmt)
            session.flush()
            print(f"Upserted {len(chat_rows)} chats")

        # --- Insert messages in batches ---
        inserted = 0
        skipped = 0
        batch: list[dict[str, Any]] = []

        def _flush_batch() -> None:
            nonlocal inserted
            if not batch:
                return
            stmt = pg_insert(Message.__table__).values(batch)
            stmt = stmt.on_conflict_do_nothing(index_elements=["chat_id", "message_id"])
            result = session.execute(stmt)
            session.flush()
            inserted += result.rowcount

        for chat_id, messages in message_rows:
            for msg in messages:
                if msg.get("type") != "message":
                    skipped += 1
                    continue

                from_id_raw = msg.get("from_id")
                sender_id: int | None = None
                from_id_str = str(from_id_raw or "")
                if not from_id_str.startswith("channel"):
                    sender_id = _parse_user_id(from_id_raw)

                text = _extract_text(msg.get("text"))
                sent_at = _parse_dt(msg.get("date"))
                if sent_at is None:
                    skipped += 1
                    continue

                batch.append({
                    "message_id": msg["id"],
                    "chat_id": chat_id,
                    "sender_id": sender_id,
                    "sent_at": sent_at,
                    "text": text,
                    "reply_to_id": msg.get("reply_to_message_id"),
                    "edited_at": _parse_dt(msg.get("edited")),
                    "deleted_at": None,
                    "raw": msg,
                })

                if len(batch) >= BATCH_SIZE:
                    _flush_batch()
                    batch.clear()
                    print(f"  … {inserted} messages inserted so far", end="\r")

        _flush_batch()
        session.commit()

    print(f"\nDone. Inserted: {inserted} messages, skipped (service/bad): {skipped}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Import Telegram Desktop export into Supabase")
    parser.add_argument("export_path", type=Path, help="Path to result.json from TG Desktop export")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL DSN (default: $DATABASE_URL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and count without writing anything",
    )
    args = parser.parse_args()

    if not args.export_path.exists():
        print(f"Error: {args.export_path} not found", file=sys.stderr)
        sys.exit(1)

    if not args.database_url:
        print("Error: DATABASE_URL not set and --database-url not provided", file=sys.stderr)
        sys.exit(1)

    import_export(args.export_path, args.database_url, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

# Changelog

## 2026-04-23 — Initial backfill hardening

Three changes to make the one-time onboarding backfill actually work end-to-end on first deploy. PRs landed in this order:

### feat: 6-month initial backfill with 500-msg per-chat cap ([#15](https://github.com/gokhanseckin/telegram-brain-claude/pull/15))
`services/ingestion/tbc_ingestion/initial_backfill.py`

- Window widened from 30 days to **6 months (180 days)**.
- New per-chat hard cap of **500 messages**, applied only during the initial onboarding pass. Live ingestion and subsequent gap recovery are unaffected.
- For each dialog enumerated via `client.iter_dialogs()`:
  1. Broadcast channels and public supergroups (`username IS NOT NULL`) are excluded via `_is_excluded_chat` (unchanged).
  2. If `dialog.date` (timestamp of the latest message) is older than the 6-month cutoff, the dialog is **skipped entirely** — no `chats` row created, no messages fetched. The live handler will create a row if and when a new message arrives, so `/tag` only ever asks about chats that are actually active.
  3. Otherwise the chat is upserted and messages are paged backwards until either the cutoff or the 500-message cap is hit (whichever comes first).
- Startup notification text updated to reflect the new window.

### fix: JSON-sanitize backfill message payload ([#14](https://github.com/gokhanseckin/telegram-brain-claude/pull/14))
`services/ingestion/tbc_ingestion/gap_recovery.py`

The shared `_store_messages` (used by both gap recovery and initial backfill) stored `msg.to_dict()` raw into the JSONB `raw` column. Telethon dicts contain `datetime` and `bytes` values, which JSONB can't serialize, so every batch commit raised `TypeError: Object of type datetime is not JSON serializable`. Fix #8 applied the same class of sanitization to the live handler path but it had never propagated here. Reused the existing `_make_json_safe` helper from `handlers.py`. 920 datetime errors and 1 bytes error in production logs before the fix.

### fix: two-phase sender resolution ([#16](https://github.com/gokhanseckin/telegram-brain-claude/pull/16))
`services/ingestion/tbc_ingestion/gap_recovery.py`

After #14 landed, the backfill surfaced a second failure mode: `ForeignKeyViolation: messages_sender_id_fkey`. The original code added `User` and `Message` rows to the same session inside a `for` loop, relying on SQLAlchemy's autoflush to commit the User before the Message's FK check. In practice the autoflush ordering raced, and the Message insert ran before the User had landed.

Rewrote `_store_messages` as two phases per batch:
  1. Collect unique `sender_id`s from the batch, look up which ones already exist, resolve the missing ones via `client.get_entity`, and **commit** those new `User` rows in their own session.
  2. Open a second session and insert the `Message` rows, setting `sender_id=NULL` as a fallback for any sender whose `get_entity` call failed.

The live handler path (`_handle_new_message`) is unchanged — it already resolves the sender from the event entity and commits per-message.

### Operational notes

- `service_state.initial_backfill_done_at` was already set from an earlier failed run, so after each fix we had to reset it manually (`UPDATE service_state SET initial_backfill_done_at = NULL, initial_backfill_started_at = NULL WHERE id = 1;`) and `systemctl restart tbc-ingestion` to re-trigger the backfill. This is fine for a one-time migration; no code change needed.
- After all three fixes, backfill completed without `dialog_failed` errors; owner receives a DM on completion.

# Changelog

## 2026-05-02 — Understanding decoupled from cron, coupled to brief

The understanding worker no longer runs the LLM (Gemma) pass on a 5-second polling loop. Instead the heavy LLM pass is now **step 1 of every brief** — both the 07:00 cron and the on-demand `/brief` trigger drain the LLM-understanding queue first, then generate the brief. This guarantees every brief reflects the latest messages without paying for LLM calls between briefs. Embeddings (`bge-m3`) stay real-time via a lightweight 5-second loop so semantic_search / MCP tools remain fresh between briefs.

### What changed

- **New mode env**: `TBC_UNDERSTANDING_MODE=brief-coupled` (default) | `continuous`. The continuous mode preserves the legacy 5s LLM polling loop as a fallback.
- **worker-understanding** in brief-coupled mode runs two concurrent loops:
  - `embed_loop` (every 5s) — embeds new messages into `bge-m3`, writes partial rows at `model_version="embeddings-only-<YYYY-MM-DD>"`.
  - `trigger_watcher` (every 30s) — drains the LLM queue when `/tmp/tbc_trigger_understanding` appears.
- **worker-brief** wraps `run_brief()` in `run_brief_with_drain()` — counts pending LLM-understanding rows, touches `/tmp/tbc_trigger_understanding`, polls until the queue is empty (or `TBC_BRIEF_PRE_UNDERSTANDING_TIMEOUT_S` fires, default 300s), then generates the brief. Used by both the cron-fired and trigger-fired briefs.
- **Shared queue helpers**: `tbc_common.db.understanding_queue.pending_understanding_rows / pending_understanding_count` consolidate the SQL previously inline in worker-understanding's `_poll`.
- **v13 untouched**: prompt rules, `MODEL_VERSION`, `process_message_batch`, the `_sanitize_recipient` guard in worker-commitments — all byte-identical. Only orchestration changed.

### To revert

`TBC_UNDERSTANDING_MODE=continuous` + restart `tbc-worker-understanding`. The brief drain helper still works — it just sees the queue at 0 immediately because the continuous loop is keeping up in real time.

---

## 2026-05-01 / 2026-05-02 — Understanding worker quality pass (v1 → v13)

13 `MODEL_VERSION` iterations on the understanding worker to drive commitment quality from ~50% precision (Qwen 7B) to **0 banned-phrase noun-placeholder leaks** on Gemma 4 26B (4B-active MoE via [novita.ai](https://novita.ai)). Provider stack and architecture stabilised; full session log in [`docs/session-2026-05-01-understanding-worker-iteration.md`](docs/session-2026-05-01-understanding-worker-iteration.md).

### How the understanding worker runs (current)

`services/worker-understanding/tbc_worker_understanding/`

- **Provider:** novita.ai, model `google/gemma-4-26b-a4b-it`. Selected by `TBC_UNDERSTANDING_PROVIDER=novita` + `TBC_UNDERSTANDING_NOVITA_MODEL`. Routing in `ollama_client.py` supports `ollama|deepseek|novita`.
- **Batching:** chat-aware (`main.py:_assemble_chat_aware_batch`). Polls a 3-day window. If any chat has ≥20 pending messages, the entire batch comes from that one chat (coherent thread). Otherwise round-robin across the top 3 chats, each emitted as a contiguous block. Defaults: `BATCH_MAX_N=20`, `MAX_CHATS_PER_BATCH=3`, `BATCH_CHAR_BUDGET=60000`.
- **Prompt structure** (`packages/common/tbc_common/prompts/understanding.py` + `processor.py:process_message_batch`):
  - System prompt opens with a **WHY section** — explains the brief-generation purpose, that false commitments cost the user real time, "precision over recall", "default `is_commitment=false`".
  - User prompt = `BATCH OVERVIEW` (chat count + cross-chat warning if >1) → per-chat blocks with title / type / tag headers and a **speaker registry** (`YOU = Gokhan` + others) → enumerated messages with prior context (7 msgs) and a per-message **REMINDER** footer that names the formatting requirement adjacent to the input.
  - The **COMMITMENT.WHAT formatting block** sits at the END of the system prompt (recency bias) with banned-phrase list, GOOD vs BAD examples, and the literal `(recipient unclear from context)` escape hatch.
- **Output:** single envelope `{"results": [{...}, ...]}` with each result echoing its `id` for matching. Schema fields all default-able for resilience against the model dropping fields on big batches (`schema.py`).

### How commitments are extracted (current)

`services/worker-commitments/tbc_worker_commitments/extractor.py`

- Polls `message_understanding` for rows where `is_commitment=true` and no commitment yet exists for `(chat_id, source_message_id)`.
- **Programmatic banned-phrase guard** (`_sanitize_recipient`) — runs BEFORE the confidence gate:
  - Detects noun-phrase placeholders the LLM occasionally still emits: `"the recipient"`, `"the relevant person"`, `"the intended recipient"`, `"the person"`, `"the user"`, `"someone"`. Pronouns (`him`/`her`/`them`) deliberately excluded — they're valid with a local antecedent in the same sentence.
  - Hits → rewrites to `(recipient unclear from context)` and **drops confidence by 1**. Hybrid case (LLM wrote both banned phrase AND marker) — strips the banned phrase so we don't end up with two markers.
  - Net effect: high-conf leaks survive but with a deterministic review marker; low-conf leaks (≥4 → 3) fall below the threshold and never become commitments.
- **Confidence gate:** `confidence < 4 → drop`. Anything below is logged as `commitment_skipped_low_confidence` and never reaches the brief.
- Sanitiser firings are logged as `banned_recipient_phrase_sanitized` for observability.

### Final v13 quality

- **5 active commitments** generated under v13 on the post-cleanup 3-day window, **0 banned-phrase leaks** at the LLM level. The one hybrid-leak case (`"tell the person (recipient unclear from context) immediately"`) was caught by the sanitiser and dropped below the conf threshold.
- The two worst v12 failure cases (Turkish acks `"Tamamdır söylüyorum"` / `"Hemen iletiyorum"` — generic "I'm telling / forwarding" with antecedent beyond the visible window) are now correctly classified as `is_commitment=false` thanks to the new ack-style rule (`"if you have to infer the deliverable from prior context, set is_commitment=false"`).
- 9 stale v8/v11/v12 commitment rows removed in two batches (per [`CLAUDE.md`](CLAUDE.md) data-deletion confirmation rule) so the brief sees only v13 verdicts.

### What's exhausted on Gemma 4

The prompt-side toolbox is fully spent — anything left points at the model. If quality regresses or new failure modes appear, escalate via: prior context window 7 → 10 (env), batch size 20 → 15, Gemma 4 31B (heavier MoE), or Sonnet for understanding (~25× cost but volume is low). The deterministic `_sanitize_recipient` guard is permanent regardless of model — extend `_BANNED_RECIPIENT_PHRASES` if new generic placeholders show up.

---

## 2026-04-23 — CI hygiene: lint-and-test green for the first time

### ci: fix mypy strict + pytest collection on main ([#22](https://github.com/gokhanseckin/telegram-brain-claude/pull/22))

`CI / lint-and-test` had been red on every push to `main` since the project started. Deploy-to-VPS is a separate workflow and succeeds, so prod kept shipping regardless — this was pure hygiene. The root cause was a pair of duplicate-module errors that blocked `mypy` before it could finish, masking the real type errors, and the same collision broke `pytest` collection:

- Every `services/*/tests/__init__.py` resolved to module name `tests`.
- `services/worker-radar/conftest.py` and `services/worker-commitments/conftest.py` both resolved to module name `conftest`.

**Config (`pyproject.toml`):**
- `[tool.mypy]` `exclude = ['(^|/)tests/', '(^|/)conftest\.py$']` — tests don't need strict typing and share names across services; source code stays `strict = true`.
- `[tool.pytest.ini_options]` `addopts = "--import-mode=importlib"` — the standard fix for duplicate test package names in a monorepo ([pytest docs](https://docs.pytest.org/en/stable/explanation/goodpractices.html#tests-outside-application-code)).

**Source fixes (45 real mypy errors across 8 files, all targeted — strict stays on, no blanket suppressions):**
- `services/worker-brief/tbc_worker_brief/assembler.py`: `.where(expr if cond else True)` → guarded `if`.
- `services/worker-brief/tbc_worker_brief/sender.py`, `services/worker-weekly/tbc_worker_weekly/sender.py`: Anthropic SDK's `response.content[0]` is a big union that doesn't guarantee `.text` — wrapped with `cast(TextBlock, …)` / `cast(BetaTextBlock, …)`.
- `services/ingestion/tbc_ingestion/handlers.py`: narrow `# type: ignore[untyped-decorator]` on the three Telethon `@client.on(...)` decorators.
- `services/tg-bot/tbc_bot/guards.py`: wrapped return in `bool(...)`.
- `services/tg-bot/tbc_bot/handlers/onboarding.py`, `services/tg-bot/tbc_bot/handlers/chat.py`, `services/tg-bot/tbc_bot/agent.py`: `list[dict]` → `list[dict[str, Any]]`.
- `services/mcp-server/tbc_mcp_server/auth.py`: properly typed `BaseHTTPMiddleware.dispatch` (`call_next: RequestResponseEndpoint`, `-> Response`); dropped stale `# type: ignore[override]`.
- `services/mcp-server/tbc_mcp_server/main.py`: `cast(Session, get_sessionmaker()())`; narrow `# type: ignore[untyped-decorator, no-untyped-call]` on MCP SDK decorators; renamed five reassigned `results` locals so mypy doesn't narrow to the first branch's return type.

No test files or CI workflow files were modified; the "Prune missing workspace members" step is still a no-op on `main` and stays untouched.

Verified locally and in CI: `ruff` clean, `mypy` "no issues found in 64 source files", `pytest -m "not real_ollama" -q` → 65 passed.

## 2026-04-23 — Mobile Claude agent via Telegram bot

Three PRs to wire the Telegram bot as a mobile prompting interface for telegram-brain data.

### feat(bot): free-text DM handler with Claude + MCP agent ([#18](https://github.com/gokhanseckin/telegram-brain-claude/pull/18))
`services/tg-bot/tbc_bot/agent.py` (new), `services/tg-bot/tbc_bot/handlers/chat.py` (new), `services/tg-bot/tbc_bot/main.py`, `services/tg-bot/pyproject.toml`

DM the bot any free-text question from mobile → Claude (claude-sonnet-4-6) calls MCP tools via the Anthropic `mcp-client-2025-04-04` beta → reply sent back. Runs inside the existing `tbc-bot` process, no new service needed.

- `agent.py`: `ask(history, text)` calls `AsyncAnthropic.beta.messages.create` with `mcp_servers` pointing at `TBC_MCP_PUBLIC_URL/mcp`, authenticated by `TBC_MCP_BEARER_TOKEN`.
- `handlers/chat.py`: catch-all `F.text` handler (owner-gated). Per-chat in-memory history (last 10 turns). `/reset` clears it. Splits replies >4096 chars.
- Chat router registered last so existing commands (`/brief`, `/status`, etc.) still take priority.

### fix(mcp-server): mount /mcp as ASGI app instead of Route handler ([#19](https://github.com/gokhanseckin/telegram-brain-claude/pull/19))
`services/mcp-server/tbc_mcp_server/main.py`

`Route("/mcp", handle_mcp)` was calling `handle_mcp(request)` — wrong calling convention for an ASGI callable. Replaced with `Mount("/mcp", app=handle_mcp)`. This revealed a second routing issue (see #20).

### fix(mcp-server): replace Mount routing with custom _Router for /mcp path ([#20](https://github.com/gokhanseckin/telegram-brain-claude/pull/20))
`services/mcp-server/tbc_mcp_server/main.py`

Starlette's `Mount("/mcp")` compiles regex `^/mcp/(?P<path>.*)$` — matches `/mcp/` but **not** `/mcp`. Anthropic's remote MCP connector sends `POST /mcp` (no trailing slash), so every request fell through to FastAPI → 404.

Replaced with `Mount("") + _Router`. `Mount("")` passes the original full path to the child app without stripping. `_Router` checks `scope["path"]` directly: `/mcp` and `/mcp/*` → session manager, everything else → FastAPI.

---

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

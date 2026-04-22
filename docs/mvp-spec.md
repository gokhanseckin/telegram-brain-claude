# Telegram Business Brain — MVP Spec

A reference for Claude Code. Describes intent, scope, architecture, data, prompts, and constraints. Claude Code plans the build order and writes the code.

---

## 1. Product intent

The user conducts ~90% of their business on Telegram — sales, BD, account management — across DMs and group chats with clients, prospects, and colleagues. Important information is lost: forgotten replies, missed opportunities, unshipped proposals, patterns invisible at message-level that matter at portfolio-level.

This product is a **second brain for Telegram-driven business**. It continuously reads the user's Telegram history, understands what matters, and surfaces what the user would miss on their own. Two modes of interaction:

- **Proactive**: a Morning Brief delivered daily via Telegram bot, and a Weekly Review. The brief answers *"what should I pay attention to today"* — missed opportunities and bird's-eye oversight first, forgotten replies second.
- **Reactive**: Ask Anything against the full corpus via an MCP custom connector in the user's Claude Pro/Max account.

Success criterion for v1: after 2 weeks of use, the user identifies at least one real opportunity or risk they would have missed, directly from a Brief or Radar alert.

**The product is not a CRM alternative.** It maintains its own lightweight relationship state per chat, but the user may or may not use an external CRM — the product does not integrate with one and does not try to be one.

---

## 2. v1 feature scope

**In scope:**
1. Telegram ingestion via MTProto userbot (Telethon), user's own account
2. Chat curation (tagging) onboarding flow via Telegram bot
3. Per-message **understanding pass** producing structured JSON (local Qwen 2.5 7B)
4. Per-message **embeddings** (local bge-m3) indexed in pgvector
5. **Radar**: signal detection + aggregation into opportunity/risk alerts
6. **Commitment tracking**: extracted promises (yours and theirs), open/closed state
7. **Relationship state**: inferred lightweight pipeline stage per tagged chat
8. **Morning Brief**: scheduled daily Telegram message (Sonnet 4.6 via API)
9. **Weekly Review**: scheduled weekly Telegram message (Sonnet 4.6 via API, batch)
10. **Ask Anything**: MCP server consumed by user's Claude Pro/Max via custom connector
11. **Feedback loop**: user can mark brief items as "not useful" / "you missed this" to tune Radar

**Explicitly out of scope for v1:**
- Web dashboard (deferred — Telegram-only UI)
- Multi-user support (single-user product)
- Secret chats (MTProto can't access these by design)
- Media ingestion beyond file references (no image/video understanding)
- Voice message transcription
- External CRM sync
- Email/Meet/phone integration
- Drafting replies or sending messages on user's behalf
- Auto-scheduling, calendar integration
- Sentiment graphs, analytics dashboards

---

## 3. Architecture

Single Hetzner CCX23 (4 dedicated vCPU, 16GB RAM, 80GB NVMe, Ubuntu LTS). All services co-located. Backups via `pg_dump` to a Hetzner Storage Box.

**Services on the box (all systemd units):**
- `ingestion` — Telethon client, persistent connection, three handlers: `NewMessage`, `MessageEdited`, `MessageDeleted`. On startup, reconciles gaps per dialog via `get_messages` pagination.
- `ollama` — serves Qwen 2.5 7B Instruct (Q4_K_M) and bge-m3. Loopback only.
- `worker-understanding` — polls `messages` for unprocessed rows; runs understanding pass; writes structured fields + embedding.
- `worker-radar` — scans new understanding outputs; aggregates signals; writes to `radar_alerts`.
- `worker-brief` — scheduled daily; builds brief input from last 24h + open items; calls Sonnet 4.6 API; posts to Telegram.
- `worker-weekly` — scheduled weekly; batch API call; posts to Telegram.
- `tg-bot` — aiogram bot, serves user commands and onboarding. Loopback → Caddy on TLS if webhook mode; polling mode also fine for single user.
- `mcp-server` — FastAPI + `mcp` SDK, exposes read-only tools over Streamable HTTP. Behind Caddy with TLS and OAuth.
- `caddy` — reverse proxy, Let's Encrypt, firewall allowlists Anthropic's published IP ranges for the MCP endpoint only.
- `postgres` — with `pgvector` and `pg_trgm` extensions.

**External dependencies:**
- Claude API (Sonnet 4.6) for Brief + Weekly Review only. Prompt caching on brief system prompt; batch API for weekly.
- Claude Pro/Max subscription for interactive Ask Anything via MCP.

**Non-obvious architectural decisions:**
- **Why local Qwen 7B for understanding, not API**: ~500 msgs/day × understanding call is enough volume that local is cheaper; quality is sufficient for structured JSON extraction; keeps message content on-box.
- **Why Sonnet API for Brief, not local model**: Brief is the user's daily touchpoint, judgment-heavy synthesis — quality difference between Sonnet and Qwen 72B matters here and cost is trivial (~$1/mo cached).
- **Why MCP for Ask Anything, not API**: interactive by nature, uses user's existing Pro/Max subscription — zero incremental cost, full Claude app UX.
- **Why self-hosted Postgres, not Supabase**: single-box simplicity, no third-party dependency, backups to Hetzner Storage Box suffice at this scale.
- **Why Telethon (userbot), not Bot API**: Bot API only sees messages sent to a bot; we need the user's full dialog stream.

---

## 4. Data model

Postgres 16 with `pgvector` and `pg_trgm`. All timestamps are `TIMESTAMPTZ`. IDs are Telegram's native IDs where applicable.

```sql
-- Chats the user participates in; tagged during onboarding or on first sight.
CREATE TABLE chats (
  chat_id        BIGINT PRIMARY KEY,           -- Telegram chat ID
  type           TEXT NOT NULL,                -- 'private' | 'group' | 'supergroup' | 'channel'
  title          TEXT,
  username       TEXT,
  tag            TEXT,                         -- 'client' | 'prospect' | 'colleague' | 'personal' | 'ignore' | NULL
  tag_set_at     TIMESTAMPTZ,
  notes          TEXT,                         -- user-supplied: who this is, what matters
  participant_count INT,
  created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Known Telegram users (senders across all chats).
CREATE TABLE users (
  user_id        BIGINT PRIMARY KEY,
  first_name     TEXT,
  last_name      TEXT,
  username       TEXT,
  notes          TEXT,                         -- user-supplied identity context
  is_self        BOOLEAN DEFAULT FALSE
);

-- Every kept message. Raw Telegram object preserved in `raw`.
CREATE TABLE messages (
  message_id     BIGINT,
  chat_id        BIGINT REFERENCES chats,
  sender_id      BIGINT REFERENCES users,
  sent_at        TIMESTAMPTZ NOT NULL,
  text           TEXT,
  reply_to_id    BIGINT,
  edited_at      TIMESTAMPTZ,
  deleted_at     TIMESTAMPTZ,                  -- soft delete; we retain history of deletions
  raw            JSONB NOT NULL,               -- full Telethon message
  PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX ON messages (sent_at DESC);
CREATE INDEX ON messages USING GIN (to_tsvector('simple', text));

-- Understanding pass output, 1:1 with messages (only for kept/tagged chats).
CREATE TABLE message_understanding (
  chat_id        BIGINT,
  message_id     BIGINT,
  processed_at   TIMESTAMPTZ DEFAULT NOW(),
  model_version  TEXT NOT NULL,                -- so we can reprocess when prompts change
  language       TEXT,                         -- 'en' | 'tr' | etc.
  entities       JSONB,                        -- [{type, value, normalized_en}]
  intent         TEXT,                         -- 'question' | 'commitment' | 'update' | 'smalltalk' | ...
  is_directed_at_user BOOLEAN,                 -- message addressed to user, expects reply
  is_commitment  BOOLEAN,
  commitment     JSONB,                        -- {who, what, due, confidence} when is_commitment
  is_signal      BOOLEAN,
  signal_type    TEXT,                         -- 'buying' | 'risk' | 'expansion' | 'competitor' | 'referral' | 'cooling' | ...
  signal_strength SMALLINT,                    -- 1..5
  sentiment_delta SMALLINT,                    -- -2..+2 vs chat baseline
  summary_en     TEXT,                         -- short normalized-English gloss
  embedding      VECTOR(1024),                 -- bge-m3, multilingual
  PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX ON message_understanding USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON message_understanding (is_signal, signal_strength DESC) WHERE is_signal;
CREATE INDEX ON message_understanding (is_directed_at_user) WHERE is_directed_at_user;

-- Commitments extracted across messages, tracked to closure.
CREATE TABLE commitments (
  id             BIGSERIAL PRIMARY KEY,
  chat_id        BIGINT,
  source_message_id BIGINT,
  owner          TEXT NOT NULL,                -- 'user' | 'counterparty'
  description    TEXT NOT NULL,
  due_at         TIMESTAMPTZ,
  created_at     TIMESTAMPTZ DEFAULT NOW(),
  resolved_at    TIMESTAMPTZ,
  resolved_by_message_id BIGINT,
  status         TEXT DEFAULT 'open'           -- 'open' | 'fulfilled' | 'stale' | 'dismissed'
);

-- Radar alerts: aggregated signals surfaced to the user.
CREATE TABLE radar_alerts (
  id             BIGSERIAL PRIMARY KEY,
  chat_id        BIGINT,
  alert_type     TEXT NOT NULL,                -- mirrors signal_type vocabulary
  severity       SMALLINT,                     -- 1..5
  title          TEXT,
  reasoning      TEXT,                         -- why this was flagged; cites message_ids
  supporting_message_ids JSONB,                -- array of {chat_id, message_id}
  created_at     TIMESTAMPTZ DEFAULT NOW(),
  surfaced_in_brief_at TIMESTAMPTZ,
  user_feedback  TEXT,                         -- 'useful' | 'not_useful' | NULL
  feedback_note  TEXT
);

-- Inferred relationship state per tagged chat.
CREATE TABLE relationship_state (
  chat_id        BIGINT PRIMARY KEY,
  stage          TEXT,                         -- 'prospecting' | 'qualifying' | 'proposal' | 'negotiation' | 'active' | 'dormant' | 'churned'
  stage_confidence SMALLINT,                   -- 1..5
  last_meaningful_contact_at TIMESTAMPTZ,
  last_user_message_at TIMESTAMPTZ,
  last_counterparty_message_at TIMESTAMPTZ,
  temperature    TEXT,                         -- 'warming' | 'stable' | 'cooling'
  open_threads   JSONB,                        -- array of {topic, last_mentioned_at}
  user_override  JSONB,                        -- fields user has manually corrected
  updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Per-chat rolling summaries used by the Brief and MCP tools.
CREATE TABLE chat_summaries (
  id             BIGSERIAL PRIMARY KEY,
  chat_id        BIGINT,
  period         TEXT NOT NULL,                -- 'day' | 'week'
  period_start   DATE NOT NULL,
  summary        TEXT NOT NULL,
  key_points     JSONB,
  generated_at   TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (chat_id, period, period_start)
);

-- User feedback on briefs (tunes Radar over time).
CREATE TABLE brief_feedback (
  id             BIGSERIAL PRIMARY KEY,
  brief_date     DATE NOT NULL,
  item_ref       TEXT,                         -- alert id or commitment id etc.
  feedback       TEXT NOT NULL,                -- 'useful' | 'not_useful' | 'missed_important'
  note           TEXT,
  created_at     TIMESTAMPTZ DEFAULT NOW()
);
```

**Model versioning rule**: whenever a prompt for the understanding pass changes, bump `model_version`. A reprocessing command should re-run the understanding pass on all messages where `model_version` differs from current. Don't silently mix outputs from different prompt versions.

**Noise filter rule**: messages from chats with `tag = 'ignore'` or `tag IS NULL` are stored in `messages` but not processed into `message_understanding`. Tagging a chat later triggers backfill for that chat.

---

## 5. Core prompts

These are the crafted content of the system. Claude Code should use them as-is rather than inventing its own. All three run on Sonnet 4.6 except the understanding pass which runs on local Qwen 2.5 7B.

### 5.1 Understanding pass (Qwen 2.5 7B, per-message)

System prompt anchors the model on structured JSON output. Input is one message plus minimal context (chat tag, last 3 messages in thread, sender identity).

```
You are an analyst processing business messages from Telegram. The user runs sales,
business development, and account management — ~90% of business happens on Telegram.

Your job: read ONE message and emit a single JSON object describing it. Both English
and Turkish messages occur; normalize entities and summary to English.

Output schema (return ONLY this JSON, no prose):
{
  "language": "en" | "tr" | "mixed" | "other",
  "entities": [{"type": "person|company|product|money|date|location|competitor", "value": "...", "normalized_en": "..."}],
  "intent": "question|commitment|update|objection|request|smalltalk|announcement|other",
  "is_directed_at_user": bool,
  "is_commitment": bool,
  "commitment": null | {"who": "user|counterparty", "what": "...", "due": "YYYY-MM-DD or null", "confidence": 1-5},
  "is_signal": bool,
  "signal_type": null | "buying|risk|expansion|competitor|referral|cooling|budget|timeline|decision_maker|pricing_objection|champion_exit|other",
  "signal_strength": null | 1-5,
  "sentiment_delta": -2..+2,
  "summary_en": "one sentence, <=25 words"
}

Rules:
- is_directed_at_user: true if message asks the user something or clearly addresses them.
- Commitments are explicit ("I will send X by Friday"). Vague intentions are not commitments.
- Signals require evidence in the message; do not speculate. Low confidence → signal_strength 1-2.
- Treat channel announcements and group spam as is_signal=false, intent="announcement".
- Normalize Turkish entity values to English in normalized_en (e.g., "Türkiye" → "Turkey").
- summary_en captures what happened, not what the message says literally.
```

### 5.2 Morning Brief (Sonnet 4.6, daily, prompt-cached)

Input assembled by `worker-brief`:
- Cached block (stable): system prompt, user's chat tags and notes, signal taxonomy, brief format spec.
- Fresh block (varies daily): last 24h of `message_understanding` rows filtered to non-ignored chats, open `commitments`, today's `radar_alerts`, current `relationship_state` deltas vs last week, yesterday's `brief_feedback`.

System prompt:

```
You write the user's daily Morning Brief. The user runs sales, BD, and account
management, mostly on Telegram. The user's top priorities are spotting missed
opportunities and maintaining bird's-eye oversight of their business — not just
inbox triage.

Write a brief to be read in Telegram on a phone before the day starts. Five
sections, in this order:

1. 🎯 OPPORTUNITIES & RISKS
   Lead with this. Surface buying signals, expansion openings, referral moments,
   cooling relationships, competitive threats. 3-7 items. Each item: one sharp
   sentence stating the signal, then one sentence on what to do. Cite chat name.

2. ⏳ YOU OWE
   Replies the user missed and commitments the user made that are open.
   Rank by relationship value × age. Include chat name and what's owed.

3. 📬 THEY OWE YOU
   Things others committed to the user that haven't landed. Flag chase-worthy ones.

4. 📊 PORTFOLIO MOVEMENT
   Cross-chat patterns: who's warming, who's cooling, recurring themes across
   multiple clients. This is the bird's-eye layer — use it to name things the
   user wouldn't see at message-level.

5. 🧭 TODAY'S FOCUS
   One paragraph. If the user only does three things today, what are they.

Style:
- Direct, no fluff, no restating the obvious.
- Name chats and people specifically. The user knows them.
- If a section has nothing meaningful, write "Nothing notable." Do not invent items.
- Respect user feedback from prior briefs: if items like X were marked "not useful",
  don't surface similar items; if the user said "you missed Y", weight that pattern higher.
- Max length: fits comfortably in a single Telegram message (~3000 chars).
```

### 5.3 Weekly Review (Sonnet 4.6, weekly, batch API)

Input: last 7 days of `chat_summaries` (period='day'), resolved/unresolved `commitments`, all `radar_alerts` from the week, `relationship_state` deltas vs prior week, all `brief_feedback` from the week.

System prompt:

```
Write the user's Weekly Review. Goals:
1. Pattern recognition across the week that daily briefs couldn't see.
2. Honest assessment of where the user's attention went vs where business value is.
3. Specific, concrete recommendations for the coming week.

Sections:

A. WHERE YOUR ATTENTION WENT — data-driven, cite message volume by chat tag.
B. WHERE VALUE MOVED — deals progressed, stalled, lost, or opened.
C. PATTERNS — recurring objections, themes across clients, things said more than once.
D. MISSED OR AT-RISK — what slipped this week and why.
E. NEXT WEEK'S PRIORITIES — 3-5 specific, named actions.

Be willing to say uncomfortable things. If the user is avoiding a deal, name it.
If two clients are circling the same concern, connect them. If the pipeline looks
thin, say so plainly.

Max length: ~6000 chars.
```

---

## 6. MCP server tool contract

Transport: Streamable HTTP. Auth: OAuth 2.1 or bearer token. All tools are **read-only** — the MCP server does not mutate state. Return types are JSON.

```
search_messages(query: str, chat_ids?: list[int], tags?: list[str],
                date_from?: date, date_to?: date, sender_ids?: list[int],
                limit: int = 50) -> list[MessageResult]
  # Full-text + filter search over messages. Uses pg_trgm / tsvector.

semantic_search(query: str, top_k: int = 20, chat_ids?: list[int],
                tags?: list[str], date_from?: date) -> list[MessageResult]
  # Vector search via pgvector over message_understanding.embedding.

get_chat_history(chat_id: int, before?: datetime, limit: int = 50) -> list[Message]
  # Paginated history for one chat.

list_chats(tag?: str, include_untagged: bool = False) -> list[ChatSummary]
  # All chats with their tag, last activity, participant count.

get_chat_summary(chat_id: int, period: str = "week",
                 periods_back: int = 1) -> list[ChatSummary]
  # Pre-computed daily/weekly summaries.

get_commitments(status?: str, owner?: str, chat_id?: int,
                overdue_only: bool = False) -> list[Commitment]

get_signals(signal_types?: list[str], min_strength: int = 1,
            date_from?: date, chat_ids?: list[int]) -> list[Signal]

get_relationship_state(chat_id?: int) -> list[RelationshipState]
  # All or one; shows stage, temperature, open threads.

get_recent_brief(date?: date) -> BriefText
  # Returns the Telegram-posted Morning Brief content.
```

**MessageResult** includes: `chat_id, chat_title, chat_tag, message_id, sent_at, sender_name, text, summary_en, signal_type, url` (deep link to Telegram message).

Every message result should carry enough context that Claude can cite it to the user without re-querying.

---

## 7. Telegram bot command surface

The bot is the sole UI in v1. Commands:

```
/start         — onboarding (first time only)
/tag           — re-run chat tagging flow (paginated tap-to-tag)
/ignore        — mark current chat or a chat by name as ignored
/brief         — regenerate and send today's brief on demand
/weekly        — on-demand weekly review
/feedback      — reply to a brief item with "useful", "not_useful", "missed"
/search <q>    — quick keyword search (for the user, not the LLM)
/pause         — pause ingestion (privacy moment)
/resume        — resume ingestion
/status        — show ingestion health, queue depth, last processing times
```

**Onboarding flow** (`/start`, 10–15 minutes):
1. Bot fetches top N most-active dialogs by message volume over last 90 days (default N=40).
2. Bot walks through each, showing chat name and last 3 messages preview, with inline-keyboard buttons: `Client | Prospect | Colleague | Personal | Ignore | Skip`.
3. Optional free-text note per tagged chat ("who is this, what matters").
4. Bot stores tags, triggers understanding-pass backfill for newly-tagged chats.

**Brief delivery**: fixed time (user sets in `/start`, default 07:00 local), as a single message. Reply with `useful` / `not_useful` / `missed: <your note>` to feed `brief_feedback`.

---

## 8. Radar feedback loop

Radar is the most error-prone component — what counts as a "signal" is user-specific. The design includes a correction loop from day one.

- Every item surfaced in the Morning Brief carries a hidden alert_id (encoded in a short tag the user can reference, e.g., `#a7f2`).
- User feedback via `/feedback a7f2 not_useful "just smalltalk"` writes to `brief_feedback` with the alert reference.
- User feedback via `/feedback missed "acme mentioned budget twice yesterday"` creates a `brief_feedback` row with no alert_id — flagging a false negative.
- The Morning Brief prompt includes the last 14 days of `brief_feedback` as part of its context. Sonnet uses this to calibrate what to surface.
- A weekly offline job (`worker-weekly` epilogue) mines `brief_feedback` for patterns and emits a summary into the next week's brief system context ("you consistently flag X as not useful; deprioritize").

No ML retraining loop in v1 — Sonnet's in-context adaptation from feedback data is sufficient at this scale.

---

## 9. Non-obvious constraints & gotchas

**Telethon session:**
- 2FA must be enabled on the user's Telegram account.
- Session file is stored encrypted at rest on the VPS (libsecret or age + key held outside the repo).
- On first run, the user performs interactive auth (phone code + 2FA password). Document this clearly — it's the only non-automatable setup step.
- Handle `SessionPasswordNeededError`, `FloodWaitError`, `PhoneCodeInvalidError` explicitly.

**Flood waits:**
- Live event handling does not trigger flood waits at this volume.
- Backfill on onboarding is the risk. Paginate `get_messages` with `limit=100`, sleep 1s between pages. On `FloodWaitError`, honor `e.seconds` and resume.

**Gap recovery:**
- On ingestion restart, for each non-ignored chat, fetch messages with `min_id = max(message_id) in DB for that chat`, paginate forward. Do this before attaching live handlers.

**Deletions and edits:**
- Store edits as `edited_at` + overwrite `text` but keep the prior version in an `edit_history JSONB` column (add to schema if desired). Deletions set `deleted_at` but never hard-delete the row.

**MCP + Anthropic IP allowlist:**
- The MCP endpoint (e.g., `mcp.<user-domain>.tld`) must be publicly reachable from Anthropic's published IP ranges.
- `ufw` / `iptables` rules: allow `443/tcp` inbound only from Anthropic's ranges; allow SSH from user's IP; drop everything else on that subdomain.
- The rest of the box (Telegram bot, Postgres, Ollama) has no inbound public exposure; the bot talks outbound to Telegram only.

**Prompt caching on Brief:**
- The cached portion must be first in the request and stable across runs. System prompt + chat tags/notes + signal taxonomy + format spec = cached block. Daily-varying inputs (understanding-pass outputs, alerts, commitments, feedback) come after the cache boundary.

**Understanding-pass throughput:**
- Qwen 2.5 7B Q4 on 4 dedicated vCPU via llama.cpp / Ollama: ~10–15 tok/s, ~15–20s per message with ~200-token output. At 500 msgs/day this averages ~10% CPU, well within headroom. Process sequentially (single-worker); concurrency gives nothing on CPU inference.

**Noise filtering:**
- Chats tagged `ignore` or untagged after onboarding: ingest messages, skip understanding-pass, don't embed, don't index for MCP.
- Public channels (non-group broadcast type) default to `ignore` unless explicitly tagged.
- Within tagged chats, no per-message filtering in v1 — trust the tag.

**Language normalization:**
- All `summary_en` and `entities.normalized_en` are English regardless of source language. This keeps downstream prompts, Radar, and MCP queries uniform. Original `text` stays untouched.

**Privacy posture:**
- Group chats: the user is a legitimate party. Content is treated as business records for the user's own review.
- No outbound sharing, no training use, no third-party exposure beyond the understood external calls (Claude API for Brief/Weekly; Claude Pro/Max MCP for Ask Anything).
- `/pause` and `/resume` let the user stop ingestion mid-conversation for sensitive moments.

---

## 10. Open questions for future versions

Not for v1, but worth parking:

- Should Radar learn per-chat baselines (what's normal tone for this client) vs global signals?
- Automatic proposal/deck reminder when user commits to send materials but doesn't follow up within N days.
- Integration with email (Gmail MCP already exists in user's Claude).
- Voice note transcription (Whisper on the same VPS, CPU feasible for low volume).
- Cross-chat entity resolution: same company mentioned across multiple DMs under different names.

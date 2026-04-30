# Session changes — 2026-04-29

This file is a self-contained record of every change shipped in the
2026-04-29 session, intended as context for follow-up work in a new
Claude session. Read this before starting Stage 1 / Stage 2 of the
unified DM router (see `docs/dm-router-prompt.md`).

## Headline

The brief was lying about dates. Two distinct bugs, same shape: both
`commitments.created_at` and `radar_alerts.created_at` were the
extractor/aggregator's clock, not the underlying conversation's clock.
Months-old "tamam abi" messages were surfacing as fresh action items.
Plus the auto-tagger was wasting Ollama on chats the user has never
engaged with. Plus the tagging / understanding / embedding scopes were
incoherent. All of that is now fixed.

## Pull requests merged (in order)

| # | Title | What it does |
|---|---|---|
| 30 | feat: personal-assistant brief + auto chat tagger | Six-section brief, new signal taxonomy (business + personal), `worker-chat-tagger` service with embedding-centroid + Ollama LLM stages, migration 0003 (tag_confidence/tag_source/tag_locked/tag_reason on chats) |
| 31 | fix: chunk brief over 4096 chars for Telegram | Splits brief at line boundaries; falls back to plain text if HTML parse mode trips |
| 32 | fix: anchor every brief item against today's date | Lead fresh input with "Today is YYYY-MM-DD"; full timestamps on message lines; explicit recency rules in BRIEF_FORMAT_SPEC |
| 34 | fix: tagger Stage B + bot manual-tag locks + progress + cap | `_has_min_messages` counts raw text (not embeddings); bot tag-set writes `tag_source='manual', tag_locked=TRUE`; full new taxonomy in onboarding UI; per-25-chat progress logs; `TBC_TAGGER_MAX_PER_RUN=200` cap; migration 0004 (colleague→internal); MCP description hints |
| 35 | fix: commitments use true conversation date | New `commitments.source_sent_at` column (migration 0005); extractor populates it from source message's `sent_at`; brief filters/renders on it |
| 36 | feat: resolve / cancel / update commitments by DM | Three new MCP write tools (`resolve_commitment`, `cancel_commitment`, `update_commitment`); `get_commitments` gains `query` (ILIKE) and `limit`; bot agent prompt updated; chat handler injects `[meta] current_message_id=...` |
| 37 | fix: radar alerts use true source date, skip stale signals | New `radar_alerts.source_sent_at` (migration 0006); aggregator skips signal groups whose newest source message is >7d old (`STALE_SIGNAL_CUTOFF`); brief filters/renders on it |
| 38 | fix: understanding worker processes newest messages first | One-line change to `_POLL_SQL`: `ORDER BY sent_at DESC` so live traffic is understood within seconds |
| 39 | fix: keep Ollama models warm; raise chat timeout | Every Ollama call passes `keep_alive="60m"`; chat timeout 120s → 300s |
| 40 | fix: understanding worker only processes last 30 days | Add `m.sent_at >= NOW() - INTERVAL '30 days'` to poll SQL; 91% of backlog (>30d messages) was waste |
| 41 | feat: tagger only considers chats where owner is involved | Strict rule in `candidate_chats`: chat must have owner sender / @-mention / reply-to-owner within 180d; 96% of untagged chats now correctly skipped; new `TBC_TG_OWNER_USERNAME` setting (default `gokhanseckin`) |
| 42 | fix: bulk_embed scoped to owner-involved chats only | Same involvement filter in bulk_embed CLI |
| 43 | fix: tagger no longer requires 10-message floor | Remove `_has_min_messages` gate; involvement filter alone is sufficient |
| 44 | feat: brief preserves radar #xxxx tags | Surface `ref=#xxxx` in assembler input; prompt requires LLM to carry tag through to "Worth Noticing" output as parenthetical |
| 45 | docs: README — bot commands, brief feedback loop, commitment DM flow | Documents all 11 slash commands, the feedback calibration loop, the natural-language commitment flow |

## DB migrations applied (in order)

| Rev | What |
|---|---|
| `0003_chat_tagging` | Adds `chats.tag_confidence`, `tag_source`, `tag_locked`, `tag_reason`. Locks all existing manually-tagged chats. |
| `0004_rename_colleague_tag` | UPDATE chats SET tag='internal' WHERE tag='colleague'. |
| `0005_commitment_source_sent_at` | Adds `commitments.source_sent_at`; backfills 1812 rows from messages.sent_at. |
| `0006_radar_alert_source_sent_at` | Adds `radar_alerts.source_sent_at`; backfills 728 rows from MAX(sent_at) across `supporting_message_ids` JSONB array. |

## One-shot data operations performed on prod DB

| Op | Effect |
|---|---|
| Untag chats failing strict involvement rule | 63 auto_llm-tagged chats reverted to NULL tag (`tag_source=NULL, tag_locked=FALSE, tag_confidence=NULL, tag_reason=NULL, tag_set_at=NULL`). Manual tags untouched. |
| Delete radar alerts in non-involved chats | 301 rows removed from `radar_alerts`. |
| Delete commitments in non-involved chats | 597 rows removed from `commitments`. |
| Run `bulk_embed` (one-shot via systemd-run) | All eligible messages now have embeddings — `B. Embedding satisfying = processed` in audit. |

## Big-picture data audit (post all changes)

| Stage | Criteria | Satisfying | Processed | Pending | Ignored |
|---|---|---:|---:|---:|---:|
| A. Ingestion | last 180d from Telegram | 47,508 | 47,508 | 0 | — |
| B. Embedding | tagged-non-ignore + owner-involved 180d | ~8,145 | ~8,030 | small | rest |
| C. Tagging | chat owner-involved 180d | ~8,612 | mostly tagged | small | rest |
| D. Understanding | tagged-non-ignore + last 30d | ~1,179 | catching up | catching up | rest |

## Pipeline architecture (post-fix)

```
┌──────────────────────────────────────────────────────────────────┐
│ Ingestion: 180d                                                  │
│  └─ Embeddings (bge-m3): tagged-non-ignore + owner-involved 180d │
│      ├─ Auto-tagger: owner-involved 180d, no msg-count floor     │
│      └─ Understanding (Qwen 2.5 7B): tagged-non-ignore + 30d     │
│          ├─ Radar alerts: source >= now-7d at creation           │
│          │   ├─ Brief: source_sent_at >= now-24h                 │
│          ├─ Commitments: source_sent_at present                  │
│          │   └─ Brief: source_sent_at >= now-90d                 │
└──────────────────────────────────────────────────────────────────┘
```

## Settings introduced this session

```
TBC_TAGGER_INTERVAL_SECONDS=3600    # tagger run cadence
TBC_TAGGER_AUTO_THRESHOLD=0.78      # Stage A cosine sim floor
TBC_TAGGER_MARGIN=0.05              # Stage A top-1 vs top-2 margin
TBC_TAGGER_MIN_MESSAGES=10          # USED ONLY for Stage A centroid quality (not as a per-chat eligibility floor anymore)
TBC_TAGGER_SAMPLE_SIZE=50           # messages sampled per chat for centroid + LLM prompt
TBC_TAGGER_MAX_PER_RUN=200          # cap per sweep so the tagger can't dominate Ollama
TBC_TG_OWNER_USERNAME=gokhanseckin  # used for ILIKE %@gokhanseckin% mention detection
```

## Bot slash commands (current)

`/start`, `/tag`, `/ignore <ChatName>`, `/brief`, `/weekly`, `/search <query>`,
`/feedback #xxxx <type> "note"` or `/feedback missed "note"`,
`/pause`, `/resume`, `/status`, `/reset`, `/help`.
Free-text DMs go to **Claude Sonnet 4.6** via the agent (`agent.py` →
Anthropic API with MCP tool access).

## Where each LLM is used today

| Job | LLM | Where |
|---|---|---|
| Bot DM responses (free-text) | **Claude Sonnet 4.6** | Anthropic API (cloud, paid) |
| Morning brief generation | **Claude Sonnet 4.6** | Anthropic API (cloud, paid) |
| Weekly review | **Claude Sonnet 4.6** | Anthropic API (cloud, paid) |
| Per-message understanding | **Qwen 2.5 7B** | Local Ollama on VPS (free) |
| Chat auto-tagging Stage B | **Qwen 2.5 7B** | Local Ollama on VPS (free) |
| Embeddings | **bge-m3** | Local Ollama on VPS (free) |

## Known dormant / partially-built features

- `BriefFeedback` table is wired into the brief prompt; `/feedback` slash
  command writes to it. **No equivalent natural-language path** for the
  agent — there's no MCP write tool for feedback. This is the gap the
  next session should close.
- `relationship_state.updated_at` has the same shape bug we fixed for
  commitments and radar — it's the worker's clock, not the
  conversation's clock. Not yet fixed; lower priority because the
  stage/temperature value is meant to be a current snapshot.
- No `/retag` bot command for forcing re-classification of a single
  chat.
- No drift detection for auto-tagged chats whose centroid has shifted.
- No auto-resolution detection from group chats (only DM bot path).

## MCP tools available today

| Tool | R/W | Purpose |
|---|---|---|
| `search_messages` | R | Full-text search |
| `semantic_search` | R | pgvector cosine search |
| `get_chat_history` | R | Paginated messages for a chat |
| `list_chats` | R | All chats with tag |
| `get_chat_summary` | R | Pre-computed daily/weekly summaries |
| `get_commitments` | R | Filter by status/owner/chat/query |
| `get_signals` | R | Radar signals |
| `get_relationship_state` | R | Stage/temperature per chat |
| `get_recent_brief` | R | Recent brief text |
| **`resolve_commitment`** | W | Mark commitment done (PR #36) |
| **`cancel_commitment`** | W | Mark commitment cancelled (PR #36) |
| **`update_commitment`** | W | Set due_at or append note (PR #36) |

## Files of interest for the next session

- `services/tg-bot/tbc_bot/agent.py` — current Claude-only DM agent
- `services/tg-bot/tbc_bot/handlers/chat.py` — free-text handler that calls agent.ask
- `services/tg-bot/tbc_bot/handlers/feedback.py` — slash-command feedback handler (will become a fallback)
- `services/mcp-server/tbc_mcp_server/tools/commitments.py` — pattern for adding write tools
- `services/mcp-server/tbc_mcp_server/main.py` — tool registration + dispatcher
- `services/worker-understanding/tbc_worker_understanding/ollama_client.py` — async httpx wrapper used by understanding + tagger
- `packages/common/tbc_common/db/models.py` — `BriefFeedback` model

## Operational state at session end

- All workers active.
- chat-tagger candidate pool is empty (sweeps will be no-ops until
  new owner-involved chats appear or someone untags one manually).
- worker-understanding catching up on last-30d backlog (~218 pending
  at session end, ETA ~1.5h to 100%).
- No background jobs running, no rogue processes.

---

*This file is a snapshot. Run the audit query in the prompt context if
you want fresh numbers.*

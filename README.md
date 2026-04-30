# Telegram Business Brain

A second brain for Telegram-driven business. Continuously reads the user's Telegram history, understands what matters, surfaces missed opportunities, and answers questions about the corpus.

- **Morning Brief** (proactive): daily Telegram message synthesizing opportunities, owed replies, portfolio movement.
- **Weekly Review** (proactive): pattern recognition across the week.
- **Ask Anything** (reactive): MCP server consumed by the user's Claude Pro/Max via custom connector.

See [docs/mvp-spec.md](docs/mvp-spec.md) for the full product spec.

## Status

MVP in active development. Single-user, self-hosted on a Hetzner CX43 (8 vCPU shared, 16 GB RAM, no GPU — Qwen/embeddings run on CPU).

## Architecture at a glance

- `services/ingestion` — Telethon userbot.
- `services/worker-understanding` — local Qwen 2.5 7B JSON extraction + bge-m3 embeddings.
- `services/worker-radar`, `services/worker-commitments` — derived analytics.
- `services/worker-brief`, `services/worker-weekly` — Sonnet 4.6 synthesis.
- `services/tg-bot` — aiogram bot (sole UI). Local-first DM router uses Qwen 2.5 3B for intent classification and falls through to Sonnet 4.6 only for Q&A and unkeyed commitment phrasing — see [DM routing](#dm-routing).
- `services/mcp-server` — read-only MCP tools over Streamable HTTP. Plus the write tools (`write_brief_feedback`, `resolve_commitment`, `cancel_commitment`, `update_commitment`) used by the Sonnet path.
- `packages/common` — shared DB models, config, prompts, and write functions reused by the bot and the MCP server (single source of truth for both code paths).
- `infra/` — Terraform (Hetzner) + Ansible provisioning.

## Bot commands

The Telegram bot is the sole UI. DM the bot one of these commands (only the configured owner is recognized).

### Daily use

| Command | What it does |
|---|---|
| `/brief` | Generate today's Morning Brief now (instead of waiting for the 7am job). |
| `/weekly` | Generate the Weekly Review now. |
| `/feedback #xxxx <type> "note"` | Rate a brief item by its `#xxxx` tag. See [Tags](#tags) and [Brief feedback](#brief-feedback). |
| `/feedback missed "note"` | Report something the brief should have surfaced but didn't. |
| `/done c<id> [note]` | Mark a commitment complete by its `(c<id>)` tag from the brief. See [Commitment shortcuts](#commitment-shortcuts). |
| `/cancel c<id> [reason]` | Cancel a commitment as no-longer-relevant. |
| `/search <query>` | Keyword search over your Telegram messages. Returns up to 5 hits. |
| `/status` | Ingestion + understanding health: counts, last-activity timestamps, pause flag. |

### Setup & maintenance

| Command | What it does |
|---|---|
| `/start` | Onboarding flow: tag your top chats with role labels. |
| `/tag` | Re-runs the onboarding tag-walk for any chats still missing a manual tag. |
| `/ignore <ChatName>` | Mark a chat as `ignore` so the brief skips it. The mark is locked. |

### Conversation control

| Command | What it does |
|---|---|
| Any non-slash text | Routed through the [DM router](#dm-routing) — local-first, falls through to Claude only when needed. |
| `/reset` | Clear the Claude conversation history for this DM thread. |

### Operations

| Command | What it does |
|---|---|
| `/pause` | Stop the Telegram-side ingestion (workers keep running on what's already in the DB). |
| `/resume` | Re-enable ingestion after `/pause`. |
| `/help` | In-bot help text. |

## Tags

Three distinct tag systems, three different purposes — easy to confuse, important to keep separate:

| Tag | Where it appears | What it identifies | Used for |
|---|---|---|---|
| `#xxxx` | At the end of "💡 Worth Noticing" bullets in the brief. 4-char hex (e.g. `#a8ce`). | A specific **radar alert / brief item**. Minted by `worker-radar`. | `/feedback #xxxx <type>` to calibrate future briefs. |
| `(c<id>)` | At the end of "✅ On Your Plate" / "🔔 Waiting on Others" bullets in the brief. Integer with `c` prefix (e.g. `(c42)`). | A specific **commitment** row. The integer is the database primary key. | `/done c<id>` or `/cancel c<id>` to mark complete or drop. |
| Role tag | Per-chat metadata on the `chats` table. One of `client / prospect / supplier / partner / internal / friend / family / personal / ignore`. | The **role this counterparty plays**. | `/tag`, `/ignore`, the auto-tagger, and brief routing logic. **Not yet** addressable by DM — see roadmap. |

**These are NOT interchangeable.** A DM like "Doğa is personal" is a *role-tag correction* on the `chats.tag` column. It is **not** brief feedback (`#xxxx`) and **not** a commitment action (`(c<id>)`). Until the planned `retag` intent ships, the bot treats role-tag DMs as ambiguous and asks you to rephrase.

## DM routing

Free-text DMs flow through a three-stage router. The first stage that matches handles the message; later stages don't run.

```
DM
 │
 ▼
[1] Slash command? ──► dedicated handler              (~150ms)
 │     (covers: /done, /cancel, /feedback, /brief, /tag, /ignore, etc.)
 │  no
 ▼
[2] Regex rules ─────► local executor                 (~150ms, no LLM)
 │     • #xxxx <sentiment> / <sentiment> #xxxx  → write brief_feedback
 │     • done|finished|completed c<id> [note]   → resolve_commitment
 │     • cancel|drop|forget c<id> [reason]      → cancel_commitment
 │  no match
 ▼
[3] Qwen 2.5 3B (Ollama, format=json) ──► classify intent + extract fields
 │     │
 │     ▼ intent ∈ {feedback}        → local executor   (~10-25s, no Claude)
 │     ▼ intent = ambiguous         → "rephrase" reply (no Claude)
 │     ▼ intent ∈ {qa, commitment_*  → fall through    (Claude, see below)
 │       without explicit c<id>}
 ▼
[4] Claude (Anthropic API) + MCP tools                 (~5min)
       • Q&A questions: "did Alice mention pricing last week?"
       • Free-text commitments: "I sent the report" (Claude searches via MCP get_commitments)
```

### What Qwen does vs what Claude does

**Local Qwen 2.5 3B** (Ollama on the VPS, no network):
- Classifies intent on free-text DMs that the rule path didn't catch.
- Extracts fields (`feedback_type`, `item_ref`, `note`, `query`).
- Schema-validated output. Failure modes (parse error, unknown intent, low confidence) all collapse to `ambiguous` — **never** escalate to Claude.
- Cost: free. Latency: ~10-25s on CPU (varies with model warm/cold and contention).

**Claude (Anthropic API)** is called **at most once per DM**, and only on:
- LLM intent = `qa` (question that needs to query chat history / signals).
- LLM intent = `commitment_*` **without** an explicit `c<id>` (Claude uses MCP `get_commitments(query=...)` to find the right row).

That's it. The rule path, the local feedback executor, the local commitment shortcut path, and the ambiguous-rephrase reply **never** call Claude.

You can audit the routing in real time:
```
journalctl -u tbc-bot | grep router_dispatch
```
Each DM emits one structured log line: `{intent, source, confidence, claude_called: bool}`.

### The cost guardrail

The router was built around one hard property: **at most one Claude call per DM**. There is no auto-retry through Claude on Qwen failure, no "if Qwen is unsure, just ask Claude," no batching. Schema mismatch → ambiguous. Ollama outage → ambiguous. Low confidence → ambiguous. The user gets asked to rephrase. The next DM is a fresh budget.

## Brief feedback

The brief is **calibrated by you**. Each "💡 Worth Noticing" item that comes from a radar alert ends with a `#xxxx` reference tag (see [Tags](#tags)). Three ways to react, all writing the same `brief_feedback` row:

**Slash command** (precise, no LLM):
```
/feedback #5096 not_useful "I already know about this"
/feedback #5096 useful
/feedback #5096 missed_important "this should have been bigger"
/feedback missed "Yuri's exit deserved a callout"
```

**Rule-matched DM** (sub-second, no LLM):
```
#5096 useful
#5096 not useful just smalltalk
not useful #5096
```

**Free-text DM** (Qwen ~10-25s, no Claude):
```
the #5096 was useful, good catch
you missed Yuri's exit, that deserved a callout
```

Slash aliases:

| Canonical | Aliases |
|---|---|
| `useful` | `yes`, `good` |
| `not_useful` | `notuseful`, `no` |
| `missed_important` | `missed` |

The slash parser also rejects unknown types — `/feedback #abcd bogus "..."` replies with usage help and writes nothing.

What the row drives:
- Tomorrow's brief includes the last 14 days of feedback in its system prompt under "calibration."
- The brief LLM drops similar items, weights missed-pattern feedback higher, avoids repeating things you've called out as noisy.
- After ~5-10 events, briefs start feeling personally tuned.

## Commitment shortcuts

Each open commitment in the brief carries a `(c<id>)` tag — see [Tags](#tags). Three ways to mark one done or cancelled, all going through the same shared write path (identical row shape, same audit annotation `[resolved YYYY-MM-DD: <note>]`):

| You DM | Path | Latency |
|---|---|---|
| `/done c42 sent today` | slash | ~150ms |
| `/cancel c42 no longer needed` | slash | ~150ms |
| `done c42 sent today` | rule (regex) | ~150ms |
| `finished c42` / `completed c42` / `resolved c42` | rule | ~150ms |
| `cancel c42` / `drop c42` / `forget c42` | rule | ~150ms |
| `I sent the report` (no id) | Qwen → Claude → MCP | ~5min |

The free-text path (last row) stays on Claude because it needs MCP `get_commitments(query="report")` to find the right row. The shortcut paths bypass that lookup — you already did it by reading the brief.

Updates that aren't a resolve/cancel (push the due date, append a status note) currently still go through the natural-language → Claude path:

| You DM | Bot does |
|---|---|
| `Push the contract to next Friday` | `update_commitment(id, due_at=...)` |
| `Add a note: waiting on Sara's reply` | `update_commitment(id, note_append="...")` |

When Claude is involved it always confirms with the commitment id and description: `"Marked done: c42 — send the report to Bob."` If multiple plausible matches exist, it lists them and asks which one — never guesses.

## Development

See [DEVELOPING.md](DEVELOPING.md).

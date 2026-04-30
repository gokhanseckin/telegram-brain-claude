# Telegram Business Brain

A second brain for Telegram-driven business. Continuously reads the user's Telegram history, understands what matters, surfaces missed opportunities, and answers questions about the corpus.

- **Morning Brief** (proactive): daily Telegram message synthesizing opportunities, owed replies, portfolio movement.
- **Weekly Review** (proactive): pattern recognition across the week.
- **Ask Anything** (reactive): MCP server consumed by the user's Claude Pro/Max via custom connector.

See [docs/mvp-spec.md](docs/mvp-spec.md) for the full product spec.

## Status

MVP in active development. Single-user, self-hosted on a Hetzner CCX23.

## Architecture at a glance

- `services/ingestion` — Telethon userbot.
- `services/worker-understanding` — local Qwen 2.5 7B JSON extraction + bge-m3 embeddings.
- `services/worker-radar`, `services/worker-commitments` — derived analytics.
- `services/worker-brief`, `services/worker-weekly` — Sonnet 4.6 synthesis.
- `services/tg-bot` — aiogram bot (sole UI).
- `services/mcp-server` — read-only MCP tools over Streamable HTTP.
- `packages/common` — shared DB models, config, prompts.
- `infra/` — Terraform (Hetzner) + Ansible provisioning.

## Bot commands

The Telegram bot is the sole UI. DM the bot one of these commands (only the configured owner is recognized).

### Daily use

| Command | What it does |
|---|---|
| `/brief` | Generate today's Morning Brief now (instead of waiting for the 7am job). Bot replies once the worker has triggered it. |
| `/weekly` | Generate the Weekly Review now. |
| `/feedback #xxxx <type> "note"` | Rate an item from the brief (see [Brief feedback loop](#brief-feedback-loop) below). |
| `/feedback missed "note"` | Tell the brief writer it should have surfaced something it missed. |
| `/search <query>` | Keyword search over your Telegram messages. Returns up to 5 hits with chat name + 120-char snippet. |
| `/status` | Show ingestion + understanding health: total messages, understood count, unprocessed backlog, last activity timestamps, pause flag. |

### Setup & maintenance

| Command | What it does |
|---|---|
| `/start` | Onboarding flow: walks you through tagging your top chats with role labels (client, prospect, supplier, partner, internal, friend, family, personal, ignore). |
| `/tag` | Re-runs the onboarding tag-walk for any chats still missing a manual tag. Same UI as `/start`. |
| `/ignore <ChatName>` | Mark a chat (matched by title substring) as `ignore` so the brief skips it. The mark is locked — the auto-tagger won't overwrite. |

### Conversation control

| Command | What it does |
|---|---|
| Any non-slash text | Sent to Claude with full MCP tool access. Used for ad-hoc questions ("did Mieszko ever mention BVI?") and **commitment management** (see below). |
| `/reset` | Clear the Claude conversation history for this DM thread. |

### Operations

| Command | What it does |
|---|---|
| `/pause` | Stop the Telegram-side ingestion (keeps the worker pipeline running on what's already in the DB). Useful before maintenance. |
| `/resume` | Re-enable ingestion after `/pause`. |
| `/help` | Show the in-bot help text — abbreviated version of this table. |

## Brief feedback loop

The brief is **calibrated by you**. Each "Worth Noticing" item that comes from a radar alert ends with a `#xxxx` reference tag. Two ways to react — both write the same `brief_feedback` row:

**Plain DM** (Claude routes it via the `write_brief_feedback` MCP tool):

```
the #5096 was useful
not useful, just smalltalk #5096
you missed Yuri's exit, that deserved a callout
```

**Slash command** (precise bypass, no LLM):

```
/feedback #5096 not_useful "I already know about this"
/feedback #5096 useful
/feedback #5096 missed_important "this should have been bigger"
/feedback missed "Yuri's exit deserved a callout"
```

Aliases the slash parser accepts:

| Type | Aliases |
|---|---|
| `useful` | `useful`, `yes`, `good` |
| `not_useful` | `not_useful`, `notuseful`, `no` |
| `missed_important` | `missed`, `missed_important` |

What it does:

- Bot writes a row to `brief_feedback`.
- Tomorrow's brief includes the last 14 days of feedback in its system prompt under "calibration".
- The brief LLM uses that to drop similar items, weight missed-pattern feedback higher, and avoid repeating things you've called out as noisy.
- After ~5-10 events, briefs start feeling personally tuned.

## DM router (local-first)

Free-text DMs go through a 3-stage router before reaching Claude:

```
DM → [1] regex rules        → exec_feedback / exec_commitment_*   (sub-second)
       │ no match
       ▼
     [2] Qwen 2.5 3B (Ollama, format=json)
       │ schema-validated + confidence ≥ 0.7
       ▼
     intent = feedback          → exec_feedback              (~3-5s, free-text reactions)
     intent = ambiguous         → ask user to rephrase       (no Claude)
     intent ∈ {qa, commitment_*}→ ask() → Claude             (1 call, existing path)
```

The rule path now also catches **commitment shortcuts** keyed off the `(c<id>)` tag rendered in the brief:

| You DM | Path | Bot does |
|---|---|---|
| `/done c42 sent today` | slash | resolve_commitment(42, note="sent today") → "Marked done: c42 — …" |
| `/cancel c7 no longer needed` | slash | cancel_commitment(7, reason=…) |
| `done c42 sent today` | rules | same as above, no Claude |
| `cancel c7` | rules | same as above |
| `I sent the report` (no id) | qwen → claude | unchanged — Claude uses MCP get_commitments to find the row |

**At most one Claude call per DM.** The router never auto-retries through Claude on a Qwen failure — schema mismatch, low confidence, or Ollama outage all collapse to "rephrase" rather than escalating. Verify with `journalctl -u tbc-bot | grep claude_called`.

Slash commands (`/feedback`, `/done`, `/cancel`, `/brief`, `/ignore`, etc.) bypass the router entirely.

## Commitment management via DM

Free-text messages to the bot can resolve, cancel, or update tracked commitments. The agent reads your wording, finds the matching open commitment, and writes the change.

| You DM | Bot does |
|---|---|
| "I sent the report today" | `resolve_commitment(id, note="sent today")` → status=`done`, audit-annotated |
| "Paid Gizem the $67.05" | resolve_commitment, same path |
| "Forget the contract thing with Acme" | `cancel_commitment(id, reason="...")` → status=`cancelled` |
| "Push the contract to next Friday" | `update_commitment(id, due_at=...)` |
| "Add a note: waiting on Sara's reply" | `update_commitment(id, note_append="...")` |

The bot always confirms with the commitment id and description: `"Marked done: #6879 — send $67.05 more to Gizem."` If multiple plausible matches exist, it lists them and asks which one — never guesses.

## Development

See [DEVELOPING.md](DEVELOPING.md).

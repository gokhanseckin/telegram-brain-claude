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

## Development

See [DEVELOPING.md](DEVELOPING.md).

# Project notes for Claude

## Data deletion — always ask first

Before running anything that **deletes or wipes data** in this project, **stop and ask the owner explicitly**, even if it looks obviously safe or is part of a multi-step plan you're already executing.

Applies to:
- `DELETE FROM ...` against any production Postgres table (`commitments`, `message_understanding`, `messages`, etc.) — including filtered deletes by `model_version`, `status`, or any other column.
- `TRUNCATE`, `DROP TABLE`, `DROP COLUMN`, schema migrations that drop data.
- Removing files on the VPS (`rm`, `find -delete`).
- Force-pushing or `git reset --hard` against shared branches.
- Cancelling/resolving commitments at scale (`UPDATE commitments SET status=...`).

Quote the exact statement and the row count it would touch, then wait for an explicit "yes" before running. Do **not** chain a destructive query into a longer pipeline (e.g. `DELETE ... && systemctl restart ...`) — the destructive step gets its own confirmation turn.

A bumped `MODEL_VERSION` plus a worker restart is the **non-destructive** way to reprocess understanding rows; prefer it when possible.

## Deployment topology

- Local repo at `/Users/gokhanseckin/claude-projects/telegram-brain-claude`
- VPS at `root@116.203.2.243` (floating IP), checked out at `/opt/tbc`
- Workers run as systemd units (`tbc-worker-*`, `tbc-bot`, `tbc-mcp-server`)
- Trigger files for on-demand runs (must be touched as `tbc` user):
  - `sudo -u tbc touch /tmp/tbc_trigger_brief`
- DB is local Postgres on the VPS; access via `sudo -u postgres psql tbc`

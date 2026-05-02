# Brief-coupled understanding worker

**Date:** 2026-05-02
**Status:** shipped on `feat/brief-coupled-understanding`
**Reverts to previous behaviour with:** `TBC_UNDERSTANDING_MODE=continuous`

## Why

The LLM understanding pass (Gemma 4 26B via novita.ai) ran every 5 seconds on a continuous polling loop. The only consumer of LLM-derived fields (`is_commitment`, `summary_en`, signals) is the daily brief, so the spend and inference noise were excessive. Commitments are not time-critical — they only need to be fresh at brief time.

## What it does

The LLM pass is now triggered **only as step 1 of brief generation**. The flow is identical for the 07:00 cron and on-demand `/brief`:

```
brief trigger (cron OR /tmp/tbc_trigger_brief)
  → pending_understanding_count(MODEL_VERSION)
  → if > 0:
       touch /tmp/tbc_trigger_understanding   # wakes worker-understanding
       poll count every 5s, log every 30s
       on count==0 → continue
       on timeout (TBC_BRIEF_PRE_UNDERSTANDING_TIMEOUT_S, default 300s) → continue with warning
  → run_brief()                               # existing assembler + LLM + Telegram send
```

Meanwhile worker-understanding in `brief-coupled` mode runs:

- `embed_loop` (5s) — keeps `bge-m3` embeddings fresh so `semantic_search` and MCP tools stay usable between briefs. Writes partial rows at `model_version="embeddings-only-<YYYY-MM-DD>"`.
- `trigger_watcher` (30s) — when `/tmp/tbc_trigger_understanding` appears, drain the LLM queue and delete the file.

The handshake between brief and understanding is **DB-state-based** (re-counting pending rows). The trigger file is just an edge signal; idempotent if touched multiple times.

## Why two `model_version` rows per message

- The realtime embed loop writes `model_version="embeddings-only-<date>"` with only `embedding` populated.
- The brief-time LLM bulk pass writes a second row at the canonical LLM `MODEL_VERSION` (`understanding-2026-05-01-v13-...`) with both embedding *and* understanding fields.

Both rows coexist. Existing consumers either query by current LLM `MODEL_VERSION` (commitments worker, brief assembler) or by "any row with `embedding IS NOT NULL`" (semantic_search). No schema change.

## What was off-limits (per `feat/brief-coupled-understanding` PR)

The v13 understanding behaviour stays byte-identical:

- `packages/common/tbc_common/prompts/understanding.py` — no changes.
- `packages/common/tbc_common/prompts/__init__.py` — `MODEL_VERSION` unchanged.
- `services/worker-understanding/tbc_worker_understanding/processor.py` — used as-is.
- `services/worker-understanding/tbc_worker_understanding/schema.py` — no changes.
- `services/worker-understanding/tbc_worker_understanding/ollama_client.py` — no changes.
- `services/worker-commitments/tbc_worker_commitments/extractor.py` — no changes (sanitizer + conf≥4 gate intact).

This change is structural orchestration only.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `TBC_UNDERSTANDING_MODE` | `brief-coupled` | `brief-coupled` (LLM on demand) or `continuous` (legacy 5s loop) |
| `TBC_BRIEF_PRE_UNDERSTANDING_TIMEOUT_S` | `300` | Max time worker-brief waits for the LLM queue to drain before proceeding anyway |
| `TBC_UNDERSTANDING_BATCH_MAX_N` | `20` | Max messages per LLM call (unchanged) |
| `TBC_UNDERSTANDING_MAX_CHATS_PER_BATCH` | `3` | Max chats per LLM call (unchanged) |
| `TBC_UNDERSTANDING_BATCH_CHAR_BUDGET` | `60000` | Char budget per LLM call (unchanged) |

## Operational notes

- **On-demand `/brief` is now slower** when the queue is non-empty. Typical drain for 200–500 messages: ~30–60s on Gemma 4 via novita. Logs show progress every 30s.
- **Drain timeout** is a backstop, not a happy path. If it fires, the brief still goes out — just with whatever was previously understood. The `understanding_drain_timeout` warning in `journalctl -u tbc-worker-brief` is alertable.
- **Manual on-demand drain** without firing a brief: `sudo -u tbc touch /tmp/tbc_trigger_understanding`.
- **Verify embed loop is alive**: `journalctl -u tbc-worker-understanding -f | grep embed_loop_batch` — should fire whenever new tagged-chat messages arrive.

## Files touched

- `packages/common/tbc_common/config.py` — new fields.
- `packages/common/tbc_common/db/understanding_queue.py` — new shared queue helpers.
- `services/worker-understanding/tbc_worker_understanding/main.py` — refactored entrypoint with mode dispatcher, `run_llm_bulk`, `embed_loop`, `trigger_watcher`.
- `services/worker-brief/tbc_worker_brief/main.py` — `_ensure_understanding_caught_up` + `run_brief_with_drain`.
- Tests: `services/worker-understanding/tests/test_main.py`, `services/worker-brief/tests/test_main.py`.

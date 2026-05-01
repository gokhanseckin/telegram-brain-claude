# Session 2026-05-01 — Understanding worker iteration log

This document is a **handoff to a fresh Claude session.** It captures what was tried, what worked, what didn't, and the exact state the worker is in right now (workers **stopped**; v11 prompt committed but not yet running).

---

## TL;DR — current state

- **Workers stopped** (`tbc-worker-understanding`, `tbc-worker-commitments`).
- **Latest committed code:** v11-resolve-vs-fabricate (`packages/common/tbc_common/prompts/__init__.py:12` → `MODEL_VERSION = "understanding-2026-05-01-v11-resolve-vs-fabricate"`).
- **Latest committed git rev:** `e04f4e3` on `main`, pushed to origin.
- **VPS at `root@116.203.2.243:/opt/tbc`:** has v11 code pulled (the previous deploy was v10, then the v11 prompt clarification was committed). Run `cd /opt/tbc && git pull` to confirm.
- **Database state:** 3 v9 commitments still present (`c10966`, `c10967`, `c10968`), 5 v8 commitments still present (`c10961-c10965`). User chose option (c) — keep older versions for side-by-side comparison.
- **Provider env active on VPS:**
  - `TBC_LLM_PROVIDER=anthropic` (brief / weekly / tg-bot all on `claude-sonnet-4-6`)
  - `TBC_UNDERSTANDING_PROVIDER=novita` + `TBC_UNDERSTANDING_NOVITA_MODEL=google/gemma-4-26b-a4b-it`
  - `TBC_UNDERSTANDING_BATCH_MAX_N=20`
  - `TBC_UNDERSTANDING_BATCH_CHAR_BUDGET=60000`
  - `TBC_UNDERSTANDING_MAX_CHATS_PER_BATCH=3` (hardcoded default in main.py — not in env file)
  - `TBC_UNDERSTANDING_PRIOR_CONTEXT_N=7` (hardcoded default — not in env file)
- **Poll window:** 3 days (`services/worker-understanding/tbc_worker_understanding/main.py:34`).

---

## What kicked off this session

The user posted a daily brief example with **5 problems**:

1. Names in the brief lacked `@username / tag` after them (e.g. "Doğa" not "Doğa / @unquaLe / colleague").
2. The model confused who "you" was — sometimes treating Gokhan (the user) as a third party in his own chats.
3. Multiple radar IDs were getting clustered at end of paragraph `(#4099, #a8ce, #86ab)` instead of inline per-observation.
4. Commitments were being invented for almost every chat, regardless of whether a real promise was made.
5. People with active commitments were also appearing in `WORTH NOTICING` — redundancy.

### The 5 brief-side fixes (all landed early in session)

Single commit `0887bcf` on `main` resolved them:
- assembler.py — added `@username` to chat label in cached context.
- brief.py — instruction to format names as `Name / @username / tag` in output bullets.
- brief.py — radar `(#xxxx)` now goes IMMEDIATELY after each observation, never batched at end.
- understanding.py + processor.py — `[YOU]` sender label so Qwen could tell who said what (commit attribution fix).
- prompt rule: skip people from `WORTH NOTICING` if they already appear in `ON YOUR PLATE` / `WAITING ON OTHERS` without genuinely new signal.

These all hit production cleanly. Brief now correctly shows `Barış / @barisalpaykut / personal —`.

After the brief fixes, the rest of the session was about **commitment quality** specifically.

---

## Provider migration arc

Started on **Qwen 2.5 7B (local Ollama)** which the worker had used since Phase 7. Qwen showed:
- Mis-attribution between user and counterparty (the [YOU] prefix didn't fully fix this on Qwen).
- High false-positive rate for commitments.
- ~30s/msg latency.

Switched to **DeepSeek Flash** (`deepseek-chat`). Provider routing added in `ollama_client.py` controlled by `TBC_UNDERSTANDING_PROVIDER` env var (`ollama|deepseek|novita`).

DeepSeek Flash run (v4-info, 313 msgs, 21 commits): **76% precision**.

Tried **DeepSeek v4-pro** briefly (v5-pro): 25-30s/msg latency — too slow for interactive iteration. Aborted.

Switched to **novita.ai with `google/gemma-4-26b-a4b-it`** (Gemma 4 26B MoE / 4B active, 256K context, $0.13/M input + $0.40/M output). This is the model PR #66 added. Verified live calls hit `https://api.novita.ai/v3/openai/chat/completions` in worker logs.

### Architectural change: batched calls

Before novita, the worker sent **1 message per LLM call** (with 3 prior messages as context). With novita's 256K context, this was wasteful. Built `process_message_batch` in `processor.py`:

- N messages enumerated as `=== Message #1 ===`, `=== Message #2 ===`, ...
- Model returns `{"results": [<obj1>, <obj2>, ...]}` envelope.
- Each result echoes its `id` field; processor matches results to inputs by id (handles model emitting extras/missing).
- Embeddings computed via `embed_batch` (already existed).
- Single DB transaction commits all rows.

### Architectural change: chat-aware batching (v9)

User pointed out that mixing chats in one batch dilutes attention. Implemented `_assemble_chat_aware_batch()` in `main.py`:

- If a single chat has >=N pending msgs → fill the whole batch from that one chat (coherent thread).
- Otherwise → round-robin across the top 3 chats, each emitted as a contiguous block.
- Configurable via `TBC_UNDERSTANDING_BATCH_MAX_N` and `TBC_UNDERSTANDING_MAX_CHATS_PER_BATCH`.

### Architectural change: prior context window 3 → 7 (v9)

The 3-message prior context window was too tight; antecedents like "Baranjemu" sometimes drifted 4-6 messages back. Bumped to 7 (env: `TBC_UNDERSTANDING_PRIOR_CONTEXT_N`).

---

## Prompt rule evolution (commitment quality)

Each MODEL_VERSION bump triggers reprocessing. Naming is `understanding-2026-05-01-v<N>-<tag>`.

| version | provider | prompt change | recall | precision | notes |
|---|---|---|---|---|---|
| v1 | Qwen 7B | (legacy) | high | low — 50% | wrong attribution common |
| v2-deepseek | DeepSeek Flash | + `[YOU]` rule | medium | 70% | attribution mostly fixed |
| v3-strict | DeepSeek Flash | + third-party / status / questions / acks NOT commitments | medium | 81% | clean precision win |
| v4-info | DeepSeek Flash | + FACTS rule (sharing addresses ≠ commitment) + extractor confidence floor (None or <4 → drop) | medium | **76%** (21/313 msgs) | best DeepSeek run |
| v5-pro | DeepSeek v4-pro | (same prompt) | medium | n/a | latency too high — aborted |
| v6 | Gemma 4 (novita) batched-20 | (same v4 prompt + batch envelope) | low | **86%** (7/280) | best precision so far; 0 parse failures |
| v7-tokenbudget-resolves | Gemma 4 batched-30 + auto-resolve | + `resolves: <id>` field for in-batch fulfillment detection | medium | 70% (10/266) | regression — bigger batches confused [YOU] tracking; auto-resolve never fired (no fulfillment in 3d window) |
| v8-explicit-what | Gemma 4 batched-30 | + COMMITMENT.WHAT MUST BE SELF-CONTAINED rule with BAD/GOOD examples | low | partial — see below | "tell the person" / "forward the information" still slipped through 3/5 cases |
| v9-chat-grouped-7ctx | Gemma 4 batched-20 chat-aware + 7-msg prior context | (same v8 prompt) | low | only 3 commits before user stopped | "Baris will forward the information to the relevant person" — placeholder words still leaked |
| **v10-strict-what** | (deployed, then stopped) | + explicit BANNED phrases list ("the person", "the user", "him", "the information" alone), 6 GOOD examples, "(recipient unclear from context)" fallback | not measured | not measured | workers stopped before any commit landed |
| **v11-resolve-vs-fabricate** | (committed, NOT deployed) | clarified the contradiction between "do not import from prior context" (anti-fabrication) and "use names from prior context" (anti-pronoun) | TBD | TBD | this is the version waiting to run |

---

## v8 quality table — preserved for comparison

These 5 commitments were generated by **v8** (Gemma 4, batch=30, 3-msg context). They demonstrate the failure modes that v9-v11 are trying to fix.

| commit_id | source msg (Turkish) | translation | attr | description (commit_what) | verdict |
|---|---|---|---|---|---|
| **c10961** | "Hemen iletiyorum" | "I'm forwarding immediately" | Barış / counterparty | "forward the envelope instructions (Baranjemu Dyukeru, Keppan 10k, Keiko 3k) to the recipient" | ⚠️ topic captured ✅ but recipient = "the recipient" ❌ |
| **c10962** | "Tamamdır hocam söylüyorum" | "OK master, I'm telling [him]" | Barış / counterparty | "tell the person/instructor (hocam) the information" | ❌ both target AND topic vague |
| **c10963** | "Tamamdır hemen söylüyorum" | "OK, I'm telling immediately" | Barış / counterparty | "tell the relevant person about the printing/writing instructions" | ❌ "the relevant person" — antecedent "Baran" was 3 msgs back, model saw it but ignored |
| **c10964** | "bir dahaki sefere 'yeniden' önceden uyarayım. bir daha yaparsa almayız keikoya" | "Next time let me warn beforehand. If he does it again we won't take to keiko" | YOU / user | "warn Baran again next time and not take him to keikoya if he repeats the behavior" | ✅ excellent — names Baran, action, condition, consequence |
| **c10965** | "Günaydın hocam. Tabii ben bugün tekrar arayıp konuşayım. Hatta elektrik süpürgesi aldığımızı da belirteceğim" | "Good morning. Of course I'll call again today and talk. I'll also mention we bought a vacuum" | Barış / counterparty | "Barış will call the manager again today to discuss the weekly deletion issue and mention the vacuum cleaner purchase" | ✅ excellent — names self, recipient role (manager), both topics |

**Pattern observed:** the rule works when the antecedent is in the immediate prior context AND named clearly. It fails when the message is a generic acknowledgement ("Tamamdır") even if the name was visible to the model. Smaller MoE models with 4B active params drop weak attention signals.

---

## v9 commits (3, before user halted)

| commit_id | source | description | verdict |
|---|---|---|---|
| c10966 | Barış: announcement message | "make an announcement regarding the May 1 holiday" | ❌ no audience |
| c10967 | Barış: report observations | "report yesterday's observations to the user tomorrow morning" | ⚠️ "the user" generic; should be "Gokhan" |
| c10968 | Barış: forwarding | "Baris will forward the information to the relevant person" | ❌ both "info" + "person" generic |

These are still in the database; user picked option (c) (preserve all old-version data for comparison).

---

## Key files modified this session

| file | what changed |
|---|---|
| `services/worker-understanding/tbc_worker_understanding/ollama_client.py` | provider routing for ollama/deepseek/novita; chat() and chat_batch() methods |
| `services/worker-understanding/tbc_worker_understanding/processor.py` | `[YOU]` sender labels; `process_message_batch`; auto-resolve via `resolves` field; prior context env-driven (default 7) |
| `services/worker-understanding/tbc_worker_understanding/main.py` | poll LIMIT 100→200; `_assemble_chat_aware_batch()`; chat breakdown logged |
| `services/worker-understanding/tbc_worker_understanding/schema.py` | default values for all schema fields (resilience against Gemma dropping fields on big batches) |
| `services/worker-commitments/tbc_worker_commitments/extractor.py` | confidence filter `None or <4 → drop` (was just `<3`) |
| `packages/common/tbc_common/prompts/understanding.py` | all the prompt rule iterations (third-party, status, questions, acks, FACTS, explicit-what, strict-what, resolve-vs-fabricate) |
| `packages/common/tbc_common/prompts/__init__.py` | MODEL_VERSION bumps |
| `services/worker-brief/tbc_worker_brief/assembler.py` | `[YOU]` sender labels; chat label with `@username` and tag; per-commitment source-text + summary + 3-msg prior thread blocks |
| `packages/common/tbc_common/prompts/brief.py` | the 5 brief-side fixes from earlier in the session |
| `CLAUDE.md` (repo root) | data-deletion confirmation rule |

---

## CLAUDE.md rule added this session

User asked me to add a rule: **always confirm before deleting data.** Quote the exact statement and rowcount, wait for explicit yes, don't chain destructive ops with restarts. Prefer `MODEL_VERSION` bumps for reprocessing (non-destructive — old rows just become invisible to the new poll, you can compare side-by-side).

The rule is enforced by both `CLAUDE.md` at repo root AND a memory file at `~/.claude/projects/-Users-gokhanseckin-claude-projects-telegram-brain-claude/memory/feedback_data_deletion.md`.

The harness already prevented one violation this session — when I tried to chain `DELETE ... && systemctl restart ...`, the permission system blocked it. Working as designed.

---

## What v11 is supposed to fix

The strict-what rule (v10) said BOTH:
- "Look in prior context and use the proper name" (anti-pronoun)
- (carried over from earlier) "do not import content, intent, or actions from prior messages into the description" (anti-fabrication)

These are not actually contradictory but to a small model they look like mixed signals. v11 reframes:

> The ACTION must come from the target message. The NAMES must come from prior context. These two are complementary, not in tension.

If v11 still produces "the relevant person" placeholders, the next things to try:
1. Bump max prior context to 10+.
2. Lower batch size further (back to 15 or even single-message processing for sensitive cases).
3. Try a stronger model (Gemma 4 31B or Sonnet for understanding too — at $0.13/M input the cost is negligible compared to Sonnet's $3/M).
4. Few-shot the prompt with actual recent Telegram conversation examples (would require careful examples — currently rules-only).

---

## How to resume

```bash
# 1. Confirm code state
cd /Users/gokhanseckin/claude-projects/telegram-brain-claude
git log -1 --oneline   # should show e04f4e3

# 2. Confirm VPS state
ssh root@116.203.2.243 "cd /opt/tbc && git log -1 --oneline && grep MODEL_VERSION packages/common/tbc_common/prompts/__init__.py"
# expected: e04f4e3 + v11-resolve-vs-fabricate

# 3. Start workers (no wipe needed — MODEL_VERSION bump triggers fresh reprocess)
ssh root@116.203.2.243 "systemctl start tbc-worker-understanding tbc-worker-commitments"

# 4. Watch new commits land
ssh root@116.203.2.243 "sudo -u postgres psql tbc -c \"
  SELECT c.id,
         CASE WHEN m.sender_id=417601774 THEN 'YOU' ELSE COALESCE(u.first_name,'?') END AS sender,
         c.owner, (mu.commitment->>'confidence')::int AS conf,
         COALESCE(ch.title,'?') AS chat, c.description
  FROM commitments c
  JOIN message_understanding mu ON mu.chat_id=c.chat_id AND mu.message_id=c.source_message_id
  JOIN messages m ON m.chat_id=c.chat_id AND m.message_id=c.source_message_id
  LEFT JOIN users u ON u.user_id=m.sender_id
  LEFT JOIN chats ch ON ch.chat_id=c.chat_id
  WHERE mu.model_version='understanding-2026-05-01-v11-resolve-vs-fabricate'
  ORDER BY c.id;
\""

# 5. Trigger a fresh brief once enough commits have accumulated
ssh root@116.203.2.243 "sudo -u tbc touch /tmp/tbc_trigger_brief"
```

The 3-day pending count was 222 messages right before workers stopped. Expect ~5-10 commits after full reprocess at v11.

---

## Open questions / nice-to-haves

- **Auto-resolve never fired** — the `resolves: <id>` mechanism works in code but no real fulfillment+commitment pair lived in the same 3-day batch in our test data. Worth keeping the code; will fire naturally as the system runs over time.
- **Brief assembler** now passes 3-msg prior thread per commitment to Claude, which was a fallback for when Gemma's `commit.what` was vague. Once `commit.what` becomes reliably self-contained, the 3-msg thread block becomes redundant — could be removed for cleaner brief input. Don't remove yet.
- **The brief still uses Anthropic** (claude-sonnet-4-6). User explicitly chose this split: novita+Gemma for understanding (cheap, batchable), Anthropic+Sonnet for the brief (high quality, low volume).
- **Tagging table** — system has 6 tags, of which 38 chats are `prospect` and 27 are `partner` but both have ~0 messages in the 3d window. The system handles this fine — those chats just don't appear in the brief.
- **The 5 v8 commits and 3 v9 commits in the DB** — left intact per user choice. They'll naturally age out as new versions take over, or can be cleaned up manually with explicit `DELETE WHERE model_version='...'`.

---

## File this is saved as

`docs/session-2026-05-01-understanding-worker-iteration.md`

Committed alongside this message. Future Claude can read it to understand the full arc of changes.

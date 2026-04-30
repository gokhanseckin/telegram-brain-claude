# Prompt for next session — Unified DM router (Qwen-first, Claude as fallback)

> Use this as the opening message in a new Claude Code session. Read
> `docs/2026-04-29-session-changes.md` first as background — it
> documents everything shipped in the prior session and is the source
> of truth for what already exists.

---

## What I want you to build

A **unified natural-language DM handler** for the Telegram bot that
routes incoming DMs through a local Qwen 2.5 7B (via Ollama) and only
escalates to Claude (Anthropic API) when Qwen explicitly delegates.
The goal is to (a) close the asymmetry where natural-language works
for commitments but not for brief feedback, and (b) cut my Claude API
spend by handling simple actions locally.

## Hard constraints

These are non-negotiable. The implementation MUST enforce them in code,
not just in prompts.

### Cost guardrail (CRITICAL)

- **Qwen MUST NOT loop or chain Claude calls.** A single user DM may
  result in **at most ONE Claude API call**, ever.
- **Claude is called only when Qwen explicitly emits a `delegate_to_claude`
  decision.** No automatic fallthrough on parse failure, ambiguity, or
  "let me think harder."
- **If Qwen fails to produce a valid decision** (malformed JSON, no
  recognized intent, model error), respond to the user with a clear
  message asking to rephrase — do NOT silently fall through to Claude.
- **Claude calls do not recurse.** Claude's response is returned
  verbatim to the user. Claude does not call back into the router.
- **No background polling, no retry-on-failure-into-Claude, no batching
  multiple DMs into one Claude call.** One DM in, one final response
  out.

I should be able to inspect the code and prove that Claude can be
called at most once per `handle_text` invocation. Treat this like a
safety property, not a soft preference.

### Qwen judgment quality

Qwen needs reliable judgment about whether to handle locally or
delegate. The router prompt must:

- Give Qwen explicit examples for each intent class (feedback,
  commitment_resolve, commitment_cancel, commitment_update, qa,
  ambiguous).
- Force a structured JSON response with `intent`, `confidence`, and
  per-intent fields.
- Reject low-confidence local decisions (configurable threshold, e.g.,
  `< 0.7`) → those become "ambiguous" and the user is asked to clarify
  rather than hitting Claude. Only DMs Qwen confidently classifies as
  Q&A should reach Claude.
- Ship with a small eval set (~20 example DMs) that I can run to
  benchmark routing accuracy.

### Behavior rules — please follow these even if I forget to repeat them

- Read `docs/2026-04-29-session-changes.md` first. Don't re-discover
  context that's already there.
- Keep changes scoped. Don't refactor unrelated code.
- All new code must have tests. SQLite-in-memory pattern via the
  existing `conftest.py` is fine.
- Lint-clean (ruff) and typecheck-clean.
- Open one PR per logical change, not a giant bundle.
- Confirm with me before deploying to prod or running destructive SQL.
- Keep commit messages explanatory: WHY, not just WHAT.

## Recommended two-stage delivery

### Stage 1 — Close the feedback asymmetry (~30 minutes, no router yet)

Goal: natural-language brief feedback works through the existing
Claude agent path. Removes the slash-command-only limitation. Pure
addition; no Qwen involved yet.

Tasks:
1. Add MCP write tool `write_brief_feedback(item_ref?, feedback_type,
   note?)`. Mirror the structure of `resolve_commitment` in
   `services/mcp-server/tbc_mcp_server/tools/commitments.py`.
2. Register it in `services/mcp-server/tbc_mcp_server/main.py`
   (`handle_list_tools` and `_dispatch_tool`).
3. Update `services/tg-bot/tbc_bot/agent.py` SYSTEM_PROMPT with the
   feedback section: "When the user says an item was useful / not
   useful / missed, call write_brief_feedback. If they reference a
   `#xxxx` tag, pass it as item_ref. Otherwise pass None and use
   feedback_type='missed_important'. Always confirm in your reply
   what you wrote."
4. Tests for the write tool (8-10 cases covering each feedback_type,
   missing fields, etc.). Use the SQLite conftest pattern.
5. Update README's "Brief feedback loop" section to mention that
   natural-language DM also works.
6. Deploy and trigger a manual brief to verify the loop end-to-end.

After Stage 1: feedback works via DM. Cost is unchanged (still Claude).
Asymmetry is closed.

### Stage 2 — Qwen router (the meaningful refactor)

Goal: Qwen handles all action-shaped DMs locally. Claude only for Q&A.
Hard constraint: max 1 Claude call per DM, only when Qwen says so.

Tasks:
1. **New module `services/tg-bot/tbc_bot/router.py`** that:
   - Takes a user DM text.
   - Calls Qwen 2.5 7B via the existing `OllamaClient` with a strict
     JSON schema response.
   - Returns a `RouterDecision` with `intent`, `confidence`, and
     per-intent fields.

2. **Router prompt** in a new `services/tg-bot/tbc_bot/router_prompt.py`
   that:
   - Defines the intents: `feedback`, `commitment_resolve`,
     `commitment_cancel`, `commitment_update`, `qa`, `ambiguous`.
   - Includes 2-3 examples per intent.
   - Demands structured JSON output (use the same `_extract_json_object`
     pattern used in `worker-chat-tagger/classifier.py`).
   - Uses a constrained schema: `{intent, confidence, ...fields}`.

3. **Router executors**: per-intent handlers that call MCP tools
   directly (not via Claude). Use synchronous SQLAlchemy session — the
   bot is async but DB writes can be wrapped in `asyncio.to_thread`.

4. **Confidence threshold**: configurable env var
   `TBC_ROUTER_MIN_CONFIDENCE=0.7`. Below this, respond to user with
   "I wasn't sure if you meant X or Y — can you clarify?" and DO NOT
   call Claude. This is the loop guard.

5. **Single Claude path**: if intent is `qa` (and only then), call the
   existing `agent.ask()` exactly once. The agent retains MCP tool
   access for the Q&A — that's fine, it's still one network call from
   the user's perspective.

6. **Update `services/tg-bot/tbc_bot/handlers/chat.py`** to call the
   router first, dispatch to local executor or single Claude call.

7. **Slash commands stay** as power-user shortcuts (no LLM
   involvement). `/feedback`, `/done`, etc. become the bypass for when
   you don't want to spend even a Qwen call.

8. **Counter / observability**: log every router decision with
   `{intent, confidence, claude_called: bool, ms_local, ms_claude}`.
   So you can audit "is Claude getting called more than expected?"
   from journalctl.

9. **Eval set** under `services/tg-bot/tests/test_router_eval.py`:
   20-30 example DMs with expected intents. Run as a normal test —
   uses a stub Ollama client returning deterministic JSON. Catches
   prompt regressions before they hit prod.

10. **Tests** for the router and each executor (intent dispatch, low
    confidence path, Claude exactly-once, MCP write side effects).

11. **Update README** with the new architecture diagram and a note that
    "Free-text DMs are routed locally through Qwen first; Q&A goes to
    Claude. Cost dropped from $X/day to $Y/day."

After Stage 2: ~80%+ of DMs handled locally for free. Quality of
action handling depends on Qwen 2.5 7B; my concern is that ambiguous
phrasings degrade gracefully (ask to clarify) rather than escalate
silently.

## Edge cases I want explicitly covered

- "I sent the report and also paid Bob" — two actions in one DM.
  Decide: does router handle both sequentially, or ask user to
  separate? My preference: sequentially, but only if both intents are
  high-confidence. If either is low-confidence, ask.
- "Did I commit to send the report?" — this is Q&A about commitments,
  NOT a resolution action. Router must distinguish.
- "Forget about it" — needs context (what's "it"?). Should be
  ambiguous, asked to clarify.
- Mixed-language input (Turkish + English). Qwen 2.5 7B handles
  multilingual reasonably; eval set should include some Turkish.
- A `/feedback #xxxx ...` slash command that the user typed should
  bypass the router entirely. Slash commands have their own handler
  in `feedback.py` and `commands.py` already.
- The agent prompt's `[meta] current_message_id=N` injection (added
  in PR #36) should still work for the Claude-fallback path so
  resolve_commitment can record the triggering DM.

## What I do NOT want

- Don't extend `/feedback` to be smarter — leave it as a precise
  bypass.
- Don't introduce a separate LLM beyond Qwen 2.5 7B (already on the
  VPS) and Claude. No third model.
- Don't add OpenAI, Mistral, or any other paid provider as a fallback.
- Don't add streaming. Bot DMs are conversational; one shot in, one
  shot out.
- Don't auto-correct routing mistakes by retrying with Claude. If
  Qwen got it wrong, a simple human "no, I meant..." in the next DM
  fixes it — that's a new request, the budget resets cleanly.

## Files to read first (in order)

1. `docs/2026-04-29-session-changes.md` — what already exists
2. `services/tg-bot/tbc_bot/agent.py` — current Claude-only path
3. `services/tg-bot/tbc_bot/handlers/chat.py` — current free-text handler
4. `services/tg-bot/tbc_bot/handlers/feedback.py` — current `/feedback` slash
5. `services/mcp-server/tbc_mcp_server/tools/commitments.py` — pattern for MCP write tools (resolve, cancel, update)
6. `services/worker-understanding/tbc_worker_understanding/ollama_client.py` — Ollama HTTP client to reuse
7. `services/worker-chat-tagger/tbc_worker_chat_tagger/classifier.py` — example of Qwen-with-function-calling pattern (Stage B)
8. `services/worker-chat-tagger/tbc_worker_chat_tagger/prompts.py` — example of structured prompt + JSON output

## Where to start

Confirm you've read the changes doc, then propose Stage 1 first.
Don't ship Stage 2 in the same PR as Stage 1 — they're independently
useful and Stage 1 is a quick win. Wait for me to greenlight Stage 2
before starting it.

If anything in the changes doc contradicts the current state of `main`
(unlikely but possible if I made changes between sessions), trust the
code and tell me.

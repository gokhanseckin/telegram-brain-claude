"""Shared Ollama HTTP client.

Lifted out of `tbc_worker_understanding` so the bot's router can reuse
it without a cross-service dependency. Behavior matches the original
client; the only addition is the optional `format` parameter on
`chat()` so callers can opt into Ollama's native JSON-mode output.
"""

from __future__ import annotations

import httpx

# CPU-only Ollama on this VPS shares models between worker-understanding,
# worker-chat-tagger, and the bot router. Two operational settings keep
# that workable:
# - keep_alive="60m" so a model stays in RAM across a quiet stretch
#   instead of unloading every 4 minutes (the default) and incurring a
#   ~120s cold-load on the next request.
# - per-call timeout 300s so the occasional cold-load or contended call
#   doesn't tip the caller into a retry storm.
KEEP_ALIVE = "60m"
CHAT_TIMEOUT_SECONDS = 300.0
EMBED_TIMEOUT_SECONDS = 120.0
EMBED_BATCH_TIMEOUT_SECONDS = 300.0


class OllamaClient:
    """Minimal async client for the Ollama inference server."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def chat(
        self,
        model: str,
        system: str,
        user: str,
        format: str | None = None,
    ) -> str:
        """Call /api/chat and return the assistant message content string.

        Pass `format="json"` to enable Ollama's structured-output mode —
        the model is constrained to emit a valid JSON object. Quietly
        unsupported models still return a string; we still parse it on
        the caller side, so the worst case is "no constraint applied"
        rather than a crash.
        """
        payload: dict[str, object] = {
            "model": model,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if format is not None:
            payload["format"] = format
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=CHAT_TIMEOUT_SECONDS
        ) as client:
            response = await client.post("/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            return data["message"]["content"]  # type: ignore[no-any-return]

    async def embed(self, model: str, input: str) -> list[float]:
        """Call /api/embed and return the first embedding vector."""
        payload = {"model": model, "input": input, "keep_alive": KEEP_ALIVE}
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=EMBED_TIMEOUT_SECONDS
        ) as client:
            response = await client.post("/api/embed", json=payload)
            response.raise_for_status()
            data = response.json()
            return data["embeddings"][0]  # type: ignore[no-any-return]

    async def embed_batch(self, model: str, inputs: list[str]) -> list[list[float]]:
        """Call /api/embed with a list of inputs and return a parallel list of vectors."""
        payload = {"model": model, "input": inputs, "keep_alive": KEEP_ALIVE}
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=EMBED_BATCH_TIMEOUT_SECONDS
        ) as client:
            response = await client.post("/api/embed", json=payload)
            response.raise_for_status()
            data = response.json()
            return data["embeddings"]  # type: ignore[no-any-return]

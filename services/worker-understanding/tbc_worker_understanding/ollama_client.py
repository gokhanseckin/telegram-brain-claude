"""Thin httpx-based wrapper around the Ollama REST API."""

from __future__ import annotations

import httpx

# CPU-only Ollama on this VPS shares two models (qwen2.5 chat, bge-m3
# embeddings) between worker-understanding, worker-chat-tagger, and bot
# follow-up calls. Two operational settings keep that workable:
# - keep_alive="60m" so a model stays in RAM across a quiet stretch
#   instead of unloading every 4 minutes (the default) and incurring a
#   ~120s cold-load on the next request.
# - per-call timeout 300s so the occasional cold-load or contended call
#   doesn't tip the worker into a retry storm of failed messages.
KEEP_ALIVE = "60m"
CHAT_TIMEOUT_SECONDS = 300.0
EMBED_TIMEOUT_SECONDS = 120.0
EMBED_BATCH_TIMEOUT_SECONDS = 300.0


class OllamaClient:
    """Minimal async client for the Ollama inference server."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def chat(self, model: str, system: str, user: str) -> str:
        """Call /api/chat and return the assistant message content string."""
        payload = {
            "model": model,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
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

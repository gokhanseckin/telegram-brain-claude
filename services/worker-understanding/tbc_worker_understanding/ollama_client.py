"""Thin httpx-based wrapper around the Ollama REST API."""

from __future__ import annotations

import httpx


class OllamaClient:
    """Minimal async client for the Ollama inference server."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def chat(self, model: str, system: str, user: str) -> str:
        """Call /api/chat and return the assistant message content string."""
        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as client:
            response = await client.post("/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            return data["message"]["content"]  # type: ignore[no-any-return]

    async def embed(self, model: str, input: str) -> list[float]:
        """Call /api/embed and return the first embedding vector."""
        payload = {"model": model, "input": input}
        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as client:
            response = await client.post("/api/embed", json=payload)
            response.raise_for_status()
            data = response.json()
            return data["embeddings"][0]  # type: ignore[no-any-return]

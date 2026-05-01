"""Provider-routing client. chat() and chat_batch() route to ollama|deepseek|novita;
embeddings always Ollama. Original at .bak."""

from __future__ import annotations

import os

import httpx

KEEP_ALIVE = "60m"
CHAT_TIMEOUT_SECONDS = 300.0
EMBED_TIMEOUT_SECONDS = 120.0
EMBED_BATCH_TIMEOUT_SECONDS = 300.0
NOVITA_BATCH_TIMEOUT = 300.0


class OllamaClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def _provider(self) -> str:
        return os.environ.get("TBC_UNDERSTANDING_PROVIDER", "ollama").lower()

    async def chat(self, model: str, system: str, user: str) -> str:
        p = self._provider()
        if p == "deepseek":
            return await self._chat_deepseek(system, user)
        if p == "novita":
            return await self._chat_novita(system, user)
        return await self._chat_ollama(model, system, user)

    async def chat_batch(self, system: str, user: str) -> str:
        """Single-call batch chat — same shape as chat() but with novita timeout/limits."""
        p = self._provider()
        if p == "novita":
            return await self._chat_novita(system, user, max_tokens=int(os.environ.get("TBC_UNDERSTANDING_MAX_TOKENS", "8000")))
        if p == "deepseek":
            return await self._chat_deepseek(system, user, max_tokens=int(os.environ.get("TBC_UNDERSTANDING_MAX_TOKENS", "8000")))
        return await self._chat_ollama(os.environ.get("TBC_UNDERSTANDING_MODEL", "qwen2.5:7b-instruct-q4_K_M"), system, user)

    async def _chat_ollama(self, model: str, system: str, user: str) -> str:
        payload = {
            "model": model,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        async with httpx.AsyncClient(base_url=self._base_url, timeout=CHAT_TIMEOUT_SECONDS) as client:
            r = await client.post("/api/chat", json=payload)
            r.raise_for_status()
            return r.json()["message"]["content"]

    async def _chat_deepseek(self, system: str, user: str, *, max_tokens: int = 1000) -> str:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not set")
        model = os.environ.get("TBC_UNDERSTANDING_DEEPSEEK_MODEL", "deepseek-chat")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=NOVITA_BATCH_TIMEOUT) as client:
            r = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def _chat_novita(self, system: str, user: str, *, max_tokens: int = 8000) -> str:
        api_key = os.environ.get("NOVITA_API_KEY")
        if not api_key:
            raise RuntimeError("NOVITA_API_KEY not set")
        model = os.environ.get("TBC_UNDERSTANDING_NOVITA_MODEL", "google/gemma-4-26b-a4b-it")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=NOVITA_BATCH_TIMEOUT) as client:
            r = await client.post(
                "https://api.novita.ai/v3/openai/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def embed(self, model: str, input: str) -> list[float]:
        payload = {"model": model, "input": input, "keep_alive": KEEP_ALIVE}
        async with httpx.AsyncClient(base_url=self._base_url, timeout=EMBED_TIMEOUT_SECONDS) as client:
            r = await client.post("/api/embed", json=payload)
            r.raise_for_status()
            return r.json()["embeddings"][0]

    async def embed_batch(self, model: str, inputs: list[str]) -> list[list[float]]:
        payload = {"model": model, "input": inputs, "keep_alive": KEEP_ALIVE}
        async with httpx.AsyncClient(base_url=self._base_url, timeout=EMBED_BATCH_TIMEOUT_SECONDS) as client:
            r = await client.post("/api/embed", json=payload)
            r.raise_for_status()
            return r.json()["embeddings"]

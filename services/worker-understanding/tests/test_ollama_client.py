"""Unit tests for OllamaClient using respx to mock HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from tbc_worker_understanding.ollama_client import OllamaClient


@pytest.fixture
def client() -> OllamaClient:
    return OllamaClient("http://localhost:11434")


@pytest.mark.asyncio
async def test_chat_returns_content(client: OllamaClient) -> None:
    with respx.mock(base_url="http://localhost:11434") as mock:
        mock.post("/api/chat").mock(
            return_value=httpx.Response(
                200,
                json={"message": {"role": "assistant", "content": "Hello, world!"}},
            )
        )
        result = await client.chat(model="test-model", system="sys", user="hi")
    assert result == "Hello, world!"


@pytest.mark.asyncio
async def test_embed_returns_vector(client: OllamaClient) -> None:
    vector = [0.1, 0.2, 0.3]
    with respx.mock(base_url="http://localhost:11434") as mock:
        mock.post("/api/embed").mock(
            return_value=httpx.Response(200, json={"embeddings": [vector]})
        )
        result = await client.embed(model="test-model", input="hello")
    assert result == vector


@pytest.mark.asyncio
async def test_chat_http_error_raises(client: OllamaClient) -> None:
    with respx.mock(base_url="http://localhost:11434") as mock:
        mock.post("/api/chat").mock(
            return_value=httpx.Response(500, json={"error": "internal server error"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat(model="test-model", system="sys", user="hi")

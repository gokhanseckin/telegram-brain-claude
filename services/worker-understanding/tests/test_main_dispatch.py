"""Tests for the brief-coupled mode wiring in worker-understanding.main.

These tests stay at the orchestration layer — they mock _poll,
process_message_batch, the OllamaClient, and settings, and verify that:

- run_llm_bulk drains the queue (loops until _poll returns empty)
- trigger_watcher runs the bulk drain when the trigger file appears
- main() dispatches to the right entrypoint per TBC_UNDERSTANDING_MODE
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def fake_session_factory():
    session = MagicMock()
    factory = MagicMock(return_value=session)
    factory.return_value.__enter__.return_value = session
    factory.return_value.__exit__.return_value = False
    return factory


def test_run_llm_bulk_loops_until_empty(fake_session_factory):
    from tbc_worker_understanding import main as wu_main

    # _run_one_batch returns 5, then 3, then 0 (queue drained).
    counts = iter([5, 3, 0])

    async def fake_run_one_batch(**_kwargs):
        return next(counts)

    ollama = MagicMock()
    with patch.object(wu_main, "_run_one_batch", side_effect=fake_run_one_batch) as one_batch:
        total = asyncio.run(
            wu_main.run_llm_bulk(
                session_factory=fake_session_factory,
                ollama=ollama,
                batched_prompt="sys-batched",
                understanding_prompt="sys",
            )
        )

    assert total == 8
    assert one_batch.call_count == 3


def test_run_llm_bulk_returns_zero_when_queue_empty(fake_session_factory):
    from tbc_worker_understanding import main as wu_main

    async def fake_run_one_batch(**_kwargs):
        return 0

    with patch.object(wu_main, "_run_one_batch", side_effect=fake_run_one_batch) as one_batch:
        total = asyncio.run(
            wu_main.run_llm_bulk(
                session_factory=fake_session_factory,
                ollama=MagicMock(),
                batched_prompt="sys-batched",
                understanding_prompt="sys",
            )
        )
    assert total == 0
    assert one_batch.call_count == 1


def test_trigger_watcher_runs_bulk_on_file(fake_session_factory, tmp_path):
    from tbc_worker_understanding import main as wu_main

    trigger = tmp_path / "tbc_trigger_understanding"
    trigger.touch()

    bulk_mock = AsyncMock(return_value=7)
    sleep_calls = {"n": 0}

    async def fake_sleep(_):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 1:
            raise asyncio.CancelledError

    with (
        patch.object(wu_main, "UNDERSTANDING_TRIGGER_FILE", str(trigger)),
        patch.object(wu_main, "run_llm_bulk", new=bulk_mock),
        patch.object(wu_main.asyncio, "sleep", side_effect=fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(
            wu_main.trigger_watcher(
                session_factory=fake_session_factory,
                ollama=MagicMock(),
                batched_prompt="sys-batched",
                understanding_prompt="sys",
            )
        )

    bulk_mock.assert_called_once()
    assert not trigger.exists(), "trigger file should be deleted after pickup"


def test_trigger_watcher_noop_without_file(fake_session_factory, tmp_path):
    from tbc_worker_understanding import main as wu_main

    trigger = tmp_path / "tbc_trigger_understanding"
    bulk_mock = AsyncMock()

    async def cancel_after_one_sleep(_):
        raise asyncio.CancelledError

    with (
        patch.object(wu_main, "UNDERSTANDING_TRIGGER_FILE", str(trigger)),
        patch.object(wu_main, "run_llm_bulk", new=bulk_mock),
        patch.object(wu_main.asyncio, "sleep", side_effect=cancel_after_one_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(
            wu_main.trigger_watcher(
                session_factory=fake_session_factory,
                ollama=MagicMock(),
                batched_prompt="sys-batched",
                understanding_prompt="sys",
            )
        )

    bulk_mock.assert_not_called()


def test_main_dispatches_continuous():
    from tbc_worker_understanding import main as wu_main

    with (
        patch.object(wu_main.settings, "understanding_mode", "continuous"),
        patch.object(wu_main.asyncio, "run") as run_mock,
        patch.object(wu_main, "_run_continuous") as cont_mock,
        patch.object(wu_main, "_run_brief_coupled") as bc_mock,
    ):
        cont_mock.return_value = MagicMock()  # avoid unawaited-coroutine warning
        wu_main.main()

    run_mock.assert_called_once()
    cont_mock.assert_called_once()
    bc_mock.assert_not_called()


def test_main_dispatches_brief_coupled():
    from tbc_worker_understanding import main as wu_main

    with (
        patch.object(wu_main.settings, "understanding_mode", "brief-coupled"),
        patch.object(wu_main.asyncio, "run") as run_mock,
        patch.object(wu_main, "_run_continuous") as cont_mock,
        patch.object(wu_main, "_run_brief_coupled") as bc_mock,
    ):
        bc_mock.return_value = MagicMock()
        wu_main.main()

    run_mock.assert_called_once()
    bc_mock.assert_called_once()
    cont_mock.assert_not_called()


def test_main_rejects_unknown_mode():
    from tbc_worker_understanding import main as wu_main

    with (
        patch.object(wu_main.settings, "understanding_mode", "garbage"),
        pytest.raises(RuntimeError, match="TBC_UNDERSTANDING_MODE"),
    ):
        wu_main.main()

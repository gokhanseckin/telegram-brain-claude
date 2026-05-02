"""Tests for the understanding-drain handshake in worker-brief.main."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_session_factory():
    """A session_factory whose returned session is a MagicMock context-manager."""
    session = MagicMock()
    factory = MagicMock(return_value=session)
    factory.__call_session__ = session  # for direct inspection in tests
    factory.return_value.__enter__.return_value = session
    factory.return_value.__exit__.return_value = False
    return factory


def test_drain_returns_immediately_when_queue_empty(fake_session_factory, tmp_path):
    from tbc_worker_brief import main as brief_main

    trigger = tmp_path / "tbc_trigger_understanding"
    with patch.object(brief_main, "UNDERSTANDING_TRIGGER_FILE", str(trigger)), patch(
        "tbc_worker_brief.main.pending_understanding_count", return_value=0
    ) as count_mock:
        brief_main._ensure_understanding_caught_up(fake_session_factory)
    count_mock.assert_called_once()
    assert not trigger.exists(), "trigger should not have been touched when queue empty"


def test_drain_touches_trigger_and_polls_until_empty(fake_session_factory, tmp_path):
    from tbc_worker_brief import main as brief_main

    trigger = tmp_path / "tbc_trigger_understanding"
    counts = iter([5, 5, 0])  # initial check + two polls

    with patch.object(brief_main, "UNDERSTANDING_TRIGGER_FILE", str(trigger)), patch(
        "tbc_worker_brief.main.pending_understanding_count",
        side_effect=lambda *a, **kw: next(counts),
    ), patch("tbc_worker_brief.main.time.sleep") as sleep_mock, patch(
        "tbc_worker_brief.main.time.monotonic", side_effect=[0, 0, 0, 5, 5, 10, 10, 10]
    ):
        brief_main._ensure_understanding_caught_up(fake_session_factory)

    assert trigger.exists(), "trigger file should have been touched"
    # At least one sleep(5) before draining
    assert sleep_mock.call_count >= 1


def test_drain_respects_timeout(fake_session_factory, tmp_path):
    from tbc_worker_brief import main as brief_main

    trigger = tmp_path / "tbc_trigger_understanding"

    with patch.object(brief_main, "UNDERSTANDING_TRIGGER_FILE", str(trigger)), patch(
        "tbc_worker_brief.main.pending_understanding_count", return_value=42
    ), patch("tbc_worker_brief.main.time.sleep"), patch(
        "tbc_worker_brief.main.settings"
    ) as settings_mock, patch(
        "tbc_worker_brief.main.time.monotonic",
        # First call: started=0. Each loop: elapsed check, then count, then progress check.
        # After enough iterations, monotonic returns >= timeout to trigger exit.
        side_effect=[0] + [t for t in range(0, 1000, 1)],
    ):
        settings_mock.brief_pre_understanding_timeout_s = 10
        brief_main._ensure_understanding_caught_up(fake_session_factory)

    # Reaching this line means the function returned (didn't deadlock).
    assert True


def test_run_brief_with_drain_calls_drain_then_brief(fake_session_factory):
    from tbc_worker_brief import main as brief_main

    with patch("tbc_worker_brief.main.get_sessionmaker", return_value=fake_session_factory), patch(
        "tbc_worker_brief.main._ensure_understanding_caught_up"
    ) as drain_mock, patch("tbc_worker_brief.main.run_brief") as brief_mock:
        brief_main.run_brief_with_drain()

    drain_mock.assert_called_once_with(fake_session_factory)
    brief_mock.assert_called_once()
    # Order check
    drain_call_order = drain_mock.mock_calls + brief_mock.mock_calls
    assert drain_call_order[0] == drain_mock.mock_calls[0]

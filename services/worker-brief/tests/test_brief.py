"""Tests for worker-brief."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tbc_common.prompts import BRIEF_SYSTEM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_anthropic_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


def _make_session_with_empty_data() -> MagicMock:
    """Return a mock session that returns empty results for all queries."""
    session = MagicMock()
    # scalars().all() returns empty list
    session.execute.return_value.scalars.return_value.all.return_value = []
    # all() returns empty list
    session.execute.return_value.all.return_value = []
    # session.get returns None
    session.get.return_value = None
    return session


# ---------------------------------------------------------------------------
# Test: cached block contains BRIEF_SYSTEM
# ---------------------------------------------------------------------------

def test_cached_block_contains_system_prompt():
    """build_cached_context includes BRIEF_SYSTEM, call_anthropic uses it in system."""
    from tbc_worker_brief.assembler import build_cached_context

    session = _make_session_with_empty_data()
    cached_context = build_cached_context(session)

    # The cached context has chat tags; the system prompt is passed separately in sender.py
    # Verify BRIEF_SYSTEM is used by call_anthropic in system array
    with patch("tbc_worker_brief.sender.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_anthropic_response("brief text")

        with patch("tbc_worker_brief.sender.settings") as mock_settings:
            mock_settings.anthropic_api_key = MagicMock()
            mock_settings.anthropic_api_key.get_secret_value.return_value = "test-key"
            mock_settings.brief_model = "claude-sonnet-4-6"

            from tbc_worker_brief.sender import call_anthropic
            call_anthropic(cached_context, "fresh input")

        call_kwargs = mock_client.messages.create.call_args
        system_blocks = call_kwargs.kwargs["system"]
        system_text = "".join(b["text"] for b in system_blocks)
        assert BRIEF_SYSTEM in system_text


# ---------------------------------------------------------------------------
# Test: cache_control on stable block
# ---------------------------------------------------------------------------

def test_cache_control_on_stable_block():
    """First content block (cached_context) has cache_control ephemeral."""
    from tbc_worker_brief.assembler import build_cached_context

    session = _make_session_with_empty_data()
    cached_context = build_cached_context(session)

    with patch("tbc_worker_brief.sender.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_anthropic_response("brief text")

        with patch("tbc_worker_brief.sender.settings") as mock_settings:
            mock_settings.anthropic_api_key = MagicMock()
            mock_settings.anthropic_api_key.get_secret_value.return_value = "test-key"
            mock_settings.brief_model = "claude-sonnet-4-6"

            from tbc_worker_brief.sender import call_anthropic
            call_anthropic(cached_context, "fresh input")

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs["messages"]
        first_content = messages[0]["content"][0]
        assert first_content.get("cache_control") == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Test: fresh block has NO cache_control
# ---------------------------------------------------------------------------

def test_fresh_block_has_no_cache_control():
    """Second content block (fresh_input) does NOT have cache_control."""
    from tbc_worker_brief.assembler import build_cached_context

    session = _make_session_with_empty_data()
    cached_context = build_cached_context(session)

    with patch("tbc_worker_brief.sender.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_anthropic_response("brief text")

        with patch("tbc_worker_brief.sender.settings") as mock_settings:
            mock_settings.anthropic_api_key = MagicMock()
            mock_settings.anthropic_api_key.get_secret_value.return_value = "test-key"
            mock_settings.brief_model = "claude-sonnet-4-6"

            from tbc_worker_brief.sender import call_anthropic
            call_anthropic(cached_context, "fresh data today")

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs["messages"]
        second_content = messages[0]["content"][1]
        assert "cache_control" not in second_content
        assert second_content["text"] == "fresh data today"


# ---------------------------------------------------------------------------
# Test: brief written to chat_summaries
# ---------------------------------------------------------------------------

def test_brief_written_to_chat_summaries():
    """save_brief writes a row with chat_id=0, period='brief'."""
    from tbc_worker_brief.sender import save_brief

    session = MagicMock()
    session.execute.return_value = MagicMock()
    today = date(2026, 4, 22)

    save_brief(session, "Test brief", today)

    # session.execute should have been called (pg_insert statement)
    assert session.execute.called
    assert session.commit.called


# ---------------------------------------------------------------------------
# Test: radar alerts stamped after brief
# ---------------------------------------------------------------------------

def test_radar_alerts_stamped_after_brief():
    """stamp_radar_alerts executes an update and commits."""
    from tbc_worker_brief.sender import stamp_radar_alerts

    session = MagicMock()
    stamp_radar_alerts(session, [1, 2, 3])

    assert session.execute.called
    assert session.commit.called


def test_radar_alerts_stamped_noop_when_empty():
    """stamp_radar_alerts does nothing when alert_ids is empty."""
    from tbc_worker_brief.sender import stamp_radar_alerts

    session = MagicMock()
    stamp_radar_alerts(session, [])

    session.execute.assert_not_called()
    session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Test: trigger file causes immediate run
# ---------------------------------------------------------------------------

def test_trigger_file_causes_immediate_run(tmp_path, monkeypatch):
    """check_trigger_file runs brief and deletes the file when it exists."""
    trigger_path = str(tmp_path / "tbc_trigger_brief")
    # Create the trigger file
    open(trigger_path, "w").close()

    monkeypatch.setattr("tbc_worker_brief.main.TRIGGER_FILE", trigger_path)

    run_called = []

    def fake_run_brief():
        run_called.append(True)

    monkeypatch.setattr("tbc_worker_brief.main.run_brief", fake_run_brief)

    from tbc_worker_brief.main import check_trigger_file
    check_trigger_file()

    assert run_called, "run_brief should have been called"
    assert not os.path.exists(trigger_path), "trigger file should be deleted"


def test_render_commitment_includes_short_id():
    """Each commitment row must carry `(c<id>)` so the user can reference
    it later. Regression catches accidental drop of the inline tag."""
    from tbc_worker_brief.assembler import render_commitment

    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    c = SimpleNamespace(
        id=42,
        source_sent_at=datetime(2026, 4, 25, 10, 0, tzinfo=UTC),
        created_at=datetime(2026, 4, 25, 10, 5, tzinfo=UTC),
        due_at=None,
        description="Send the report to Bob",
    )
    line = render_commitment(c, now=now)
    assert "(c42)" in line
    assert "Send the report to Bob" in line
    assert "age=5d" in line


def test_render_commitment_short_id_with_due_date():
    from tbc_worker_brief.assembler import render_commitment

    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    c = SimpleNamespace(
        id=7,
        source_sent_at=None,
        created_at=datetime(2026, 4, 28, 10, 0, tzinfo=UTC),
        due_at=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
        description="contract review with Acme",
    )
    line = render_commitment(c, now=now)
    assert "(c7)" in line
    assert "due: 2026-05-05" in line
    # source_sent_at unset → falls back to extracted-from kind
    assert "extracted" in line


def test_brief_format_spec_preserves_short_id_instruction():
    """The format spec passed to the LLM must explicitly tell it to keep
    the (c<id>) tag inline in ON YOUR PLATE / WAITING ON OTHERS."""
    from tbc_worker_brief.assembler import BRIEF_FORMAT_SPEC

    assert "(c<id>)" in BRIEF_FORMAT_SPEC


def test_trigger_file_no_run_when_absent(tmp_path, monkeypatch):
    """check_trigger_file does nothing when trigger file is absent."""
    trigger_path = str(tmp_path / "tbc_trigger_brief")
    monkeypatch.setattr("tbc_worker_brief.main.TRIGGER_FILE", trigger_path)

    run_called = []

    def fake_run_brief():
        run_called.append(True)

    monkeypatch.setattr("tbc_worker_brief.main.run_brief", fake_run_brief)

    from tbc_worker_brief.main import check_trigger_file
    check_trigger_file()

    assert not run_called

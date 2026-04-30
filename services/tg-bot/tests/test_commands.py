"""Unit tests for tbc-bot command handlers.

All tests mock the DB session and Telegram bot — no real API calls.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to build fake aiogram Message objects
# ---------------------------------------------------------------------------


def _make_message(
    text: str,
    user_id: int = 42,
    is_owner: bool = True,
    message_id: int = 1,
) -> AsyncMock:
    """Build a minimal mock Message with an async answer() method."""
    msg = AsyncMock()
    msg.text = text
    msg.message_id = message_id
    msg.from_user = SimpleNamespace(id=user_id)
    msg.answer = AsyncMock()
    return msg


OWNER_ID = 42


# ---------------------------------------------------------------------------
# Patch settings so tests don't need real env vars
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_settings():
    with patch("tbc_common.config.settings") as mock_settings:
        mock_settings.tg_owner_user_id = OWNER_ID
        mock_settings.tg_bot_token = None
        mock_settings.database_url = "postgresql+psycopg://fake/fake"
        yield mock_settings


# ---------------------------------------------------------------------------
# Guard: non-owner messages are silently ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_owner_ignored():
    """Messages from non-owners must produce no reply."""
    from tbc_bot.handlers.commands import cmd_pause

    # Non-owner message (different user_id)
    msg = _make_message("/pause", user_id=999, is_owner=False)

    with patch("tbc_bot.handlers.commands.is_owner", return_value=False):
        await cmd_pause(msg)

    msg.answer.assert_not_called()


# ---------------------------------------------------------------------------
# /feedback: not_useful with item ref
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_not_useful_parsed():
    """/feedback #a7f2 not_useful ... inserts a BriefFeedback row with correct fields."""
    from tbc_bot.handlers.feedback import cmd_feedback

    msg = _make_message('/feedback #a7f2 not_useful "just smalltalk"')

    captured_rows: list = []

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    def fake_add(row):
        captured_rows.append(row)

    mock_session.add = fake_add
    mock_session.commit = MagicMock()
    mock_session.refresh = MagicMock()

    mock_sm = MagicMock(return_value=mock_session)

    with (
        patch("tbc_bot.handlers.feedback.is_owner", return_value=True),
        patch("tbc_bot.handlers.feedback.get_sessionmaker", return_value=mock_sm),
    ):
        await cmd_feedback(msg)

    assert len(captured_rows) == 1
    row = captured_rows[0]
    assert row.item_ref == "a7f2"
    assert row.feedback == "not_useful"
    assert row.note == "just smalltalk"
    msg.answer.assert_called_once()


# ---------------------------------------------------------------------------
# /feedback: missed (no item ref → missed_important)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_missed_parsed():
    """/feedback missed ... inserts a row with feedback='missed_important' and no item_ref."""
    from tbc_bot.handlers.feedback import cmd_feedback

    msg = _make_message('/feedback missed "acme mentioned budget twice"')

    captured_rows: list = []

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.add = lambda row: captured_rows.append(row)
    mock_session.commit = MagicMock()
    mock_session.refresh = MagicMock()

    mock_sm = MagicMock(return_value=mock_session)

    with (
        patch("tbc_bot.handlers.feedback.is_owner", return_value=True),
        patch("tbc_bot.handlers.feedback.get_sessionmaker", return_value=mock_sm),
    ):
        await cmd_feedback(msg)

    assert len(captured_rows) == 1
    row = captured_rows[0]
    assert row.feedback == "missed_important"
    assert row.item_ref is None
    assert "acme" in (row.note or "").lower()


# ---------------------------------------------------------------------------
# /feedback: unknown feedback_type rejected without DB write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_unknown_type_rejected():
    """/feedback #abcd <bogus> ... must reply with usage help and NOT insert.

    Regression: a real-world DM `/feedback #d3b9 that is irrelevant ...`
    used to silently land a row with feedback='that' because the parser's
    else-branch accepted any token. Brief calibration query expects one
    of the canonical values."""
    from tbc_bot.handlers.feedback import cmd_feedback

    msg = _make_message('/feedback #abcd that is irrelevant. there is no payment issue')

    captured_rows: list = []
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.add = lambda row: captured_rows.append(row)
    mock_session.commit = MagicMock()
    mock_session.refresh = MagicMock()
    mock_sm = MagicMock(return_value=mock_session)

    with (
        patch("tbc_bot.handlers.feedback.is_owner", return_value=True),
        patch("tbc_bot.handlers.feedback.get_sessionmaker", return_value=mock_sm),
    ):
        await cmd_feedback(msg)

    # No row written
    assert captured_rows == []
    # User got a usage message naming the allowed values
    msg.answer.assert_called_once()
    reply = msg.answer.call_args[0][0]
    assert "useful" in reply
    assert "missed_important" in reply
    assert "that" in reply  # quotes the bad token back at the user


@pytest.mark.asyncio
async def test_feedback_useful_alias_resolves():
    """`/feedback #abcd yes` should still work — `yes` is a useful alias."""
    from tbc_bot.handlers.feedback import cmd_feedback

    msg = _make_message("/feedback #abcd yes")

    captured_rows: list = []
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.add = lambda row: captured_rows.append(row)
    mock_session.commit = MagicMock()
    mock_session.refresh = MagicMock()
    mock_sm = MagicMock(return_value=mock_session)

    with (
        patch("tbc_bot.handlers.feedback.is_owner", return_value=True),
        patch("tbc_bot.handlers.feedback.get_sessionmaker", return_value=mock_sm),
    ):
        await cmd_feedback(msg)

    assert len(captured_rows) == 1
    assert captured_rows[0].feedback == "useful"


@pytest.mark.asyncio
async def test_feedback_canonical_types_all_valid():
    """Each canonical value passes through unchanged (no alias mapping)."""
    from tbc_bot.handlers.feedback import cmd_feedback
    from tbc_common.db.models import ALLOWED_FEEDBACK_TYPES

    for ft in ALLOWED_FEEDBACK_TYPES:
        msg = _make_message(f"/feedback #abcd {ft}")
        captured: list = []
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.add = captured.append  # bind once per loop iter, no closure trap
        mock_session.commit = MagicMock()
        mock_session.refresh = MagicMock()
        mock_sm = MagicMock(return_value=mock_session)

        with (
            patch("tbc_bot.handlers.feedback.is_owner", return_value=True),
            patch("tbc_bot.handlers.feedback.get_sessionmaker", return_value=mock_sm),
        ):
            await cmd_feedback(msg)

        assert len(captured) == 1, f"{ft} should have inserted a row"
        assert captured[0].feedback == ft


# ---------------------------------------------------------------------------
# /done c<id> — mark commitment resolved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_resolves_commitment_by_id():
    from tbc_bot.handlers.commitments import cmd_done

    msg = _make_message("/done c42 sent today", message_id=999)
    fake_row = MagicMock()
    fake_row.id = 42
    fake_row.description = "send the report\n[resolved 2026-04-30: sent today]"

    with (
        patch("tbc_bot.handlers.commitments.is_owner", return_value=True),
        patch(
            "tbc_bot.handlers.commitments.resolve_commitment", return_value=fake_row
        ) as mock_resolve,
    ):
        await cmd_done(msg)

    mock_resolve.assert_called_once()
    assert mock_resolve.call_args.kwargs["commitment_id"] == 42
    assert mock_resolve.call_args.kwargs["note"] == "sent today"
    assert mock_resolve.call_args.kwargs["resolved_by_message_id"] == 999
    msg.answer.assert_called_once()
    reply = msg.answer.call_args[0][0]
    assert "Marked done: c42" in reply


@pytest.mark.asyncio
async def test_done_accepts_bare_digit_form():
    """/done 42 (no `c`) also works — the prefix is optional in the slash form."""
    from tbc_bot.handlers.commitments import cmd_done

    msg = _make_message("/done 42")
    fake_row = MagicMock()
    fake_row.id = 42
    fake_row.description = "send the report"

    with (
        patch("tbc_bot.handlers.commitments.is_owner", return_value=True),
        patch(
            "tbc_bot.handlers.commitments.resolve_commitment", return_value=fake_row
        ) as mock_resolve,
    ):
        await cmd_done(msg)

    assert mock_resolve.call_args.kwargs["commitment_id"] == 42


@pytest.mark.asyncio
async def test_done_unknown_id_replies_no_commitment_no_call():
    from tbc_bot.handlers.commitments import cmd_done
    from tbc_common.db.commitments import CommitmentNotFound

    msg = _make_message("/done c99999")

    with (
        patch("tbc_bot.handlers.commitments.is_owner", return_value=True),
        patch(
            "tbc_bot.handlers.commitments.resolve_commitment",
            side_effect=CommitmentNotFound("commitment 99999 not found"),
        ),
    ):
        await cmd_done(msg)

    msg.answer.assert_called_once()
    reply = msg.answer.call_args[0][0]
    assert "c99999" in reply
    assert "No commitment" in reply or "not found" in reply.lower()


@pytest.mark.asyncio
async def test_done_not_found_emits_log():
    from tbc_bot.handlers.commitments import cmd_done
    from tbc_common.db.commitments import CommitmentNotFound

    msg = _make_message("/done c99999")

    with (
        patch("tbc_bot.handlers.commitments.is_owner", return_value=True),
        patch(
            "tbc_bot.handlers.commitments.resolve_commitment",
            side_effect=CommitmentNotFound("commitment 99999 not found"),
        ),
        patch("tbc_bot.handlers.commitments.log") as mock_log,
    ):
        await cmd_done(msg)

    mock_log.info.assert_called_once_with(
        "slash_done_not_found", commitment_id=99999
    )


@pytest.mark.asyncio
async def test_cancel_not_found_emits_log():
    from tbc_bot.handlers.commitments import cmd_cancel
    from tbc_common.db.commitments import CommitmentNotFound

    msg = _make_message("/cancel c99999")

    with (
        patch("tbc_bot.handlers.commitments.is_owner", return_value=True),
        patch(
            "tbc_bot.handlers.commitments.cancel_commitment",
            side_effect=CommitmentNotFound("commitment 99999 not found"),
        ),
        patch("tbc_bot.handlers.commitments.log") as mock_log,
    ):
        await cmd_cancel(msg)

    mock_log.info.assert_called_once_with(
        "slash_cancel_not_found", commitment_id=99999
    )


@pytest.mark.asyncio
async def test_done_no_args_shows_usage():
    from tbc_bot.handlers.commitments import cmd_done

    msg = _make_message("/done")

    with patch("tbc_bot.handlers.commitments.is_owner", return_value=True):
        await cmd_done(msg)

    msg.answer.assert_called_once()
    assert "Usage" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cancel_marks_commitment_cancelled():
    from tbc_bot.handlers.commitments import cmd_cancel

    msg = _make_message("/cancel c7 no longer needed")
    fake_row = MagicMock()
    fake_row.id = 7
    fake_row.description = "follow up with vendor"

    with (
        patch("tbc_bot.handlers.commitments.is_owner", return_value=True),
        patch(
            "tbc_bot.handlers.commitments.cancel_commitment", return_value=fake_row
        ) as mock_cancel,
    ):
        await cmd_cancel(msg)

    mock_cancel.assert_called_once()
    assert mock_cancel.call_args.kwargs["commitment_id"] == 7
    assert mock_cancel.call_args.kwargs["reason"] == "no longer needed"
    msg.answer.assert_called_once()
    assert "Cancelled: c7" in msg.answer.call_args[0][0]


# ---------------------------------------------------------------------------
# /pause — creates /tmp/tbc_pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_creates_file(tmp_path: Path):
    """Calling /pause creates the pause sentinel file."""
    from tbc_bot.handlers import commands as cmd_module

    pause_file = tmp_path / "tbc_pause"

    msg = _make_message("/pause")

    with (
        patch("tbc_bot.handlers.commands.is_owner", return_value=True),
        patch.object(cmd_module, "PAUSE_FILE", pause_file),
    ):
        await cmd_module.cmd_pause(msg)

    assert pause_file.exists()
    msg.answer.assert_called_once()


# ---------------------------------------------------------------------------
# /resume — deletes /tmp/tbc_pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_deletes_file(tmp_path: Path):
    """Calling /resume removes the pause sentinel file."""
    from tbc_bot.handlers import commands as cmd_module

    pause_file = tmp_path / "tbc_pause"
    pause_file.touch()
    assert pause_file.exists()

    msg = _make_message("/resume")

    with (
        patch("tbc_bot.handlers.commands.is_owner", return_value=True),
        patch.object(cmd_module, "PAUSE_FILE", pause_file),
    ):
        await cmd_module.cmd_resume(msg)

    assert not pause_file.exists()
    msg.answer.assert_called_once()


# ---------------------------------------------------------------------------
# /status — returns counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_counts(tmp_path: Path):
    """/status queries DB and returns formatted counts."""
    from tbc_bot.handlers import commands as cmd_module

    pause_file = tmp_path / "tbc_pause"  # does not exist → not paused

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    # scalar() calls return: total_messages, total_understood, last_msg_ts, last_understanding_ts
    mock_session.scalar = MagicMock(side_effect=[100, 80, None, None])
    mock_sm = MagicMock(return_value=mock_session)

    msg = _make_message("/status")

    with (
        patch("tbc_bot.handlers.commands.is_owner", return_value=True),
        patch("tbc_bot.handlers.commands.get_sessionmaker", return_value=mock_sm),
        patch.object(cmd_module, "PAUSE_FILE", pause_file),
    ):
        await cmd_module.cmd_status(msg)

    msg.answer.assert_called_once()
    reply_text: str = msg.answer.call_args[0][0]
    assert "100" in reply_text
    assert "80" in reply_text
    assert "20" in reply_text  # unprocessed = 100 - 80

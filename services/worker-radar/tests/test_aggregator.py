"""Tests for worker-radar aggregator.

Uses SQLite in-memory (via SQLAlchemy) so no Postgres needed.
The `session` fixture is provided by ../conftest.py.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session


def _mu(
    session: Session,
    chat_id: int = 1,
    message_id: int = 1,
    is_signal: bool = True,
    signal_type: str = "buying",
    signal_strength: int = 3,
    is_commitment: bool = False,
    commitment=None,
    processed_at: datetime | None = None,
    summary_en: str = "Test signal summary",
    is_directed_at_user: bool = False,
    sentiment_delta: int = 0,
):
    """Helper to insert a MessageUnderstanding row."""
    from tbc_common.db.models import MessageUnderstanding

    now = processed_at or datetime.now(timezone.utc)
    mu = MessageUnderstanding(
        chat_id=chat_id,
        message_id=message_id,
        model_version="test-v1",
        is_signal=is_signal,
        signal_type=signal_type if is_signal else None,
        signal_strength=signal_strength if is_signal else None,
        is_commitment=is_commitment,
        commitment=commitment,
        processed_at=now,
        summary_en=summary_en,
        is_directed_at_user=is_directed_at_user,
        sentiment_delta=sentiment_delta,
    )
    session.add(mu)
    session.commit()
    return mu


def test_new_signal_creates_alert(session: Session):
    """A new is_signal=True row should produce a radar_alert."""
    from tbc_common.db.models import RadarAlert
    from tbc_worker_radar.aggregator import run_aggregation

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    _mu(session, chat_id=10, message_id=100, is_signal=True, signal_type="buying", signal_strength=4)

    run_aggregation(session, epoch)

    alerts = session.query(RadarAlert).all()
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.alert_type == "buying"
    assert alert.severity == 4
    assert alert.chat_id == 10


def test_second_signal_updates_existing_alert(session: Session):
    """Two signals for same chat+type within 24h → one alert with both message refs."""
    from tbc_common.db.models import RadarAlert
    from tbc_worker_radar.aggregator import run_aggregation

    now = datetime.now(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    _mu(session, chat_id=10, message_id=101, is_signal=True, signal_type="buying",
        signal_strength=2, processed_at=now - timedelta(hours=1))

    # First run creates the alert
    run_aggregation(session, epoch)

    alerts = session.query(RadarAlert).all()
    assert len(alerts) == 1
    alert_id = alerts[0].id

    # Second signal arrives after first run
    checkpoint = now - timedelta(minutes=30)
    _mu(session, chat_id=10, message_id=102, is_signal=True, signal_type="buying",
        signal_strength=3, processed_at=now)

    run_aggregation(session, checkpoint)

    alerts = session.query(RadarAlert).all()
    assert len(alerts) == 1, "Should still be only one alert"
    alert = alerts[0]
    assert alert.id == alert_id
    msg_ids = [ref["message_id"] for ref in (alert.supporting_message_ids or [])]
    assert 101 in msg_ids
    assert 102 in msg_ids


def test_old_alert_creates_new(session: Session):
    """Alert created >24h ago + new signal now → a second alert is created.

    We explicitly set created_at on the first alert to simulate it being old.
    """
    from tbc_common.db.models import RadarAlert
    from tbc_worker_radar.aggregator import run_aggregation

    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=26)

    # Create an old alert directly (backdated created_at)
    old_alert = RadarAlert(
        chat_id=10,
        alert_type="risk",
        severity=2,
        title="Risk signal in chat 10",
        supporting_message_ids=[{"chat_id": 10, "message_id": 200}],
        reasoning="#abcd — old signal",
        created_at=old_time,
    )
    session.add(old_alert)
    session.commit()
    assert session.query(RadarAlert).count() == 1

    # New signal arriving now — its aggregation should see the old alert as >24h
    _mu(session, chat_id=10, message_id=201, is_signal=True, signal_type="risk",
        signal_strength=3, processed_at=now)

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    run_aggregation(session, epoch)
    assert session.query(RadarAlert).count() == 2


def test_alert_tag_format(session: Session):
    """reasoning field must start with #<4 hex chars> followed by ' — '."""
    from tbc_common.db.models import RadarAlert
    from tbc_worker_radar.aggregator import run_aggregation

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    _mu(session, chat_id=20, message_id=300, is_signal=True, signal_type="expansion", signal_strength=3)

    run_aggregation(session, epoch)

    alert = session.query(RadarAlert).one()
    assert alert.reasoning is not None
    assert re.match(r"^#[0-9a-f]{4} —", alert.reasoning), (
        f"reasoning does not start with tag: {alert.reasoning!r}"
    )


def test_non_signal_not_processed(session: Session):
    """is_signal=False rows must not create any alert."""
    from tbc_common.db.models import RadarAlert
    from tbc_worker_radar.aggregator import run_aggregation

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    _mu(session, chat_id=30, message_id=400, is_signal=False, signal_type=None)

    run_aggregation(session, epoch)

    assert session.query(RadarAlert).count() == 0


def test_old_alert_creates_new_v2(session: Session):
    """Alert created 26h ago + new signal now → second alert created."""
    from tbc_common.db.models import RadarAlert
    from tbc_worker_radar.aggregator import run_aggregation

    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=26)

    # Insert old signal and create its alert directly (backdated)
    _mu(session, chat_id=11, message_id=200, is_signal=True, signal_type="risk",
        signal_strength=2, processed_at=old_time)

    # Directly create the old alert backdated
    old_alert = RadarAlert(
        chat_id=11,
        alert_type="risk",
        severity=2,
        title="Risk signal in chat 11",
        supporting_message_ids=[{"chat_id": 11, "message_id": 200}],
        reasoning="#abcd — old signal",
        created_at=old_time,
    )
    session.add(old_alert)
    session.commit()

    assert session.query(RadarAlert).count() == 1

    # Now insert a new signal and run aggregation from after the old signal
    _mu(session, chat_id=11, message_id=201, is_signal=True, signal_type="risk",
        signal_strength=3, processed_at=now)

    checkpoint = old_time + timedelta(seconds=1)
    run_aggregation(session, checkpoint)
    assert session.query(RadarAlert).count() == 2

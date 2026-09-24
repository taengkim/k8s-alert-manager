"""Tests for Phase 20's `NotificationChannel.send_message()` -- the default
`send()`-adapter implementation (used by any channel, built-in or
third-party plugin, that hasn't overridden it) and email.py's override.

See app/channels/base.py's module docstring and app/worker/outbox.py's
deliver() (trigger == 'report' branch) for how this is actually invoked in
delivery.
"""

from unittest.mock import AsyncMock, patch

from pydantic import BaseModel

from app.channels.base import (
    AlertNotification,
    NotificationChannel,
    RenderedMessage,
)
from app.channels.email import EmailChannel, EmailConfig


class _DummyConfig(BaseModel):
    pass


class _DummyChannel(NotificationChannel):
    """A minimal channel that only implements send() -- same shape as a
    third-party plugin written before Phase 20 existed."""

    type_name = "dummy"
    display_name = "Dummy"
    config_schema = _DummyConfig

    def __init__(self, config: _DummyConfig) -> None:
        super().__init__(config)
        self.sent: list[tuple[AlertNotification, RenderedMessage]] = []

    async def send(self, notification: AlertNotification, msg: RenderedMessage) -> None:
        self.sent.append((notification, msg))


async def test_default_send_message_adapts_to_send_with_example_info() -> None:
    channel = _DummyChannel(_DummyConfig())
    msg = RenderedMessage(title="Weekly Report", body="events: 5", body_html=None)

    await channel.send_message(msg)

    assert len(channel.sent) == 1
    notification, sent_msg = channel.sent[0]
    assert sent_msg is msg
    assert notification.trigger == "info"
    assert notification.event_id is None


def test_example_info_is_a_valid_alert_notification() -> None:
    notification = AlertNotification.example_info()
    assert notification.trigger == "info"
    assert notification.event_id is None
    assert notification.alertname
    assert notification.cluster
    assert notification.team_slug


async def test_email_channel_overrides_send_message_and_skips_placeholder_notification() -> None:
    """email.py's own send_message() must build the MIME message directly
    from `msg` -- it must not go through the default adapter's
    AlertNotification.example_info() placeholder at all."""
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)
    msg = RenderedMessage(title="Weekly Report", body="events: 5", body_html="<p>5</p>")

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send_message(msg)

    assert mock_send.await_count == 1
    message = mock_send.await_args.args[0]
    assert message["Subject"] == "[KAM] Weekly Report"

    parts = {
        part.get_content_type(): part.get_payload(decode=True).decode("utf-8")
        for part in message.walk()
        if not part.is_multipart()
    }
    assert parts["text/plain"] == "events: 5"
    assert parts["text/html"] == "<p>5</p>"


async def test_email_channel_send_message_applies_subject_prefix() -> None:
    config = EmailConfig(recipients=["ops@example.org"], subject_prefix="[ACME]")
    channel = EmailChannel(config)
    msg = RenderedMessage(title="Report", body="b", body_html=None)

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send_message(msg)

    message = mock_send.await_args.args[0]
    assert message["Subject"] == "[ACME] Report"

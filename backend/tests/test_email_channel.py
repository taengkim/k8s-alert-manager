"""Unit tests for the built-in email channel (app/channels/email.py):
subject/body formatting and SMTP failure -> ChannelDeliveryError mapping.
`aiosmtplib.send` is monkeypatched throughout -- no real network/SMTP.
"""

from unittest.mock import AsyncMock, patch

import aiosmtplib
import pytest

from app.channels.base import AlertNotification, ChannelDeliveryError
from app.channels.email import EmailChannel, EmailConfig


def _notification(**overrides) -> AlertNotification:
    base = AlertNotification.example().model_dump()
    base.update(overrides)
    return AlertNotification(**base)


def _parts(message) -> dict[str, str]:
    return {
        part.get_content_type(): part.get_payload(decode=True).decode("utf-8")
        for part in message.walk()
        if not part.is_multipart()
    }


async def test_send_formats_subject_recipients_and_body() -> None:
    config = EmailConfig(recipients=["ops@example.org", "oncall@example.org"])
    channel = EmailChannel(config)
    notification = _notification(
        trigger="firing",
        alertname="HighCPU",
        severity="critical",
        labels={"alertname": "HighCPU", "pod": "api-7d9f"},
    )

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send(notification)

    assert mock_send.await_count == 1
    message = mock_send.await_args.args[0]
    assert message["Subject"] == "[KAM] [FIRING] HighCPU (critical)"
    assert message["To"] == "ops@example.org, oncall@example.org"

    parts = _parts(message)
    for body in (parts["text/plain"], parts["text/html"]):
        assert "HighCPU" in body
        assert "critical" in body  # via the subject line embedded at the top
        assert "api-7d9f" in body  # from labels


async def test_send_test_uses_example_notification() -> None:
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send_test()

    message = mock_send.await_args.args[0]
    assert "[TEST]" in message["Subject"]
    assert "KamTestAlert" in message["Subject"]


async def test_smtp_exception_is_wrapped_as_delivery_error() -> None:
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)

    mock_send = AsyncMock(side_effect=aiosmtplib.SMTPException("mailbox full"))
    with (
        patch("app.channels.email.aiosmtplib.send", new=mock_send),
        pytest.raises(ChannelDeliveryError, match="mailbox full"),
    ):
        await channel.send(AlertNotification.example())


async def test_connection_error_is_wrapped_as_delivery_error() -> None:
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)

    mock_send = AsyncMock(side_effect=OSError("connection refused"))
    with (
        patch("app.channels.email.aiosmtplib.send", new=mock_send),
        pytest.raises(ChannelDeliveryError, match="connection refused"),
    ):
        await channel.send(AlertNotification.example())

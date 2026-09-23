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


async def test_header_injection_attempt_in_alertname_is_sanitized() -> None:
    """Regression: alert-derived fields (alertname here) flow unsanitized
    into the Subject header before this fix. A raw CRLF there either raises
    email.errors.HeaderParseError (an uncaught 500, since message-building
    used to sit outside send()'s try/except) or, if it slipped through,
    could inject an extra header. Phase 9 routes real Alertmanager label
    data through this path, so this must be handled, not just theoretical.
    """
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)
    notification = _notification(alertname="Evil\r\nX-Evil: 1", severity="critical")

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send(notification)  # must not raise

    message = mock_send.await_args.args[0]
    subject = message["Subject"]
    assert "\r" not in subject
    assert "\n" not in subject
    assert "Evil X-Evil: 1" in subject
    assert "X-Evil" not in message  # no header actually got injected


async def test_non_smtp_error_during_message_build_is_wrapped_as_delivery_error() -> None:
    """Regression: send() used to only wrap aiosmtplib.send()'s own
    exceptions -- anything raised while building the message (template
    rendering, a bad header value the sanitizer doesn't catch, ...) escaped
    as an unhandled exception instead of ChannelDeliveryError.
    """
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)

    with (
        patch("app.channels.email._env.get_template", side_effect=RuntimeError("template exploded")),
        pytest.raises(ChannelDeliveryError, match="template exploded"),
    ):
        await channel.send(AlertNotification.example())

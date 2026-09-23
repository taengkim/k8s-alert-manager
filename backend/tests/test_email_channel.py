"""Unit tests for the built-in email channel (app/channels/email.py).

As of Phase 13, `send()` receives an already-rendered `RenderedMessage`
(title/body/body_html) rather than rendering its own subject/body from the
raw `AlertNotification` -- that rendering now happens once, centrally, in
`app/services/templating.py` (see tests/test_templating.py for those
tests). This file only covers what email.py itself still owns: wiring
`msg` into a MIME message, applying `subject_prefix`, and mapping SMTP/
build failures to `ChannelDeliveryError`. `aiosmtplib.send` is monkeypatched
throughout -- no real network/SMTP.
"""

from unittest.mock import AsyncMock, patch

import aiosmtplib
import pytest

from app.channels.base import AlertNotification, ChannelDeliveryError, RenderedMessage
from app.channels.email import EmailChannel, EmailConfig


def _msg(**overrides) -> RenderedMessage:
    base = {"title": "HighCPU firing", "body": "cluster: prod", "body_html": "<p>prod</p>"}
    base.update(overrides)
    return RenderedMessage(**base)


def _parts(message) -> dict[str, str]:
    return {
        part.get_content_type(): part.get_payload(decode=True).decode("utf-8")
        for part in message.walk()
        if not part.is_multipart()
    }


async def test_send_maps_msg_into_subject_recipients_and_body() -> None:
    config = EmailConfig(recipients=["ops@example.org", "oncall@example.org"])
    channel = EmailChannel(config)
    msg = _msg(title="[FIRING] HighCPU (critical)", body="text body", body_html="<b>html body</b>")

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send(AlertNotification.example(), msg)

    assert mock_send.await_count == 1
    message = mock_send.await_args.args[0]
    assert message["Subject"] == "[KAM] [FIRING] HighCPU (critical)"
    assert message["To"] == "ops@example.org, oncall@example.org"

    parts = _parts(message)
    assert parts["text/plain"] == "text body"
    assert parts["text/html"] == "<b>html body</b>"


async def test_send_with_no_body_html_omits_html_part() -> None:
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)
    msg = _msg(body_html=None)

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send(AlertNotification.example(), msg)

    message = mock_send.await_args.args[0]
    assert "text/html" not in _parts(message)


async def test_send_test_uses_example_notification_and_default_template() -> None:
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send_test()

    message = mock_send.await_args.args[0]
    assert "[TEST]" in message["Subject"]
    assert "KamTestAlert" in message["Subject"]

    parts = _parts(message)
    assert "kam-demo" in parts["text/plain"]  # namespace, from the default text template
    assert "text/html" in parts


async def test_smtp_exception_is_wrapped_as_delivery_error() -> None:
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)

    mock_send = AsyncMock(side_effect=aiosmtplib.SMTPException("mailbox full"))
    with (
        patch("app.channels.email.aiosmtplib.send", new=mock_send),
        pytest.raises(ChannelDeliveryError, match="mailbox full"),
    ):
        await channel.send(AlertNotification.example(), _msg())


async def test_connection_error_is_wrapped_as_delivery_error() -> None:
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)

    mock_send = AsyncMock(side_effect=OSError("connection refused"))
    with (
        patch("app.channels.email.aiosmtplib.send", new=mock_send),
        pytest.raises(ChannelDeliveryError, match="connection refused"),
    ):
        await channel.send(AlertNotification.example(), _msg())


async def test_header_injection_in_rendered_title_is_sanitized() -> None:
    """Defense in depth: `msg.title` should already have embedded newlines
    stripped by templating.py's `render()` before it ever reaches a
    channel, but email.py re-sanitizes the combined `subject_prefix + title`
    itself regardless -- a raw CRLF reaching this far (a rendering bug, a
    directly-constructed RenderedMessage bypassing render(), ...) either
    raises `email.errors.HeaderParseError` or, if it slipped through, could
    inject an extra header.
    """
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)
    msg = _msg(title="Evil\r\nX-Evil: 1")

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send(AlertNotification.example(), msg)  # must not raise

    message = mock_send.await_args.args[0]
    subject = message["Subject"]
    assert "\r" not in subject
    assert "\n" not in subject
    assert "Evil X-Evil: 1" in subject
    assert "X-Evil" not in message  # no header actually got injected


async def test_non_smtp_error_during_message_build_is_wrapped_as_delivery_error() -> None:
    """Regression: send() must wrap failures from building the MIME message
    itself, not just aiosmtplib.send()'s own exceptions.
    """
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)

    with (
        patch("app.channels.email.MIMEText", side_effect=RuntimeError("mime exploded")),
        pytest.raises(ChannelDeliveryError, match="mime exploded"),
    ):
        await channel.send(AlertNotification.example(), _msg())


def test_default_templates_expose_title_body_and_html() -> None:
    assert set(EmailChannel.default_templates) == {"title", "body", "body_html"}
    assert "{{ alertname }}" in EmailChannel.default_templates["title"]
    assert "{{ cluster }}" in EmailChannel.default_templates["body"]
    assert "{{ cluster }}" in EmailChannel.default_templates["body_html"]

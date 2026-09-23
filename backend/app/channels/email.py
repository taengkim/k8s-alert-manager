"""Built-in SMTP email channel.

SMTP connection settings (host/port/credentials/starttls) are app-wide
config (`settings.smtp_*`, env `KAM_SMTP_*`) -- there's one mail relay per
deployment, so it's not part of this channel's per-instance config. Only
`recipients` and `subject_prefix` are, since a team may want its own
recipient list and prefix.

Rendering (Jinja2, small templates) runs synchronously; only the actual SMTP
send is awaited.
"""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import aiosmtplib
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, EmailStr, Field

from app.channels.base import (
    AlertNotification,
    ChannelDeliveryError,
    NotificationChannel,
)
from app.config import get_settings

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Only the HTML template is autoescaped -- the text template is plain text,
# not markup, so escaping it would corrupt it (e.g. turning "&" into
# "&amp;").
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=lambda name: bool(name) and name.endswith("_html.j2"),
    trim_blocks=True,
    lstrip_blocks=True,
)


class EmailConfig(BaseModel):
    recipients: list[EmailStr] = Field(min_length=1)
    subject_prefix: str = "[KAM]"


class EmailChannel(NotificationChannel):
    type_name = "email"
    display_name = "Email"
    config_schema = EmailConfig

    def __init__(self, config: EmailConfig) -> None:
        super().__init__(config)
        self.config: EmailConfig = config

    async def send(self, notification: AlertNotification) -> None:
        try:
            subject = self._subject(notification)
            text_body = _env.get_template("email_text.j2").render(
                n=notification, subject=subject
            )
            html_body = _env.get_template("email_html.j2").render(
                n=notification, subject=subject
            )

            settings = get_settings()
            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = settings.smtp_username or "kam@localhost"
            message["To"] = ", ".join(self.config.recipients)
            message.attach(MIMEText(text_body, "plain", "utf-8"))
            message.attach(MIMEText(html_body, "html", "utf-8"))

            await aiosmtplib.send(
                message,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username or None,
                password=settings.smtp_password or None,
                start_tls=settings.smtp_starttls,
            )
        except aiosmtplib.SMTPException as exc:
            raise ChannelDeliveryError(f"SMTP delivery failed: {exc}") from exc
        except OSError as exc:
            # aiosmtplib surfaces connection refused/timeout as a plain
            # OSError rather than an SMTPException subclass.
            raise ChannelDeliveryError(f"SMTP connection failed: {exc}") from exc
        except Exception as exc:
            # Alert-derived data (labels/annotations, from Phase 9 onward)
            # flows into subject/body construction above -- a bad header
            # value or template error must still surface as a delivery
            # failure, not an unhandled 500. Subject values are sanitized
            # in _subject(), but this is the backstop for anything that
            # isn't (or a future rendering change that forgets to be).
            raise ChannelDeliveryError(f"failed to send email: {exc}") from exc

    def _subject(self, notification: AlertNotification) -> str:
        severity = _strip_header_newlines(notification.severity or "none")
        alertname = _strip_header_newlines(notification.alertname)
        trigger = _strip_header_newlines(notification.trigger.upper())
        prefix = _strip_header_newlines(self.config.subject_prefix)
        return f"{prefix} [{trigger}] {alertname} ({severity})"


def _strip_header_newlines(value: str) -> str:
    """Collapse embedded CR/LF to a space.

    `email.message.Message.__setitem__` rejects (raises HeaderParseError)
    any header value containing a raw newline not followed by whitespace --
    and even a "valid" one is a header-injection vector (a second `\\r\\n`
    could start a new header). Subject is built from alert-derived fields
    (alertname, severity, ...), which is attacker/operator-controlled data
    from Phase 9 onward, so every piece is sanitized before use.
    """
    return value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")

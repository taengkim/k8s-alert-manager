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
        subject = self._subject(notification)
        text_body = _env.get_template("email_text.j2").render(n=notification, subject=subject)
        html_body = _env.get_template("email_html.j2").render(n=notification, subject=subject)

        settings = get_settings()
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = settings.smtp_username or "kam@localhost"
        message["To"] = ", ".join(self.config.recipients)
        message.attach(MIMEText(text_body, "plain", "utf-8"))
        message.attach(MIMEText(html_body, "html", "utf-8"))

        try:
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

    def _subject(self, notification: AlertNotification) -> str:
        severity = notification.severity or "none"
        return (
            f"{self.config.subject_prefix} [{notification.trigger.upper()}] "
            f"{notification.alertname} ({severity})"
        )

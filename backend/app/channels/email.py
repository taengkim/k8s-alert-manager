"""Built-in SMTP email channel.

SMTP connection settings (host/port/credentials/starttls) are app-wide
config (`settings.smtp_*`, env `KAM_SMTP_*`) -- there's one mail relay per
deployment, so it's not part of this channel's per-instance config. Only
`recipients` and `subject_prefix` are, since a team may want its own
recipient list and prefix.

As of Phase 13, this channel no longer renders its own subject/body -- that
now happens once, centrally, in `app/services/templating.py` (sandboxed,
with a template a team can customize), and `send()` receives the result as
`msg: RenderedMessage`. This channel's part is just wiring `msg` into a MIME
message: `subject_prefix` (per-channel config, not a template concern) is
still applied here, prepended to `msg.title`.

`email_text.j2`/`email_html.j2` (this package's `templates/` directory) are
this channel *type*'s default template source, exposed via
`default_templates` -- used only when neither a routing rule nor a channel
instance has a custom template (see `templating.resolve_template`). They're
loaded once at import time and passed through the same sandboxed rendering
path as any team-authored template; nothing here renders them directly.
"""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import aiosmtplib
from pydantic import BaseModel, EmailStr, Field

from app.channels.base import (
    AlertNotification,
    ChannelDeliveryError,
    NotificationChannel,
    RenderedMessage,
)
from app.config import get_settings
from app.services.templating import strip_header_newlines

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load_default_templates() -> dict[str, str]:
    return {
        "title": "[{{ trigger | upper }}] {{ alertname }}"
        "{% if severity %} ({{ severity }}){% endif %}",
        "body": (TEMPLATES_DIR / "email_text.j2").read_text(encoding="utf-8"),
        "body_html": (TEMPLATES_DIR / "email_html.j2").read_text(encoding="utf-8"),
    }


class EmailConfig(BaseModel):
    recipients: list[EmailStr] = Field(min_length=1)
    subject_prefix: str = "[KAM]"


class EmailChannel(NotificationChannel):
    type_name = "email"
    display_name = "Email"
    config_schema = EmailConfig
    default_templates = _load_default_templates()

    def __init__(self, config: EmailConfig) -> None:
        super().__init__(config)
        self.config: EmailConfig = config

    async def send(self, notification: AlertNotification, msg: RenderedMessage) -> None:
        try:
            # subject_prefix is per-channel config, not a template concern --
            # applied here, on top of the already-rendered (and already
            # newline-stripped) title. Stripped again regardless: a prefix
            # itself is free-form config text, so it gets the same
            # header-injection protection as the title it's joined with.
            subject = strip_header_newlines(f"{self.config.subject_prefix} {msg.title}")

            settings = get_settings()
            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = settings.smtp_username or "kam@localhost"
            message["To"] = ", ".join(self.config.recipients)
            message.attach(MIMEText(msg.body, "plain", "utf-8"))
            if msg.body_html:
                message.attach(MIMEText(msg.body_html, "html", "utf-8"))

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
            # Backstop for anything else building/sending the MIME message
            # could raise -- must still surface as a delivery failure, not
            # an unhandled 500.
            raise ChannelDeliveryError(f"failed to send email: {exc}") from exc

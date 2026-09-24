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
from jinja2 import Environment
from pydantic import BaseModel, EmailStr, Field

from app.channels.base import (
    AlertNotification,
    ChannelDeliveryError,
    NotificationChannel,
    RenderedMessage,
)
from app.config import get_settings
from app.services.templating import datetime_format, strip_header_newlines

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load_default_templates() -> dict[str, str]:
    return {
        "title": "[{{ trigger | upper }}] {{ alertname }}"
        "{% if severity %} ({{ severity }}){% endif %}",
        "body": (TEMPLATES_DIR / "email_text.j2").read_text(encoding="utf-8"),
        "body_html": (TEMPLATES_DIR / "email_html.j2").read_text(encoding="utf-8"),
    }


# Phase 16 digest templates -- deliberately NOT run through
# app.services.templating's SandboxedEnvironment: that sandboxing exists to
# contain a TEAM-AUTHORED template's source (resolve_template/render), which
# these aren't -- there's no per-team custom digest template in this phase
# (see this phase's brief), so the source here is fixed, built-in Python
# code, and only the alert VALUES substituted into it come from
# (Alertmanager-controlled) notification data. autoescape on the HTML
# environment is what actually matters for that data, same as any other
# Jinja2 template rendering untrusted values.
_DIGEST_TEXT_ENV = Environment(autoescape=False)
_DIGEST_TEXT_ENV.filters["datetime_format"] = datetime_format
_DIGEST_HTML_ENV = Environment(autoescape=True)
_DIGEST_HTML_ENV.filters["datetime_format"] = datetime_format


def _load_default_digest_templates() -> dict[str, str]:
    return {
        "text": (TEMPLATES_DIR / "email_digest_text.j2").read_text(encoding="utf-8"),
        "html": (TEMPLATES_DIR / "email_digest_html.j2").read_text(encoding="utf-8"),
    }


def _digest_items(notifications: list[AlertNotification]) -> list[dict]:
    return [
        {
            "alertname": n.alertname,
            "severity": n.severity,
            "namespace": n.namespace,
            "cluster": n.cluster,
            "starts_at": n.starts_at,
            "app_url": n.app_url,
        }
        for n in notifications
    ]


class EmailConfig(BaseModel):
    recipients: list[EmailStr] = Field(min_length=1)
    subject_prefix: str = "[KAM]"


class EmailChannel(NotificationChannel):
    type_name = "email"
    display_name = "Email"
    config_schema = EmailConfig
    default_templates = _load_default_templates()
    # Phase 16: this channel type's own digest templates, loaded once at
    # import time same as default_templates above -- see send_batch().
    default_digest_templates = _load_default_digest_templates()

    def __init__(self, config: EmailConfig) -> None:
        super().__init__(config)
        self.config: EmailConfig = config

    async def _send_mime(self, subject: str, body: str, body_html: str | None) -> None:
        """Build one MIME message and hand it to `aiosmtplib.send`, mapping
        every failure mode to `ChannelDeliveryError` -- the shared plumbing
        behind both `send()` (one alert) and `send_batch()` (Phase 16's
        digest: one summary email standing in for many alerts). `subject`
        is expected to already be fully composed (prefix applied, newlines
        stripped) by the caller.
        """
        try:
            settings = get_settings()
            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = settings.smtp_username or "kam@localhost"
            message["To"] = ", ".join(self.config.recipients)
            message.attach(MIMEText(body, "plain", "utf-8"))
            if body_html:
                message.attach(MIMEText(body_html, "html", "utf-8"))

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

    async def send(self, notification: AlertNotification, msg: RenderedMessage) -> None:
        # subject_prefix is per-channel config, not a template concern --
        # applied here, on top of the already-rendered (and already
        # newline-stripped) title. Stripped again regardless: a prefix
        # itself is free-form config text, so it gets the same
        # header-injection protection as the title it's joined with.
        subject = strip_header_newlines(f"{self.config.subject_prefix} {msg.title}")
        await self._send_mime(subject, msg.body, msg.body_html)

    async def send_message(self, msg: RenderedMessage) -> None:
        """Phase 20 scheduled report delivery: an ordinary email, with no
        alert-styled header/layout at all -- overrides `NotificationChannel`'s
        default `send()`-adapter implementation so a report never has to flow
        through a synthetic placeholder `AlertNotification` just to reach
        this channel's own MIME-building code. `subject_prefix` still applies
        (it's per-channel config, not an alert-specific concern), same as
        `send()`; `msg.body`/`msg.body_html` are used exactly as rendered.
        """
        subject = strip_header_newlines(f"{self.config.subject_prefix} {msg.title}")
        await self._send_mime(subject, msg.body, msg.body_html)

    async def send_batch(
        self, notifications: list[AlertNotification], msgs: list[RenderedMessage]
    ) -> None:
        """Phase 16 digest send: one summary email for the whole batch,
        overriding `NotificationChannel`'s default per-item `send()` loop.

        `msgs` (each item's individually resolved/rendered single-alert
        message -- computed by `app/worker/outbox.py`'s `deliver()` so the
        default per-item loop has something to send) is deliberately unused
        here: the digest's own subject/body come from this channel type's
        dedicated digest templates (`default_digest_templates`, see module
        docstring for why they're rendered outside the sandboxed team-
        template path), not from stitching together N already-rendered
        single-alert messages.
        """
        if not notifications:
            return
        count = len(notifications)
        # The subject/body's one "team" label is the FIRST item's team_slug,
        # not necessarily every item's -- a channel parking notifications
        # from a cross-team escalation (Channel.allow_cross_team_escalation)
        # can bundle items whose own routing rule belongs to a different
        # team than the channel's owner. Deliberate simplification: this
        # phase doesn't attempt a "mixed teams" label, since the channel
        # itself always belongs to exactly one team and that's the audience
        # actually reading the digest.
        team_slug = notifications[0].team_slug
        context = {"count": count, "team": team_slug, "items": _digest_items(notifications)}

        subject = strip_header_newlines(
            f"{self.config.subject_prefix} 알럿 다이제스트: {count}건 ({team_slug})"
        )
        body = _DIGEST_TEXT_ENV.from_string(self.default_digest_templates["text"]).render(**context)
        body_html = _DIGEST_HTML_ENV.from_string(self.default_digest_templates["html"]).render(
            **context
        )
        await self._send_mime(subject, body, body_html)

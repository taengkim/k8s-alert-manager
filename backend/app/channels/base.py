"""The channel abstraction every notification channel (built-in or
third-party plugin) implements.

Message formatting is centralized as of Phase 13: `app/services/templating.py`
sandboxed-renders a team's (or a channel type's, or the app's) template into
a `RenderedMessage`, and `app/worker/outbox.py`'s `deliver()` passes that
alongside the raw `AlertNotification` into `send()`. A channel's `send()` is
free to use `msg` however suits its transport (email maps `msg.title` to the
Subject; a channel with no meaningful "subject" concept can just use
`msg.body`) -- see `app/channels/email.py` for the built-in example.

BREAKING (Phase 13): `send()`'s signature changed from `send(notification)`
to `send(notification, msg)`. Every channel -- built-in and third-party
plugin alike -- must be updated; see `plugins/example_webhook_channel/`.

Routing, the outbox queue, and retry scheduling are Phase 9 -- a channel here
only needs to know how to deliver *one* notification. A `send()` failure
raises `ChannelDeliveryError`; the outbox worker is what decides whether/when
to retry.

Phase 16 adds `send_batch()` for a channel's storm-control digest sends
(many alerts bundled into one outbox row) -- NOT breaking: it has a default
implementation (a plain per-item `send()` loop), so a third-party channel
written before this phase keeps working unchanged, just without a genuine
"one summary message" digest experience until it overrides `send_batch()`
itself.
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel


class AlertNotification(BaseModel):
    """Everything a channel needs to format and deliver one notification."""

    event_id: int | None  # None for a synthetic test send (see `example()`).
    trigger: Literal["firing", "resolved", "test"]
    alertname: str
    severity: str | None
    namespace: str | None
    cluster: str
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: datetime
    ends_at: datetime | None
    team_slug: str
    app_url: str | None
    runbook_url: str | None = None
    grafana_url: str | None = None

    @classmethod
    def example(cls) -> "AlertNotification":
        """A synthetic alert used for `POST /channels/{id}/test` -- lets a
        team verify a channel's config actually works without waiting for a
        real alert to fire.
        """
        now = datetime.now(UTC)
        return cls(
            event_id=None,
            trigger="test",
            alertname="KamTestAlert",
            severity="warning",
            namespace="kam-demo",
            cluster="local",
            labels={
                "alertname": "KamTestAlert",
                "severity": "warning",
                "namespace": "kam-demo",
            },
            annotations={
                "summary": "This is a test notification from k8s-alert-manager.",
            },
            starts_at=now - timedelta(minutes=5),
            ends_at=None,
            team_slug="platform",
            app_url="http://localhost:5173",
            runbook_url=None,
            grafana_url=None,
        )


class RenderedMessage(BaseModel):
    """The output of `app/services/templating.py`'s `render()` -- a
    channel's `send()` receives one of these alongside the raw
    `AlertNotification`, already rendered from whichever template applies
    (a routing rule's, a channel's, a channel type's default, or the app's
    fallback default -- see `templating.resolve_template`).

    `title` has already had embedded newlines stripped (protects against
    header injection for channels that map it to something like an email
    Subject); `body_html` is `None` when no HTML slot was rendered (either
    the template supplied none, or the channel type doesn't declare one).
    """

    title: str
    body: str
    body_html: str | None = None


class ChannelDeliveryError(Exception):
    """Raised by `send()`/`send_test()` when delivery fails (network error,
    transport rejection, etc). The outbox worker catches this to decide on
    retries; the test-send API maps it to a 502.
    """


class NotificationChannel(ABC):
    """Base class for every channel type -- built-in (`email.py`) and
    third-party plugins alike (see `plugins/example_webhook_channel/`).

    Subclasses declare class attributes and implement `send()`:

    - `type_name`: stable identifier stored in `channels.type` and used as
      the discovery/registry key. Must be unique across all discovered
      channels (see `app/channels/registry.py` for collision handling).
    - `display_name`: human-readable label for the UI's channel-type picker.
    - `config_schema`: a `pydantic.BaseModel` describing this channel's
      per-instance config (e.g. recipients for email). Its
      `model_json_schema()` is exposed over the API so the frontend can
      render a config form for *any* channel type -- including third-party
      plugins -- without per-type frontend code.
    - `default_templates` (optional): this channel type's own default Jinja2
      template source, keyed by slot (`title`/`body`/`body_html`) -- used
      when neither a routing rule nor a channel instance has a custom
      template assigned (see `app.services.templating.resolve_template`).
      Left empty (the default), `app.services.templating.APP_DEFAULT_TEMPLATES`
      is used instead.
    """

    type_name: ClassVar[str]
    display_name: ClassVar[str]
    config_schema: ClassVar[type[BaseModel]]
    default_templates: ClassVar[dict[str, str]] = {}

    def __init__(self, config: BaseModel) -> None:
        self.config = config

    @abstractmethod
    async def send(self, notification: AlertNotification, msg: RenderedMessage) -> None:
        """Deliver `notification` (raw alert data) through this channel,
        formatted per `msg` (this channel's resolved+rendered template
        output). Raise `ChannelDeliveryError` on failure.
        """

    async def send_batch(
        self, notifications: list[AlertNotification], msgs: list[RenderedMessage]
    ) -> None:
        """Deliver a digest batch (Phase 16): `notifications` and `msgs` are
        parallel lists of the same length, one already-resolved/rendered
        message per notification the channel's storm control bundled
        together (see `app.worker.scheduler._dispatch_digest_flush` and
        `app/worker/outbox.py`'s `deliver()`, which calls this instead of
        `send()` for an `is_digest` outbox row).

        Default implementation, kept for third-party channel backward
        compatibility (a plugin written before this phase never overrode
        this method): delivers each item individually via `send()`, in
        order, same one-at-a-time behavior as if storm control didn't
        exist. Raises (stopping partway through the batch) on the first
        `ChannelDeliveryError`, same as this method's own callers already
        expect from a single `send()` failure -- the outbox worker retries
        the whole row, not just the remaining items.

        A channel that wants a genuine digest experience -- one message
        summarizing all N alerts, rather than N separate sends -- should
        override this instead (see `app/channels/email.py`'s `EmailChannel`
        for the built-in example).
        """
        for notification, msg in zip(notifications, msgs, strict=True):
            await self.send(notification, msg)

    async def send_test(self) -> None:
        """Send a synthetic test notification, rendered from this channel
        type's own `default_templates` (or the app default, if it declares
        none). Override only if a channel needs different test-send
        behavior.

        Imports `app.services.templating` locally to avoid a module-level
        import cycle: that module itself imports `AlertNotification` and
        `RenderedMessage` from here.
        """
        from app.services.templating import APP_DEFAULT_TEMPLATES, render

        example = AlertNotification.example()
        template_strs = type(self).default_templates or APP_DEFAULT_TEMPLATES
        outcome = await render(template_strs, example)
        await self.send(example, outcome.message)

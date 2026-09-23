"""The channel abstraction every notification channel (built-in or
third-party plugin) implements.

Message formatting is each channel's own responsibility for now -- a
central, editable message-template system (with a shared `RenderedMessage`
passed into `send()`) is planned for Phase 13. Until then, `send()` receives
the raw `AlertNotification` and formats its own subject/body however suits
its transport (see `app/channels/email.py` for the built-in example).

Routing, the outbox queue, and retry scheduling are Phase 9 -- a channel here
only needs to know how to deliver *one* notification. A `send()` failure
raises `ChannelDeliveryError`; the Phase 9 outbox worker is what will decide
whether/when to retry.
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


class ChannelDeliveryError(Exception):
    """Raised by `send()`/`send_test()` when delivery fails (network error,
    transport rejection, etc). The Phase 9 outbox worker will catch this to
    decide on retries; for now (Phase 8) it's what the test-send API maps to
    a 502.
    """


class NotificationChannel(ABC):
    """Base class for every channel type -- built-in (`email.py`) and
    third-party plugins alike (see `plugins/example_webhook_channel/`).

    Subclasses declare three class attributes and implement `send()`:

    - `type_name`: stable identifier stored in `channels.type` and used as
      the discovery/registry key. Must be unique across all discovered
      channels (see `app/channels/registry.py` for collision handling).
    - `display_name`: human-readable label for the UI's channel-type picker.
    - `config_schema`: a `pydantic.BaseModel` describing this channel's
      per-instance config (e.g. recipients for email). Its
      `model_json_schema()` is exposed over the API so the frontend can
      render a config form for *any* channel type -- including third-party
      plugins -- without per-type frontend code.
    """

    type_name: ClassVar[str]
    display_name: ClassVar[str]
    config_schema: ClassVar[type[BaseModel]]

    def __init__(self, config: BaseModel) -> None:
        self.config = config

    @abstractmethod
    async def send(self, notification: AlertNotification) -> None:
        """Deliver `notification` through this channel. Raise
        `ChannelDeliveryError` on failure."""

    async def send_test(self) -> None:
        """Send a synthetic test notification. The default implementation
        just calls `send(AlertNotification.example())`; override only if a
        channel needs different behavior for test sends.
        """
        await self.send(AlertNotification.example())

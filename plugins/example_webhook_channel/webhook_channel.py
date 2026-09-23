"""Reference third-party channel plugin: posts each AlertNotification as
JSON to a configured webhook URL.

This is the "drop a file in" style of plugin -- see this directory's
README.md for how it's loaded and how the entry-point alternative works.
"""

import httpx
from pydantic import BaseModel, HttpUrl

from app.channels.base import AlertNotification, ChannelDeliveryError, NotificationChannel

TIMEOUT_SECONDS = 10.0


class WebhookConfig(BaseModel):
    url: HttpUrl
    headers: dict[str, str] = {}


class WebhookChannel(NotificationChannel):
    type_name = "webhook"
    display_name = "Webhook"
    config_schema = WebhookConfig

    def __init__(self, config: WebhookConfig) -> None:
        super().__init__(config)
        self.config: WebhookConfig = config

    async def send(self, notification: AlertNotification) -> None:
        payload = notification.model_dump(mode="json")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.post(
                    str(self.config.url), json=payload, headers=self.config.headers
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ChannelDeliveryError(f"webhook delivery failed: {exc}") from exc

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAM_")

    database_url: str = "sqlite+aiosqlite:///./kam.db"
    app_name: str = "kam"
    # Stamped into a rule export envelope's `source.app_version` -- purely
    # informational provenance for whoever later inspects/imports the file,
    # not read back by parse_envelope/plan_import.
    app_version: str = "dev"

    secret_key: str = "dev-secret-change-me"
    jwt_ttl_hours: int = 12

    ldap_url: str = "ldap://localhost:1389"
    ldap_bind_dn: str = "cn=admin,dc=example,dc=org"
    ldap_bind_password: str = "adminpassword"
    ldap_user_base: str = "ou=users,dc=example,dc=org"
    ldap_user_filter: str = "(uid={username})"
    ldap_group_base: str = "ou=groups,dc=example,dc=org"
    # Admin group DNs, ';'-separated (NOT ',' -- a DN is itself comma-
    # separated, e.g. "dn1;dn2;dn3").
    ldap_admin_groups: str = "cn=kam-admins,ou=groups,dc=example,dc=org"

    default_cluster_name: str = "local"
    prometheus_url: str = "http://localhost:30090"
    alertmanager_url: str = "http://localhost:30093"
    webhook_token: str = "dev-webhook-token"

    # Base URL Alertmanager itself can reach this app's webhook receiver at
    # -- used only to render the copy-paste AM receiver config snippet
    # returned (once) by POST/PATCH /clusters. Defaults to what the dev kind
    # cluster's Alertmanager already uses (dev/kube-prometheus-values.yaml)
    # to reach the host machine from inside Docker.
    webhook_base_url: str = "http://host.docker.internal:8000"

    # SMTP connection settings for the built-in email channel. Per-channel
    # config (recipients, subject prefix) lives in `channels.config_encrypted`
    # instead -- these are app-wide (one mail relay per deployment), not
    # something a channel owner configures. Defaults match the dev Mailpit
    # container (docker-compose.dev.yml).
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False

    # Directory scanned for third-party channel plugin `*.py` files at
    # startup (see app/channels/registry.py). Empty string disables
    # plugins-dir discovery entirely.
    plugins_dir: str = ""

    # Base URL the frontend is served from -- used to build a notification
    # payload's app_url (a deep link back to the alert's history detail).
    app_base_url: str = "http://localhost:5173"

    # 'embedded': app.main's lifespan runs the outbox worker loop as a
    # background asyncio task. 'off': no worker runs in-process (used by
    # the test app fixture, and by any deployment running
    # `python -m app.worker.runner` as a separate process instead).
    worker_mode: str = "embedded"


@lru_cache
def get_settings() -> Settings:
    return Settings()

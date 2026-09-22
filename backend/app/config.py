from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAM_")

    database_url: str = "sqlite+aiosqlite:///./kam.db"
    app_name: str = "kam"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()

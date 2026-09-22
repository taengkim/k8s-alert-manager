from httpx import AsyncClient

from tests.conftest import login_as


async def test_login_success_sets_cookie_and_me_works(client: AsyncClient) -> None:
    response = await login_as(client, username="alice")
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "alice"
    assert body["is_admin"] is False
    assert "kam_token" in response.cookies

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


async def test_wrong_password_returns_401(client: AsyncClient) -> None:
    # authenticate() returns None for a bad password.
    from unittest.mock import patch

    with patch("app.api.auth.authenticate", lambda u, p: None):
        bad = await client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "wrong"}
        )
    assert bad.status_code == 401
    assert bad.json()["detail"] == "invalid credentials"


async def test_inactive_user_login_returns_403(client: AsyncClient) -> None:
    # First login creates the user (and makes alice an admin via group).
    admin_login = await login_as(
        client,
        username="alice",
        group_dns=["cn=kam-admins,ou=groups,dc=example,dc=org"],
    )
    assert admin_login.status_code == 200

    # Log bob in once so the account exists, then deactivate via admin API.
    bob_client_login = await login_as(client, username="bob")
    assert bob_client_login.status_code == 200
    bob_id = bob_client_login.json()["id"]

    await client.post("/api/v1/auth/logout")
    # Log back in as admin (alice) to deactivate bob.
    await login_as(
        client,
        username="alice",
        group_dns=["cn=kam-admins,ou=groups,dc=example,dc=org"],
    )
    patch_resp = await client.patch(
        f"/api/v1/admin/users/{bob_id}", json={"is_active": False}
    )
    assert patch_resp.status_code == 200
    await client.post("/api/v1/auth/logout")

    # bob attempts to log in again -> 403.
    second_login = await login_as(client, username="bob")
    assert second_login.status_code == 403


async def test_logout_clears_cookie(client: AsyncClient) -> None:
    await login_as(client, username="alice")
    logout = await client.post("/api/v1/auth/logout")
    assert logout.status_code == 200

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 401


async def test_admin_group_dn_grants_is_admin(client: AsyncClient) -> None:
    response = await login_as(
        client,
        username="alice",
        group_dns=["cn=kam-admins,ou=groups,dc=example,dc=org"],
    )
    assert response.status_code == 200
    assert response.json()["is_admin"] is True


async def test_is_admin_not_demoted_when_group_absent_later(client: AsyncClient) -> None:
    first = await login_as(
        client,
        username="alice",
        group_dns=["cn=kam-admins,ou=groups,dc=example,dc=org"],
    )
    assert first.json()["is_admin"] is True

    await client.post("/api/v1/auth/logout")

    second = await login_as(client, username="alice", group_dns=[])
    assert second.status_code == 200
    assert second.json()["is_admin"] is True

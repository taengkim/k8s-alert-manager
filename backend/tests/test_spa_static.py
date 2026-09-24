"""Tests for app/main.py's SPA static serving (STATIC_DIR/serve_spa).

Uses its own local fixtures rather than conftest.py's shared `app`/`client`
fixtures: this deliberately points `app.main.STATIC_DIR` at a throwaway
directory (conftest's `app` fixture leaves it unset, since a source checkout
without the packaging Dockerfile's frontend-build stage has no static/ dir
at all -- see STATIC_DIR's own docstring) and never touches the database, so
there's no need for the DB session override or the lifespan context either.
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module


@pytest.fixture
def spa_static_dir(tmp_path: Path) -> Path:
    static_dir = tmp_path / "static"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>spa-shell</html>")
    (assets_dir / "app.js").write_text("console.log('asset');")

    # A sentinel file OUTSIDE the static root, one level up -- this must
    # never be reachable through the SPA fallback no matter how `full_path`
    # is crafted. Stands in for a real deployment's /etc/passwd or
    # /var/run/secrets/kubernetes.io/serviceaccount/token.
    (tmp_path / "secret.txt").write_text("top-secret-outside-static-root")

    return static_dir


@pytest.fixture
def spa_app(spa_static_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_module, "STATIC_DIR", spa_static_dir)
    return main_module.create_app()


@pytest.fixture
async def spa_client(spa_app):
    transport = ASGITransport(app=spa_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _find_spa_route(spa_app):
    for route in spa_app.routes:
        if getattr(route, "path", None) == "/{full_path:path}":
            return route
    raise AssertionError("SPA fallback route not registered")


async def _call_spa_handler(spa_app, full_path: str):
    """Call the `serve_spa` endpoint directly with a pre-decoded `full_path`.

    Bypasses HTTP client/URL-parsing layers entirely (some of which
    normalize `../` out of a path before a request is ever sent -- see
    test_path_traversal_* below), which is exactly what's needed to
    reproduce what Starlette's `{full_path:path}` converter actually hands
    the handler: it does NOT normalize dot-segments out of the raw path
    before routing, so `full_path` can and does arrive containing literal
    `..` segments from a crafted request.
    """
    route = _find_spa_route(spa_app)
    return await route.endpoint(full_path=full_path)


# -- happy paths --------------------------------------------------------


async def test_root_serves_spa_shell(spa_client: AsyncClient) -> None:
    response = await spa_client.get("/")
    assert response.status_code == 200
    assert "spa-shell" in response.text


async def test_client_side_route_falls_back_to_spa_shell(spa_client: AsyncClient) -> None:
    response = await spa_client.get("/alerts/123")
    assert response.status_code == 200
    assert "spa-shell" in response.text


async def test_asset_served_directly(spa_client: AsyncClient) -> None:
    response = await spa_client.get("/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


async def test_unknown_api_path_404s_instead_of_falling_back(spa_client: AsyncClient) -> None:
    response = await spa_client.get("/api/v1/does-not-exist")
    assert response.status_code == 404


# -- path traversal containment (regression for the fix) ----------------


async def test_path_traversal_dotdot_is_contained(spa_app, spa_static_dir: Path) -> None:
    response = await _call_spa_handler(spa_app, "../secret.txt")
    # Falls back to the SPA shell, never the sentinel file outside the root.
    assert Path(response.path) == spa_static_dir / "index.html"


async def test_path_traversal_deep_dotdot_is_contained(spa_app, spa_static_dir: Path) -> None:
    # Enough "../" to walk all the way up to the filesystem root and back
    # down to /etc/passwd -- computed from spa_static_dir's actual depth
    # (not a fixed guess) so this reliably targets a real file regardless of
    # how deeply pytest nests its tmp_path fixture. A fixed count (e.g. a
    # hardcoded 6) would silently pass on a pre-fix build too, for the wrong
    # reason -- not enough ".." to reach a real file at all, rather than the
    # fix actually containing it.
    depth = len(spa_static_dir.resolve().parts) - 1  # parts[0] is "/" itself
    full_path = "../" * depth + "etc/passwd"
    response = await _call_spa_handler(spa_app, full_path)
    assert Path(response.path) == spa_static_dir / "index.html"


async def test_path_traversal_decoded_dotdot_is_contained(spa_app, spa_static_dir: Path) -> None:
    # A raw wire path of "%2e%2e%2Fsecret.txt" is what a request actually
    # sends for this -- Starlette/the ASGI server percent-decodes it to
    # "../secret.txt" before this handler ever sees `full_path`, so building
    # it via real percent-decoding (rather than typing "../" again) makes
    # that equivalence explicit instead of just duplicating the first test.
    from urllib.parse import unquote

    full_path = unquote("%2e%2e%2Fsecret.txt")
    assert full_path == "../secret.txt"
    response = await _call_spa_handler(spa_app, full_path)
    assert Path(response.path) == spa_static_dir / "index.html"


async def _raw_asgi_get(asgi_app, raw_path: str) -> tuple[int, bytes]:
    """Drive `asgi_app` with LITERALLY `raw_path` as the ASGI scope's `path`.

    httpx (like curl without --path-as-is, and most HTTP clients/proxies)
    collapses `../` out of a URL client-side per RFC 3986 before a request
    is ever sent, so `AsyncClient.get("/../secret.txt")` never actually
    reaches the server with that literal path -- it can't reproduce what a
    raw request (curl --path-as-is, or any client that doesn't normalize)
    delivers. Building the ASGI scope directly proves what actually matters
    here: that Starlette's OWN routing layer doesn't normalize dot-segments
    out of `full_path` either, so containment has to happen in the handler.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await asgi_app(scope, receive, send)

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


async def test_path_traversal_via_raw_asgi_request_is_contained(spa_app) -> None:
    _status, body = await _raw_asgi_get(spa_app, "/../secret.txt")
    # Whatever the response, it must never be the sentinel file's contents.
    assert b"top-secret-outside-static-root" not in body


def test_static_root_never_contains_traversal_target(spa_static_dir: Path) -> None:
    """Sanity check on the fixture itself: confirms the sentinel file really
    is outside spa_static_dir (i.e. this test setup would have caught the
    pre-fix bug) rather than the test passing vacuously.
    """
    secret = spa_static_dir.parent / "secret.txt"
    assert secret.is_file()
    assert not secret.resolve().is_relative_to(spa_static_dir.resolve())

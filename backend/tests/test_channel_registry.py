"""Tests for app/channels/registry.py: entry-point + plugins-dir discovery,
type_name collision handling (built-in always wins), and single-bad-file
isolation (one broken plugin must not break discovery of the rest).
"""

import logging
from unittest.mock import patch

from app.channels.email import EmailChannel
from app.channels.registry import ChannelRegistry
from app.config import get_settings

VALID_PLUGIN_SOURCE = '''
from pydantic import BaseModel

from app.channels.base import NotificationChannel


class FakeConfig(BaseModel):
    target: str = "nowhere"


class FakeChannel(NotificationChannel):
    type_name = "fake"
    display_name = "Fake"
    config_schema = FakeConfig

    async def send(self, notification):
        pass
'''

COLLIDING_PLUGIN_SOURCE = '''
from pydantic import BaseModel

from app.channels.base import NotificationChannel


class EvilConfig(BaseModel):
    pass


class EvilEmailChannel(NotificationChannel):
    type_name = "email"  # collides with the built-in
    display_name = "Evil Email"
    config_schema = EvilConfig

    async def send(self, notification):
        pass
'''

BROKEN_PLUGIN_SOURCE = "def totally broken syntax(:\n"

# Imports fine -- the module itself is valid Python -- but the class is a
# malformed NotificationChannel subclass (no `type_name`). Registering it
# raises AttributeError from inside `_register`, which must be caught
# per-class rather than propagating out of discover().
MISSING_TYPE_NAME_PLUGIN_SOURCE = '''
from pydantic import BaseModel

from app.channels.base import NotificationChannel


class BrokenConfig(BaseModel):
    pass


class BrokenChannel(NotificationChannel):
    # No type_name.
    display_name = "Broken"
    config_schema = BrokenConfig

    async def send(self, notification):
        pass
'''


def _write(tmp_path, name: str, source: str):
    path = tmp_path / name
    path.write_text(source)
    return path


def test_discover_registers_builtin_email():
    registry = ChannelRegistry()
    with patch.object(get_settings(), "plugins_dir", ""):
        registry.discover()
    assert registry.get("email") is EmailChannel


def test_discover_loads_valid_plugin_from_plugins_dir(tmp_path):
    _write(tmp_path, "fake_channel.py", VALID_PLUGIN_SOURCE)
    registry = ChannelRegistry()
    with patch.object(get_settings(), "plugins_dir", str(tmp_path)):
        registry.discover()

    cls = registry.get("fake")
    assert cls is not None
    assert cls.display_name == "Fake"
    types = {item["type_name"] for item in registry.list()}
    assert {"email", "fake"} <= types


def test_broken_plugin_file_is_skipped_and_others_still_load(tmp_path, caplog):
    _write(tmp_path, "broken.py", BROKEN_PLUGIN_SOURCE)
    _write(tmp_path, "fake_channel.py", VALID_PLUGIN_SOURCE)

    registry = ChannelRegistry()
    with (
        patch.object(get_settings(), "plugins_dir", str(tmp_path)),
        caplog.at_level(logging.ERROR),
    ):
        registry.discover()

    # The broken file is logged and skipped -- the app (registry) still
    # comes up with everything else intact.
    assert registry.get("fake") is not None
    assert registry.get("email") is EmailChannel
    assert any("broken.py" in rec.message for rec in caplog.records)


def test_plugin_class_missing_type_name_is_skipped_and_others_still_load(tmp_path, caplog):
    """Regression: a plugin file that imports fine but whose
    NotificationChannel subclass is malformed (missing `type_name`) used to
    raise AttributeError straight out of discover() -- this must instead be
    caught per-class, logged, and skipped, leaving every other channel
    (including ones from the very same file) registered.
    """
    _write(tmp_path, "broken_class.py", MISSING_TYPE_NAME_PLUGIN_SOURCE)
    _write(tmp_path, "fake_channel.py", VALID_PLUGIN_SOURCE)

    registry = ChannelRegistry()
    with (
        patch.object(get_settings(), "plugins_dir", str(tmp_path)),
        caplog.at_level(logging.WARNING),
    ):
        registry.discover()  # must not raise

    assert registry.get("email") is EmailChannel
    assert registry.get("fake") is not None
    assert any(
        "BrokenChannel" in rec.message and "broken_class.py" in rec.message
        for rec in caplog.records
    )


def test_duplicate_type_name_builtin_wins(tmp_path, caplog):
    _write(tmp_path, "evil.py", COLLIDING_PLUGIN_SOURCE)

    registry = ChannelRegistry()
    with (
        patch.object(get_settings(), "plugins_dir", str(tmp_path)),
        caplog.at_level(logging.WARNING),
    ):
        registry.discover()

    assert registry.get("email") is EmailChannel
    assert any("already registered" in rec.message for rec in caplog.records)


class _FakeEntryPoint:
    def __init__(self, name: str, value: str, cls_or_exc):
        self.name = name
        self.value = value
        self._cls_or_exc = cls_or_exc

    def load(self):
        if isinstance(self._cls_or_exc, type) and issubclass(self._cls_or_exc, Exception):
            raise self._cls_or_exc("boom")
        return self._cls_or_exc


def test_entry_point_discovery_registers_channel():
    from pydantic import BaseModel

    from app.channels.base import NotificationChannel

    class EpConfig(BaseModel):
        pass

    class EpChannel(NotificationChannel):
        type_name = "ep-channel"
        display_name = "EP Channel"
        config_schema = EpConfig

        async def send(self, notification):
            pass

    fake_ep = _FakeEntryPoint("ep", "somewhere:EpChannel", EpChannel)

    registry = ChannelRegistry()
    with (
        patch("importlib.metadata.entry_points", return_value=[fake_ep]),
        patch.object(get_settings(), "plugins_dir", ""),
    ):
        registry.discover()

    assert registry.get("ep-channel") is EpChannel
    assert registry.get("email") is EmailChannel


def test_broken_entry_point_is_skipped():
    fake_ep = _FakeEntryPoint("broken", "somewhere:Broken", ImportError)

    registry = ChannelRegistry()
    with (
        patch("importlib.metadata.entry_points", return_value=[fake_ep]),
        patch.object(get_settings(), "plugins_dir", ""),
    ):
        registry.discover()

    # Built-in discovery still succeeds despite the broken entry point.
    assert registry.get("email") is EmailChannel


def test_entry_point_channel_missing_type_name_is_skipped(caplog):
    """Same regression as the plugins-dir case above, for the entry-point
    discovery path: the class loads fine (`.load()` succeeds) but is itself
    malformed (missing `type_name`), so registering it raises AttributeError
    -- must be caught per-entry-point, not propagate out of discover().
    """
    from pydantic import BaseModel

    from app.channels.base import NotificationChannel

    class BrokenConfig(BaseModel):
        pass

    class BrokenChannel(NotificationChannel):
        display_name = "Broken"
        config_schema = BrokenConfig

        async def send(self, notification):
            pass

    fake_ep = _FakeEntryPoint("broken-class", "somewhere:BrokenChannel", BrokenChannel)

    registry = ChannelRegistry()
    with (
        patch("importlib.metadata.entry_points", return_value=[fake_ep]),
        patch.object(get_settings(), "plugins_dir", ""),
        caplog.at_level(logging.WARNING),
    ):
        registry.discover()  # must not raise

    assert registry.get("email") is EmailChannel
    assert any("BrokenChannel" in rec.message for rec in caplog.records)


async def test_app_boots_and_channel_types_works_despite_malformed_plugin(tmp_path):
    """Regression for the app-lifespan half of the fix: app/main.py's
    lifespan must not crash startup even if channel_registry.discover()
    itself somehow raises (defense in depth on top of the per-class
    isolation tested above). This builds the app the same way
    tests/conftest.py's `app` fixture does, but with `plugins_dir` patched
    to a directory containing a malformed plugin -- something the shared
    fixture can't parametrize since it's already resolved by the time a
    test body runs.
    """
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db as db_module
    from app.db import Base, get_session
    from app.main import create_app
    from tests.conftest import login_as

    _write(tmp_path, "broken_class.py", MISSING_TYPE_NAME_PLUGIN_SOURCE)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with session_factory() as session:
            yield session

    with patch.object(get_settings(), "plugins_dir", str(tmp_path)):
        fastapi_app = create_app()
        fastapi_app.dependency_overrides[get_session] = override_get_session
        db_module.async_session_factory = session_factory

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # This is the actual regression check: if discover() (or the
        # lifespan's handling of it) weren't isolated, entering the
        # lifespan context here would raise and the app would never boot.
        async with fastapi_app.router.lifespan_context(fastapi_app):
            from httpx import ASGITransport, AsyncClient

            transport = ASGITransport(app=fastapi_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await login_as(client, username="alice")
                resp = await client.get("/api/v1/channel-types")
                assert resp.status_code == 200
                types = {item["type_name"] for item in resp.json()}
                assert "email" in types

    await engine.dispose()

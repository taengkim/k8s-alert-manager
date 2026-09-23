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

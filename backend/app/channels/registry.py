"""Discovers and holds every available `NotificationChannel` type.

Three sources, in this fixed order (first registration of a `type_name`
wins -- see `_register`):

1. The built-in email channel, always registered first so no plugin or
   entry point can shadow it.
2. `importlib.metadata` entry points in the `kam.channels` group -- each
   entry point is expected to resolve directly to a `NotificationChannel`
   subclass (the packaging-based extension mechanism: a plugin ships as an
   installed distribution and advertises itself via `pyproject.toml`
   `[project.entry-points."kam.channels"]`).
3. `*.py` files in `settings.plugins_dir` (disabled when empty) -- the
   drop-a-file extension mechanism, see `plugins/example_webhook_channel/`.
   Every `NotificationChannel` subclass defined directly in such a file is
   registered.

A single broken plugin (syntax error, import error, whatever) is logged and
skipped rather than blocking startup -- one bad file must not take the whole
app down.
"""

import importlib.metadata
import importlib.util
import inspect
import logging
import time
from pathlib import Path
from typing import Any

from app.channels.base import NotificationChannel
from app.channels.email import EmailChannel
from app.config import get_settings

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "kam.channels"


class ChannelRegistry:
    def __init__(self) -> None:
        self._channels: dict[str, type[NotificationChannel]] = {}
        # type_name -> where it came from, for the "already registered by"
        # warning when a later source collides.
        self._sources: dict[str, str] = {}

    def discover(self) -> None:
        """(Re-)populate the registry from all three sources. Safe to call
        more than once (e.g. in tests) -- each call starts from empty.
        """
        self._channels = {}
        self._sources = {}

        self._register(EmailChannel, source="builtin")
        self._discover_entry_points()
        self._discover_plugins_dir()

    def _register(self, cls: type[NotificationChannel], *, source: str) -> None:
        type_name = cls.type_name
        if type_name in self._channels:
            logger.warning(
                "channel type '%s' from %s ignored: already registered by %s",
                type_name,
                source,
                self._sources[type_name],
            )
            return
        self._channels[type_name] = cls
        self._sources[type_name] = source

    def _discover_entry_points(self) -> None:
        try:
            entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        except Exception:
            logger.exception("failed to enumerate '%s' entry points", ENTRY_POINT_GROUP)
            return

        for entry_point in entry_points:
            try:
                cls = entry_point.load()
                if not (isinstance(cls, type) and issubclass(cls, NotificationChannel)):
                    raise TypeError(
                        f"entry point '{entry_point.name}' does not resolve to a "
                        "NotificationChannel subclass"
                    )
            except Exception:
                logger.exception(
                    "failed to load channel entry point '%s' (%s) -- skipping",
                    entry_point.name,
                    entry_point.value,
                )
                continue
            self._register(cls, source=f"entry point '{entry_point.name}'")

    def _discover_plugins_dir(self) -> None:
        plugins_dir = get_settings().plugins_dir
        if not plugins_dir:
            return

        directory = Path(plugins_dir)
        if not directory.is_dir():
            logger.warning("KAM_PLUGINS_DIR '%s' is not a directory -- skipping", plugins_dir)
            return

        for path in sorted(directory.glob("*.py")):
            try:
                module = _load_module_from_path(path)
            except Exception:
                logger.exception(
                    "failed to load plugin file '%s' -- skipping (app continues)", path
                )
                continue

            found_any = False
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, NotificationChannel)
                    and obj is not NotificationChannel
                    and obj.__module__ == module.__name__
                ):
                    found_any = True
                    self._register(obj, source=f"plugin file '{path.name}'")

            if not found_any:
                logger.warning(
                    "plugin file '%s' defines no NotificationChannel subclass", path
                )

    def get(self, type_name: str) -> type[NotificationChannel] | None:
        return self._channels.get(type_name)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "type_name": cls.type_name,
                "display_name": cls.display_name,
                "json_schema": cls.config_schema.model_json_schema(),
            }
            for cls in sorted(self._channels.values(), key=lambda c: c.type_name)
        ]


def _load_module_from_path(path: Path):
    # A unique-per-call module name: two discover() calls (e.g. across
    # tests) loading the same filename must not collide, and we don't need
    # this name to mean anything beyond identifying classes defined in it.
    module_name = f"_kam_plugin_{path.stem}_{time.monotonic_ns()}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load spec for plugin file '{path}'")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

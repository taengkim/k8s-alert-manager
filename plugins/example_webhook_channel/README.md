# Example channel plugin: webhook

This directory is a reference implementation of a third-party notification
channel, showing the two ways k8s-alert-manager can pick up a channel type
it doesn't ship with.

**Drop-a-file (what this example uses).** Write a module that defines one
or more subclasses of `app.channels.base.NotificationChannel`, each
declaring a unique `type_name`, a `display_name`, a `config_schema`
(a pydantic model describing that channel's per-instance config, e.g. a
webhook's `url`), and an async `send(notification)` that raises
`ChannelDeliveryError` on failure. `webhook_channel.py` in this directory is
exactly that.

**Load it.** Point `KAM_PLUGINS_DIR` at a directory containing your file(s)
(for this example: `KAM_PLUGINS_DIR=/path/to/plugins/example_webhook_channel`)
and restart the app. On startup, `app/channels/registry.py` scans every
`*.py` file in that directory and registers every `NotificationChannel`
subclass it finds. A file that fails to import (syntax error, missing
dependency, whatever) is logged and skipped -- it won't take the rest of the
app down. Once loaded, the type shows up in `GET /api/v1/channel-types`
(including its config's JSON Schema, so the UI can render a config form for
it automatically) and teams can create channels of that type like any
built-in one.

**Entry points (packaging-based alternative).** If you'd rather ship your
channel as an installed Python distribution instead of a loose file, expose
it via an entry point in the `kam.channels` group instead of `KAM_PLUGINS_DIR`
-- e.g. in your package's `pyproject.toml`:

```toml
[project.entry-points."kam.channels"]
webhook = "my_package.webhook_channel:WebhookChannel"
```

The registry loads every entry point in that group the same way it loads
plugins-dir files. If both a `KAM_PLUGINS_DIR` file and an entry point (or
the built-in email channel) declare the same `type_name`, whichever was
discovered first wins -- built-ins first, then entry points, then
plugins-dir -- and the loser is logged as a warning, not silently dropped.

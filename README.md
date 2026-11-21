# Pancake Development Environment (PDE)

Pancake Development Environment (PDE) is a small, standalone testing environment for developers who want to test Pancake-style Discord plugins using a private Discord bot. It does not connect to Pancake Core or its production database.

Plugin installation state, plugin test data, and Custom Events metadata are stored locally in `data.json`. PDE does not require cogs, the core permission manager, development commands, external services, or remote plugin downloads.

> **Testing only.** The included license strictly prohibits production use and use as a public bot. Use PDE only with a private test bot and private test servers.

## Features

- `/install <plugin>`: loads `plugins/<plugin>.py`, registers non-conflicting application commands for the current server, and records the installation in `data.json`.
- `/installed-list`: lists plugins recorded as installed for the current server and reports their command status.
- `/uninstall <plugin|all>`: requests confirmation, stops that plugin's Custom Events consumers and emitters, removes its registered commands, and deletes its installation record and plugin-specific JSON test data.
- **Per-server restoration:** reads each server's installed plugin names from `data.json` when the bot starts or joins a server. Plugins are restored after the Custom Events manager starts, so their `on_plugin_load(guild)` hooks can recreate subscriptions and emitter registrations.
- **Local JSON storage:** provides a small API for plugin-specific test data, scoped by server and plugin.
- **Custom Events manager:** `bot.custom_events` exposes a JSON-backed manager with event schemas, emitter registration, subscriptions, asynchronous delivery, rate limiting, and lifecycle cleanup. Its plugin-facing calls follow the public Custom Events interface used by Pancake Core.

Commands are synchronized per server, not globally. There is no command permission manager in PDE: anyone who can use application commands in a test server may run `/install` and `/uninstall`. Keep the bot confined to private test servers.

## Project layout

```text
Pancake-Development-Environment/
├── bot.py
├── config.py
├── db.py
├── custom_events.py
├── data.json
├── .env
├── LICENSE
├── README.md
├── requirements.txt
└── plugins/
    ├── example.py
    ├── ping.py
    ├── eventsource.py
    └── eventlistener.py
```

## Requirements

- Python 3.10 or newer.
- A Discord application/bot you control, created in the [Discord Developer Portal](https://discord.com/developers/applications).
- A private Discord server for testing.
- The libraries listed in `requirements.txt`: `discord.py`, `python-dotenv`, `pymongo`, `requests`, and `pillow`.

PDE itself uses local JSON storage; installing PyMongo does not configure or connect to MongoDB.

## Setup

1. Extract the ZIP into a dedicated folder.
2. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   ```

   **On Windows:**

   ```powershell
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

   **On macOS/Linux:**

   ```bash
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Open `.env` and add your private test bot's token after `TOKEN=`:

   ```dotenv
   TOKEN=YOUR_PRIVATE_TEST_BOT_TOKEN
   ```

   The included `.env` file intentionally contains a blank token field. You should never share this file or your token with anyone. This means that you should never paste it into a plugin file, a GitHub Gist, a forum post, a screenshot, or a support message. If it is exposed, reset it in the Developer Portal immediately. No moderator will ever ask you to share it.

4. In the Discord Developer Portal, enable the **Message Content Intent** and **Server Members Intent** only if your plugins need them. PDE enables these intents in its code to make plugin testing easier. Discord may also require them to be enabled in the portal. Do not grant the bot permissions that your tests do not need.
5. Generate an invite URL with the `bot` and `applications.commands` scopes. Invite the bot only to your private test server.
6. Start the bot:

   ```bash
   python bot.py
   ```

   The initial per-server command sync may take a short time. Check the terminal if commands do not appear or if a plugin fails to load.

## Configure `config.py`

The included `config.py` contains the following settings:

```python
BOT_VERSION = "Pancake Development Environment 1.0.0"
BOT_PREFIX = "//"
```

This file is not essential, but if you wish to include any configuration required by your plugin, you may add it here and also mention it in your submission ticket.

## Add and test a plugin

1. Put each plugin file directly in `plugins/`, for example `plugins/example.py` or `plugins/ping.py`.
2. The plugin class name must be the filename with its first letter capitalized, followed by `Plugin`. For `example.py`, use `ExamplePlugin`. For `ping.py`, use `PingPlugin`.
3. The class must accept `bot` in `__init__` and implement `get_commands()`, returning a list or tuple of `discord.app_commands.Command` or `discord.app_commands.Group` objects.
4. Optionally implement `on_plugin_load(guild)` and `on_plugin_unload(guild)`. These hooks may be synchronous functions or async functions.
5. Start the bot and run `/install example` in your test server. Then try `/example` and `/status`. You can also run `/install ping` to test the bundled Ping plugin.
6. Use `/installed-list` to inspect the saved installation. Run `/uninstall example` or `/uninstall ping` to remove the corresponding plugin.

`plugins/example.py` is the community starter template supplied for plugin authors. It includes `/example` and `/status`. `plugins/ping.py` is a second, ready-to-install example with `/ping` and `/ping-ms`. If you edit a plugin that is already loaded, uninstall and reinstall it (or restart the bot) to load the changes.

Two additional files demonstrate Custom Events: `plugins/eventsource.py` registers and emits `demo.message_published`, while `plugins/eventlistener.py` subscribes to it and posts a message when the event is received. Install both plugins in the same test server, then run `/event-publish`.

## Local JSON API (`db.py`)

`db.py` intentionally implements only a small subset of the storage behavior needed for plugin testing. It is not a replacement for the Pancake Core database.

The available functions are:

- `get_plugins(guild_id) -> list[str]`
- `save_plugin(guild_id, plugin_name) -> bool`
- `remove_plugin(guild_id, plugin_name) -> bool`
- `get_plugin_data(guild_id, plugin_name) -> dict`
- `set_plugin_data(guild_id, plugin_name, plugin_data: dict) -> bool`
- `delete_plugin_data(guild_id, plugin_name) -> bool`
- `delete_guild_data(guild_id) -> bool`

Custom values must be JSON-serializable (objects/dictionaries, arrays/lists, strings, numbers, booleans, and null). Installed plugin names are stored under `guilds`; optional test data is stored under `plugin_data`, separately by server and plugin. PDE deletes a plugin's JSON test data when it is uninstalled and deletes all local test state for a server when the bot leaves that server.

The local database does not implement the real core's settings, storage or data. Custom Events metadata is stored in the `custom_events` section of `data.json` and is accessed through `bot.custom_events`, not through a plugin-facing database collection API.

## Custom Events API

PDE includes a local Custom Events implementation. It is initialized automatically when the bot connects and is available to plugins as `self.bot.custom_events`. Do not create a second manager, connect to MongoDB, or import the internal JSON store from a plugin.

The public API follows the plugin-facing Pancake Custom Events interface:

- `register_emitter(...)` declares that an installed plugin resource can publish a named event and its payload schema.
- `emit(...)` validates a payload and queues an event for matching subscribers.
- `subscribe(...)` registers a callback for an event. Subscriptions may be created before an emitter exists.
- `unsubscribe(subscription_id)` removes one subscription.
- `unregister_emitter(...)` removes one emitter registration.
- `unregister_resource_emitters(...)` removes all emitter registrations for a resource.
- `get_event(...)` and `list_events(...)` inspect event definitions.
- `emit_derived(...)` publishes an event caused by another event, while preserving its actor and chain metadata.

Use `on_plugin_load(guild)` to register emitters and subscriptions. PDE has already saved the plugin's installed record before this hook is called. On uninstall, PDE stops the plugin's Custom Events registrations before calling `on_plugin_unload(guild)`. After a bot restart, plugin `on_plugin_load` hooks run again and should recreate their runtime callbacks and emitter registrations. The persisted subscription's schema version remains pinned; changing that version requires removing and recreating the subscription.

A minimal access pattern is:

```python
manager = getattr(self.bot, "custom_events", None)
if manager is None:
    return

await manager.subscribe(
    guild_id=guild.id,
    event_name="demo.message_published",
    plugin_id="myconsumer",
    consumer_id="message_listener",
    schema_version=1,
    callback=self.on_message_published,
)
```

The included `eventsource.py` and `eventlistener.py` files provide a complete working example. Install both in the same test server and run `/event-publish` to see an event move from the publisher to the consumer.

### PDE behavior and differences from Pancake Core

- Event definitions, schemas, emitter records, and subscription declarations are stored in `data.json` under `custom_events`. No MongoDB connection is opened.
- Event payloads are queued and delivered in memory. Pending emissions are discarded when the bot shuts down; they are not a durable message queue.
- The default behavior includes a 60-emissions-per-resource-per-60-seconds rate limit, a maximum derived-event chain depth of 8, bounded queues, and a seven-day grace period after the final emitter is removed.
- PDE logs Custom Events notices to the console. It does not send administrative direct messages.
- The Core's internal rule storage and rule execution are not part of PDE's public testing API.
- Custom Events metadata is local to this PDE instance. It is not shared with Pancake Core or another bot, and it is not a replacement for the production service.

The API is intended to help you test plugin-to-plugin event contracts.

## Security (read before installing plugins)

> **PDE is not a Python sandbox.** When `/install` loads a plugin, it imports and executes that Python file inside the bot process. Plugin code may be able to access the bot token, local files, the network, and the privileges of the operating-system account running the bot. Malicious code could steal the token or modify files.

- Install only plugins whose source code you have reviewed and trust.
- Use a separate token for testing. Never give your `.env` file or bot token to plugin authors.
- For third-party plugins, run PDE inside a virtual machine or isolated container without personal files or unrelated credentials, using minimal operating-system privileges.
- Never add this bot to public servers or use it as a public or production bot.

## Intentional limitations

PDE does not include cogs, a real connection with Pancake's database, command permission management or per-plugin configuration restoration. It does include a local JSON-backed Custom Events API for event declarations and plugin-to-plugin testing. `data.json` stores each server's installed plugin names, optional plugin test data, and Custom Events metadata.

Read `LICENSE` for the full terms. Production use and public-bot use are strictly prohibited.
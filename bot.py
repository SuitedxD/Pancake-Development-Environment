# Pancake Development Environment
# Copyright (c) 2026 Suited
# Testing-only software. See LICENSE and README.md before running plugins.

from __future__ import annotations

import asyncio
import copy
import importlib.util
import inspect
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import config
import db
from custom_events import CustomEventManager, JsonEventStore

ROOT = Path(__file__).resolve().parent
PLUGINS_DIR = ROOT / "plugins"
PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(ROOT / ".env")
TOKEN = os.getenv("TOKEN", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pancake-development-environment")

# Plugin files use a simple name such as plugins/sample.py -> SamplePlugin.
PLUGIN_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

intents = discord.Intents.default()
# These help with common plugin development. Enable the corresponding privileged
# intents in the Discord Developer Portal only when your test plugins need them.
intents.members = True
intents.message_content = True
intents.guild_reactions = True
intents.guild_scheduled_events = True
intents.voice_states = True
if hasattr(intents, "moderation"):
    intents.moderation = True

bot = commands.Bot(command_prefix=config.BOT_PREFIX, intents=intents)


@dataclass
class PluginRuntime:
    # In-memory state for one plugin installation in one guild.

    module_name: str
    module: ModuleType
    instance: Any
    commands: list[Any]
    registered_commands: list[Any]


_runtime_plugins: dict[tuple[int, str], PluginRuntime] = {}
_initialized_guilds: set[int] = set()


async def _custom_event_plugin_is_installed(guild_id: str, plugin_id: str) -> bool:
    # Keep Custom Events registrations scoped to plugins installed in this guild.
    try:
        return plugin_id in db.get_plugins(int(guild_id))
    except (TypeError, ValueError):
        return False


async def _ensure_custom_events_manager() -> CustomEventManager:
    # Start one local JSON-backed Custom Events manager for this bot process.
    manager = getattr(bot, "custom_events", None)
    if manager is None:
        manager = CustomEventManager(
            JsonEventStore(),
            plugin_is_installed=_custom_event_plugin_is_installed,
        )
        await manager.start()
        setattr(bot, "custom_events", manager)
        log.info("PDE Custom Events integration is ready (JSON storage).")
    elif not manager.is_running:
        await manager.start()
    return manager


def _plugin_path(plugin_name: str) -> Path:
    # Validate a plugin name and return its file directly under plugins/.
    if not PLUGIN_NAME_PATTERN.fullmatch(plugin_name):
        raise ValueError("Use the plugin filename without .py or a path.")
    path = (PLUGINS_DIR / f"{plugin_name}.py").resolve()
    if path.parent != PLUGINS_DIR.resolve():
        raise ValueError("Invalid plugin path.")
    return path


def _bind_command(command: Any, plugin_instance: Any) -> Any:
    # Copy a command for this installation and bind class-style callbacks if needed.
    #
    # Pancake-style plugins usually return app_commands.Command objects from
    # get_commands(). Copying avoids sharing one command object between guilds.
    bound_command = copy.copy(command)
    if isinstance(bound_command, app_commands.Command):
        callback = bound_command.callback
        try:
            first_parameter = next(iter(inspect.signature(callback).parameters.values()), None)
        except (TypeError, ValueError):
            first_parameter = None

        # If the callback is declared as a method (self, interaction, ...), bind
        # this guild's plugin instance. Commands built from self.method already
        # carry a binding and are also safe to copy here.
        if first_parameter is not None and first_parameter.name in {"self", "plugin", "extension"}:
            bound_command.binding = plugin_instance
    return bound_command


def _load_plugin_instance(plugin_name: str, guild_id: int) -> tuple[ModuleType, Any, list[Any], str]:
    # Import one plugin file and construct the expected <Name>Plugin class.
    path = _plugin_path(plugin_name)
    if not path.is_file():
        raise FileNotFoundError(f"Plugin file not found: {path.name}")

    module_name = f"_pde_plugin_{guild_id}_{plugin_name.casefold()}"
    previous_module = sys.modules.pop(module_name, None)
    del previous_module

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create a module loader for {path.name}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        class_name = plugin_name.capitalize() + "Plugin"
        plugin_class = getattr(module, class_name, None)
        if plugin_class is None or not inspect.isclass(plugin_class):
            raise AttributeError(f"The file must define a {class_name} class.")

        instance = plugin_class(bot)
        get_commands = getattr(instance, "get_commands", None)
        if not callable(get_commands):
            raise AttributeError("The plugin class must implement get_commands().")

        raw_commands = get_commands()
        if raw_commands is None:
            raw_commands = []
        if not isinstance(raw_commands, (list, tuple)):
            raw_commands = list(raw_commands)

        commands_for_guild: list[Any] = []
        for command in raw_commands:
            if not isinstance(command, (app_commands.Command, app_commands.Group)):
                raise TypeError("get_commands() must return app_commands.Command or app_commands.Group objects.")
            commands_for_guild.append(_bind_command(command, instance))

        return module, instance, commands_for_guild, module_name
    except Exception:
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name, None)
        raise


async def _run_plugin_hook(instance: Any, hook_name: str, guild: discord.Guild, plugin_name: str) -> bool:
    # Run an optional lifecycle hook. A hook failure is logged, not fatal.
    hook = getattr(instance, hook_name, None)
    if not callable(hook):
        return False
    try:
        result = hook(guild)
        if inspect.isawaitable(result):
            await result
        return False
    except Exception:
        log.exception("Plugin '%s' failed in %s for guild %s", plugin_name, hook_name, guild.id)
        return True


async def _activate_plugin(
    plugin_name: str,
    guild: discord.Guild,
    *,
    persist: bool,
) -> tuple[bool, list[str], list[str], bool, str | None]:
    # Load a plugin for one guild and register its non-conflicting commands.
    key = (guild.id, plugin_name)
    if key in _runtime_plugins:
        return True, [], [], False, "This plugin is already loaded in memory."

    try:
        module, instance, plugin_commands, module_name = _load_plugin_instance(plugin_name, guild.id)
    except Exception as exc:
        log.exception("Could not load plugin '%s' for guild %s", plugin_name, guild.id)
        return False, [], [], False, f"{type(exc).__name__}: {exc}"

    guild_ref = discord.Object(id=guild.id)
    added: list[Any] = []
    added_names: list[str] = []
    skipped_names: list[str] = []
    seen_names: set[str] = set()

    try:
        for command in plugin_commands:
            command_name = getattr(command, "name", None)
            if not isinstance(command_name, str) or not command_name:
                raise TypeError("A plugin command has no valid name.")
            if command_name in seen_names:
                skipped_names.append(command_name)
                continue
            seen_names.add(command_name)

            existing = bot.tree.get_command(command_name, guild=guild_ref)
            if existing is not None:
                skipped_names.append(command_name)
                continue

            try:
                bot.tree.add_command(command, guild=guild_ref)
                added.append(command)
                added_names.append(command_name)
            except Exception:
                log.exception("Could not register command '%s' from plugin '%s'", command_name, plugin_name)
                skipped_names.append(command_name)

        if persist and not db.save_plugin(guild.id, plugin_name):
            for command in added:
                bot.tree.remove_command(command.name, guild=guild_ref)
            if sys.modules.get(module_name) is module:
                sys.modules.pop(module_name, None)
            return False, [], skipped_names, False, "Could not save the installation to data.json."

        runtime = PluginRuntime(
            module_name=module_name,
            module=module,
            instance=instance,
            commands=plugin_commands,
            registered_commands=added,
        )
        _runtime_plugins[key] = runtime
        hook_failed = await _run_plugin_hook(instance, "on_plugin_load", guild, plugin_name)
        log.info(
            "Plugin '%s' loaded for guild %s (%s command(s) registered; %s skipped)",
            plugin_name,
            guild.id,
            len(added_names),
            len(skipped_names),
        )
        return True, added_names, skipped_names, hook_failed, None
    except Exception as exc:
        for command in added:
            try:
                bot.tree.remove_command(command.name, guild=guild_ref)
            except Exception:
                log.exception("Failed to roll back command '%s'", getattr(command, "name", "unknown"))
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name, None)
        log.exception("Unexpected error while activating plugin '%s'", plugin_name)
        return False, [], skipped_names, False, f"{type(exc).__name__}: {exc}"


async def _deactivate_plugin(guild: discord.Guild, plugin_name: str) -> tuple[bool, list[str], str | None]:
    # Unload one plugin, remove its registered commands, and clear its test data.
    key = (guild.id, plugin_name)
    runtime = _runtime_plugins.pop(key, None)
    removed_names: list[str] = []
    guild_ref = discord.Object(id=guild.id)

    manager = getattr(bot, "custom_events", None)
    if manager is not None:
        try:
            # Match Pancake's plugin lifecycle: stop event consumers and remove
            # the plugin's emitter declarations before calling its unload hook.
            await manager.uninstall_plugin(guild_id=guild.id, plugin_id=plugin_name)
        except Exception:
            log.exception("Custom Events cleanup failed for plugin '%s' in guild %s", plugin_name, guild.id)

    if runtime is not None:
        await _run_plugin_hook(runtime.instance, "on_plugin_unload", guild, plugin_name)
        for command in runtime.registered_commands:
            removed = bot.tree.remove_command(command.name, guild=guild_ref)
            if removed is not None:
                removed_names.append(command.name)
        if sys.modules.get(runtime.module_name) is runtime.module:
            sys.modules.pop(runtime.module_name, None)

    if not db.remove_plugin(guild.id, plugin_name):
        return False, removed_names, "The installed-plugin record could not be removed from data.json."

    if not db.delete_plugin_data(guild.id, plugin_name):
        log.warning("Could not clear JSON test data for plugin '%s' in guild %s", plugin_name, guild.id)

    log.info("Plugin '%s' uninstalled from guild %s", plugin_name, guild.id)
    return True, removed_names, None


async def _sync_guild(guild: discord.Guild) -> bool:
    try:
        await bot.tree.sync(guild=discord.Object(id=guild.id))
        return True
    except Exception:
        log.exception("Could not synchronize application commands for guild %s", guild.id)
        return False


async def _initialize_guild(guild: discord.Guild) -> None:
    # Prepare the guild, restore installed plugins, and synchronize its commands.
    await _ensure_custom_events_manager()
    if guild.id in _initialized_guilds:
        return

    guild_ref = discord.Object(id=guild.id)
    try:
        # Core commands are copied to guild scope for quick test-server updates.
        # They are never globally synced by this environment.
        bot.tree.copy_global_to(guild=guild_ref)
    except Exception:
        log.exception("Could not prepare commands for guild %s", guild.id)
        return

    for plugin_name in db.get_plugins(guild.id):
        try:
            path = _plugin_path(plugin_name)
            if not path.is_file():
                log.warning("Installed plugin '%s' has no file in plugins/", plugin_name)
                continue
            success, _, _, _, error = await _activate_plugin(plugin_name, guild, persist=False)
            if not success:
                log.error("Could not restore plugin '%s' in guild %s: %s", plugin_name, guild.id, error)
        except Exception:
            log.exception("Invalid plugin entry '%s' in guild %s", plugin_name, guild.id)

    if await _sync_guild(guild):
        _initialized_guilds.add(guild.id)
    log.info("Pancake Development Environment initialized for guild '%s' (%s)", guild.name, guild.id)


class UninstallConfirmationView(discord.ui.View):
    # A short confirmation prompt for the person who invoked /uninstall.

    def __init__(self, requester_id: int) -> None:
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran /uninstall can confirm this action.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content="Confirmed. Uninstalling…", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = False
        await interaction.response.edit_message(content="Uninstall cancelled. No changes were made.", view=None)
        self.stop()


@bot.tree.command(name="install", description="Install a local plugin in this test server.")
@app_commands.describe(plugin="Filename in plugins/, without the .py extension")
async def install_command(interaction: discord.Interaction, plugin: str) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    plugin = plugin.strip()
    try:
        path = _plugin_path(plugin)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    if not path.is_file():
        await interaction.response.send_message(f"`plugins/{plugin}.py` was not found.", ephemeral=True)
        return
    if plugin in db.get_plugins(guild.id):
        await interaction.response.send_message(f"`{plugin}` is already installed in this server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    success, added, skipped, hook_failed, error = await _activate_plugin(plugin, guild, persist=True)
    if not success:
        await interaction.followup.send(
            f"Could not install `{plugin}`. {error or 'Check the console for details.'}",
            ephemeral=True,
        )
        return

    sync_ok = await _sync_guild(guild)
    message = f"Plugin `{plugin}` installed. Commands registered: {len(added)}."
    if skipped:
        message += f" Commands skipped due to conflicts or duplicates: {', '.join(f'`{name}`' for name in skipped)}."
    if hook_failed:
        message += " The on_plugin_load hook failed; check the console."
    if not sync_ok:
        message += " Discord command sync failed; the state was saved to data.json and will be retried after reconnecting."
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(name="installed-list", description="Show plugins installed in this server.")
async def installed_list_command(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    installed = db.get_plugins(guild.id)
    embed = discord.Embed(
        title="📦 | INSTALLED PLUGINS",
        color=discord.Color.blurple(),
        description="Plugins recorded for this server in the Pancake Development Environment.",
    )
    if not installed:
        embed.description = "No plugins are installed in this server."
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    total_commands = 0
    max_fields = 25
    for plugin_name in installed[:max_fields]:
        try:
            path = _plugin_path(plugin_name)
        except ValueError:
            embed.add_field(name=f"⚠️ {plugin_name}", value="Invalid entry in data.json.", inline=False)
            continue

        runtime = _runtime_plugins.get((guild.id, plugin_name))
        if not path.is_file():
            detail = "File not found in `plugins/`."
        elif runtime is None:
            detail = "File exists, but the plugin is not loaded. Check the console."
        else:
            command_count = len(runtime.commands)
            registered_count = len(runtime.registered_commands)
            total_commands += registered_count
            detail = f"{command_count} command(s) detected; {registered_count} registered in this server."
        embed.add_field(name=plugin_name, value=detail, inline=False)

    if len(installed) > max_fields:
        embed.add_field(name="…", value=f"{len(installed) - max_fields} additional plugin entr(ies) omitted due to Discord’s field limit.", inline=False)
    embed.set_footer(text=f"{len(installed)} plugin(s) installed · {total_commands} command(s) registered")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="uninstall", description="Uninstall one plugin or all plugins from this server.")
@app_commands.describe(plugin="Plugin name, or 'all' to uninstall every plugin")
async def uninstall_command(interaction: discord.Interaction, plugin: str) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    plugin = plugin.strip()
    installed = db.get_plugins(guild.id)
    if not installed:
        await interaction.response.send_message("No plugins are installed in this server.", ephemeral=True)
        return

    uninstall_all = plugin.casefold() == "all"
    if not uninstall_all:
        try:
            _plugin_path(plugin)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if plugin not in installed:
            await interaction.response.send_message(f"`{plugin}` is not listed as installed.", ephemeral=True)
            return

    target = "all installed plugins" if uninstall_all else f"plugin `{plugin}`"
    view = UninstallConfirmationView(interaction.user.id)
    await interaction.response.send_message(
        f"Are you sure you want to uninstall {target}?\nIts installation record and JSON test data will be deleted.",
        view=view,
        ephemeral=True,
    )
    timed_out = await view.wait()
    if timed_out:
        await interaction.edit_original_response(content="Confirmation timed out. No changes were made.", view=None)
        return
    if not view.confirmed:
        return

    targets = list(installed) if uninstall_all else [plugin]
    removed_plugins: list[str] = []
    failed_plugins: list[str] = []
    removed_commands: list[str] = []
    for plugin_name in targets:
        success, removed, error = await _deactivate_plugin(guild, plugin_name)
        removed_commands.extend(removed)
        if success:
            removed_plugins.append(plugin_name)
        else:
            failed_plugins.append(f"{plugin_name}: {error or 'unknown error'}")

    sync_ok = await _sync_guild(guild)
    message = "Uninstall completed. " + (f"Removed: {', '.join(f'`{name}`' for name in removed_plugins)}." if removed_plugins else "No plugins could be removed.")
    if failed_plugins:
        message += " Errors: " + "; ".join(failed_plugins) + "."
    if removed_commands:
        message += f" Commands removed: {len(removed_commands)}."
    if not sync_ok:
        message += " Discord did not confirm command sync; check the console."
    await interaction.edit_original_response(content=message, view=None)


@bot.tree.command(name="help", description="Pancake Development Environment help (customization pending).")
async def help_command(interaction: discord.Interaction) -> None:
    # Intentional placeholder: adapt this embed for the plugin development guide.
    embed = discord.Embed(
        title="🥞 | PANCAKE DEVELOPMENT ENVIRONMENT",
        description="Plugin development help will be added here.",
        color=discord.Color.dark_embed(),
    )
    embed.set_footer(text=f"Version: {config.BOT_VERSION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready() -> None:
    log.info("Pancake Development Environment v%s connected as %s (%s)", config.BOT_VERSION, bot.user, getattr(bot.user, "id", "unknown"))
    await _ensure_custom_events_manager()
    for guild in list(bot.guilds):
        await _initialize_guild(guild)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    await _ensure_custom_events_manager()
    await _initialize_guild(guild)


@bot.event
async def on_guild_remove(guild: discord.Guild) -> None:
    # Drop runtime references and test data when this test bot leaves a server.
    manager = getattr(bot, "custom_events", None)
    for (guild_id, plugin_name), runtime in list(_runtime_plugins.items()):
        if guild_id != guild.id:
            continue
        if manager is not None:
            try:
                await manager.uninstall_plugin(guild_id=guild.id, plugin_id=plugin_name)
            except Exception:
                log.exception("Custom Events cleanup failed for departed guild %s / plugin %s", guild.id, plugin_name)
        await _run_plugin_hook(runtime.instance, "on_plugin_unload", guild, plugin_name)
        if sys.modules.get(runtime.module_name) is runtime.module:
            sys.modules.pop(runtime.module_name, None)
        _runtime_plugins.pop((guild_id, plugin_name), None)

    if manager is not None:
        try:
            await manager.delete_guild_data(guild.id)
        except Exception:
            log.exception("Could not clear Custom Events state for departed guild %s", guild.id)

    bot.tree.clear_commands(guild=discord.Object(id=guild.id))
    _initialized_guilds.discard(guild.id)
    if not db.delete_guild_data(guild.id):
        log.warning("Some local JSON test data could not be cleared for guild %s", guild.id)
    log.info("Cleared test state for departed guild %s", guild.id)


async def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "TOKEN is not set. Add your private test bot token to the TOKEN field in .env."
        )
    async with bot:
        try:
            await bot.start(TOKEN)
        finally:
            manager = getattr(bot, "custom_events", None)
            if manager is not None:
                await manager.close()
                try:
                    delattr(bot, "custom_events")
                except AttributeError:
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Pancake Development Environment stopped by user")

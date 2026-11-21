# Pancake Development Environment - local JSON storage
# Copyright (c) 2026 Suited

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("pancake-development-environment.db")
DATA_FILE = Path(__file__).resolve().with_name("data.json")
_LOCK = threading.RLock()
_DEFAULT_DATA: dict[str, Any] = {
    "guilds": {},
    "plugin_data": {},
    "custom_events": {"events": [], "emitters": [], "subscriptions": []},
}


def _read_data() -> dict[str, Any]:
    # Read and validate the small local database without overwriting corrupt JSON.
    if not DATA_FILE.exists():
        return copy.deepcopy(_DEFAULT_DATA)

    with DATA_FILE.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("The top level of data.json must be an object.")

    data.setdefault("guilds", {})
    data.setdefault("plugin_data", {})
    data.setdefault("custom_events", {"events": [], "emitters": [], "subscriptions": []})
    if not isinstance(data["guilds"], dict) or not isinstance(data["plugin_data"], dict):
        raise ValueError("data.json must contain object values for 'guilds' and 'plugin_data'.")
    if not isinstance(data["custom_events"], dict):
        raise ValueError("data.json 'custom_events' value must be an object.")
    for collection in ("events", "emitters", "subscriptions"):
        data["custom_events"].setdefault(collection, [])
        if not isinstance(data["custom_events"][collection], list):
            raise ValueError(f"data.json custom_events.{collection} must be a list.")
    return data


def _write_data(data: dict[str, Any]) -> None:
    # Persist changes atomically so interrupted writes are less likely to corrupt data.
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
    with temporary_file.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    os.replace(temporary_file, DATA_FILE)


def get_plugins(guild_id: int | str) -> list[str]:
    # Return installed plugin names for one guild. Mirrors Pancake's basic API.
    try:
        guild_key = str(int(guild_id))
        with _LOCK:
            data = _read_data()
            record = data["guilds"].get(guild_key, {})
            plugins = record.get("plugins", []) if isinstance(record, dict) else []
            return list(plugins) if isinstance(plugins, list) else []
    except Exception:
        log.exception("get_plugins failed for guild %s", guild_id)
        return []


def save_plugin(guild_id: int | str, plugin_name: str) -> bool:
    # Add a plugin to a guild's installed list, avoiding duplicate entries.
    try:
        guild_key = str(int(guild_id))
        name = str(plugin_name)
        with _LOCK:
            data = _read_data()
            record = data["guilds"].setdefault(guild_key, {"plugins": []})
            if not isinstance(record, dict):
                record = {"plugins": []}
                data["guilds"][guild_key] = record
            plugins = record.setdefault("plugins", [])
            if not isinstance(plugins, list):
                plugins = []
                record["plugins"] = plugins
            if name not in plugins:
                plugins.append(name)
            _write_data(data)
        return True
    except Exception:
        log.exception("save_plugin failed for guild %s and plugin %s", guild_id, plugin_name)
        return False


def remove_plugin(guild_id: int | str, plugin_name: str) -> bool:
    # Remove a plugin from a guild's installed list. Returns False if not present.
    try:
        guild_key = str(int(guild_id))
        name = str(plugin_name)
        with _LOCK:
            data = _read_data()
            record = data["guilds"].get(guild_key)
            if not isinstance(record, dict):
                return False
            plugins = record.get("plugins", [])
            if not isinstance(plugins, list) or name not in plugins:
                return False
            plugins.remove(name)
            if not plugins:
                data["guilds"].pop(guild_key, None)
            _write_data(data)
        return True
    except Exception:
        log.exception("remove_plugin failed for guild %s and plugin %s", guild_id, plugin_name)
        return False


def get_plugin_data(guild_id: int | str, plugin_name: str) -> dict[str, Any]:
    # Return a plugin-scoped dictionary for simple JSON-serializable test data.
    try:
        guild_key = str(int(guild_id))
        with _LOCK:
            data = _read_data()
            guild_data = data["plugin_data"].get(guild_key, {})
            plugin_data = guild_data.get(str(plugin_name), {}) if isinstance(guild_data, dict) else {}
            return copy.deepcopy(plugin_data) if isinstance(plugin_data, dict) else {}
    except Exception:
        log.exception("get_plugin_data failed for guild %s and plugin %s", guild_id, plugin_name)
        return {}


def set_plugin_data(guild_id: int | str, plugin_name: str, plugin_data: dict[str, Any]) -> bool:
    # Replace a plugin's stored dictionary for one guild; values must be JSON-compatible.
    try:
        if not isinstance(plugin_data, dict):
            raise TypeError("plugin_data must be a dictionary.")
        # Validate serializability before modifying the database.
        json.dumps(plugin_data, ensure_ascii=False, allow_nan=False)
        guild_key = str(int(guild_id))
        name = str(plugin_name)
        with _LOCK:
            data = _read_data()
            data["plugin_data"].setdefault(guild_key, {})[name] = copy.deepcopy(plugin_data)
            _write_data(data)
        return True
    except Exception:
        log.exception("set_plugin_data failed for guild %s and plugin %s", guild_id, plugin_name)
        return False


def delete_plugin_data(guild_id: int | str, plugin_name: str) -> bool:
    # Delete a plugin's JSON test data for one guild.
    try:
        guild_key = str(int(guild_id))
        name = str(plugin_name)
        with _LOCK:
            data = _read_data()
            guild_data = data["plugin_data"].get(guild_key)
            if isinstance(guild_data, dict):
                guild_data.pop(name, None)
                if not guild_data:
                    data["plugin_data"].pop(guild_key, None)
            _write_data(data)
        return True
    except Exception:
        log.exception("delete_plugin_data failed for guild %s and plugin %s", guild_id, plugin_name)
        return False


def delete_guild_data(guild_id: int | str) -> bool:
    # Delete this test bot's stored plugin list and plugin data for a departed guild.
    try:
        guild_key = str(int(guild_id))
        with _LOCK:
            data = _read_data()
            data["guilds"].pop(guild_key, None)
            data["plugin_data"].pop(guild_key, None)
            custom_events = data.get("custom_events", {})
            for collection in ("events", "emitters", "subscriptions"):
                custom_events[collection] = [
                    item for item in custom_events.get(collection, [])
                    if item.get("guild_id") != guild_key
                ]
            _write_data(data)
        return True
    except Exception:
        log.exception("delete_guild_data failed for guild %s", guild_id)
        return False


# Internal persistence bridge used by the lightweight Custom Events adapter.
# Plugin authors should use bot.custom_events rather than calling these helpers.
def _get_custom_events_state() -> dict[str, Any]:
    with _LOCK:
        data = _read_data()
        return copy.deepcopy(data["custom_events"])


def _update_custom_events_state(mutator):
    # Atomically update the Custom Events section of data.json.
    with _LOCK:
        data = _read_data()
        state = data["custom_events"]
        result = mutator(state)
        _write_data(data)
        return copy.deepcopy(result)

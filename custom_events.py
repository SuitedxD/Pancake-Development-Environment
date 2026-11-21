# Local JSON-backed Custom Events API for Pancake Development Environment.

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

import db

log = logging.getLogger("pancake-development-environment.custom-events")
EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
EventCallback = Callable[["EventEnvelope"], Any | Awaitable[Any]]
AdminNotifier = Callable[[str, str], Any | Awaitable[Any]]
_ALLOWED_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SchemaValidationError(ValueError):
    # An event schema or payload does not match the supported schema format.
    pass


def validate_schema_definition(schema: Mapping[str, Any], path: str = "$") -> None:
    # Validate a strict event payload schema before it is stored.
    if not isinstance(schema, Mapping):
        raise SchemaValidationError(f"{path}: schema must be an object")
    schema_type = schema.get("type")
    if schema_type not in _ALLOWED_TYPES:
        raise SchemaValidationError(f"{path}: unsupported or missing type {schema_type!r}")

    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise SchemaValidationError(f"{path}.enum: expected a non-empty list")

    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, Mapping):
            raise SchemaValidationError(f"{path}.properties: expected an object")
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise SchemaValidationError(f"{path}.required: expected a list of field names")
        unknown = set(required) - set(properties)
        if unknown:
            raise SchemaValidationError(f"{path}.required: undefined fields {sorted(unknown)!r}")
        if schema.get("additionalProperties", False) is not False:
            raise SchemaValidationError(f"{path}: additionalProperties must be false for strict schemas")
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise SchemaValidationError(f"{path}.properties: field names must be non-empty strings")
            validate_schema_definition(child, f"{path}.{name}")
    elif schema_type == "array":
        if "items" not in schema:
            raise SchemaValidationError(f"{path}.items: required for arrays")
        validate_schema_definition(schema["items"], f"{path}[]")

    if schema_type == "string":
        for bound in ("minLength", "maxLength"):
            value = schema.get(bound)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise SchemaValidationError(f"{path}.{bound}: expected a non-negative integer")
        if schema.get("minLength", 0) > schema.get("maxLength", float("inf")):
            raise SchemaValidationError(f"{path}: minLength cannot exceed maxLength")
    elif schema_type == "array":
        for bound in ("minItems", "maxItems"):
            value = schema.get(bound)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise SchemaValidationError(f"{path}.{bound}: expected a non-negative integer")
        if schema.get("minItems", 0) > schema.get("maxItems", float("inf")):
            raise SchemaValidationError(f"{path}: minItems cannot exceed maxItems")
    elif schema_type in {"integer", "number"}:
        for bound in ("minimum", "maximum"):
            value = schema.get(bound)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                raise SchemaValidationError(f"{path}.{bound}: expected a number")
        if schema.get("minimum", float("-inf")) > schema.get("maximum", float("inf")):
            raise SchemaValidationError(f"{path}: minimum cannot exceed maximum")


def validate_payload(payload: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    # Validate a payload recursively. Unknown object properties are rejected.
    kind = schema["type"]
    checks = {
        "object": lambda value: isinstance(value, Mapping),
        "array": lambda value: isinstance(value, list),
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda value: isinstance(value, bool),
        "null": lambda value: value is None,
    }
    if not checks[kind](payload):
        raise SchemaValidationError(f"{path}: expected {kind}")

    if "enum" in schema and not any(type(payload) is type(option) and payload == option for option in schema["enum"]):
        raise SchemaValidationError(f"{path}: value is not in enum")

    if kind == "object":
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(payload)
        extra = set(payload) - set(properties)
        if missing:
            raise SchemaValidationError(f"{path}: missing required fields {sorted(missing)!r}")
        if extra:
            raise SchemaValidationError(f"{path}: unknown fields {sorted(extra)!r}")
        for key, value in payload.items():
            validate_payload(value, properties[key], f"{path}.{key}")
    elif kind == "array":
        if len(payload) < schema.get("minItems", 0):
            raise SchemaValidationError(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(payload) > schema["maxItems"]:
            raise SchemaValidationError(f"{path}: more than maxItems")
        for index, value in enumerate(payload):
            validate_payload(value, schema["items"], f"{path}[{index}]")
    elif kind == "string":
        if len(payload) < schema.get("minLength", 0):
            raise SchemaValidationError(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(payload) > schema["maxLength"]:
            raise SchemaValidationError(f"{path}: longer than maxLength")
    elif kind in {"integer", "number"}:
        if "minimum" in schema and payload < schema["minimum"]:
            raise SchemaValidationError(f"{path}: below minimum")
        if "maximum" in schema and payload > schema["maximum"]:
            raise SchemaValidationError(f"{path}: above maximum")


@dataclass(frozen=True, slots=True)
class EventContext:
    guild_id: str
    emitter_plugin_id: str
    emitter_resource_type: str
    emitter_resource_id: str
    emitted_at: str = field(default_factory=_utc_iso)
    actor_user_id: str | None = None
    cause_event_id: str | None = None
    depth: int = 0


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    guild_id: str
    name: str
    schema_version: int
    data: Mapping[str, Any]
    context: EventContext


@dataclass(frozen=True, slots=True)
class Subscription:
    subscription_id: str
    guild_id: str
    event_name: str
    plugin_id: str
    consumer_id: str
    schema_version: int
    created_at: str


@dataclass(slots=True)
class _RuntimeSubscription:
    subscription: dict[str, Any]
    callback: EventCallback
    queue: asyncio.Queue[EventEnvelope]
    task: asyncio.Task[None] | None = None
    missing_schema_versions_notified: set[int] = field(default_factory=set)


class JsonEventStore:
    # JSON persistence adapter. Internal to PDE; use bot.custom_events in plugins.

    def _read(self, collection: str) -> list[dict[str, Any]]:
        return db._get_custom_events_state()[collection]

    def _mutate(self, callback):
        return db._update_custom_events_state(callback)

    def get_event(self, guild_id: str, event_name: str) -> dict[str, Any] | None:
        for item in self._read("events"):
            if item.get("guild_id") == guild_id and item.get("event_name") == event_name:
                return item
        return None

    def list_events(self, guild_id: str, include_grace: bool = True) -> list[dict[str, Any]]:
        allowed = {"active", "grace"} if include_grace else {"active"}
        return [item for item in self._read("events") if item.get("guild_id") == guild_id and item.get("status") in allowed]

    def register_emitter(self, *, guild_id: str, event_name: str, resource_type: str, resource_id: str,
                         plugin_id: str, schema_version: int, schema: Mapping[str, Any], created_at: str) -> None:
        event_key = (guild_id, event_name)
        emitter_key = (guild_id, event_name, resource_type, resource_id)
        version = str(schema_version)
        schema_value = deepcopy(dict(schema))

        def update(state):
            event = next((item for item in state["events"] if (item.get("guild_id"), item.get("event_name")) == event_key), None)
            if event is None:
                event = {"guild_id": guild_id, "event_name": event_name, "status": "active", "schemas": {},
                         "created_at": created_at, "grace_until": None}
                state["events"].append(event)
            existing_schema = event.setdefault("schemas", {}).get(version)
            if existing_schema is not None and existing_schema != schema_value:
                raise ValueError(f"Schema version {schema_version} for event {event_name!r} already has a different definition")
            event["schemas"][version] = schema_value
            event["status"] = "active"
            event["grace_until"] = None

            emitter = next((item for item in state["emitters"] if tuple(item.get(k) for k in
                ("guild_id", "event_name", "resource_type", "resource_id")) == emitter_key), None)
            if emitter is not None and emitter.get("plugin_id") != plugin_id:
                raise PermissionError(f"Resource {resource_type}:{resource_id} is already owned by another plugin")
            if emitter is None:
                emitter = {"guild_id": guild_id, "event_name": event_name, "resource_type": resource_type,
                           "resource_id": resource_id}
                state["emitters"].append(emitter)
            emitter.update({"plugin_id": plugin_id, "schema_version": schema_version, "created_at": created_at})

        self._mutate(update)

    def get_emitter(self, guild_id: str, event_name: str, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        key = (guild_id, event_name, resource_type, resource_id)
        return next((item for item in self._read("emitters") if tuple(item.get(k) for k in
            ("guild_id", "event_name", "resource_type", "resource_id")) == key), None)

    def list_emitters_by_resource(self, guild_id: str, resource_type: str, resource_id: str) -> list[dict[str, Any]]:
        return [item for item in self._read("emitters") if item.get("guild_id") == guild_id
                and item.get("resource_type") == resource_type and item.get("resource_id") == resource_id]

    def delete_emitter(self, guild_id: str, event_name: str, resource_type: str, resource_id: str) -> bool:
        key = (guild_id, event_name, resource_type, resource_id)
        def update(state):
            before = len(state["emitters"])
            state["emitters"] = [item for item in state["emitters"] if tuple(item.get(k) for k in
                ("guild_id", "event_name", "resource_type", "resource_id")) != key]
            return len(state["emitters"]) < before
        return bool(self._mutate(update))

    def count_emitters(self, guild_id: str, event_name: str) -> int:
        return sum(1 for item in self._read("emitters") if item.get("guild_id") == guild_id and item.get("event_name") == event_name)

    def count_resource_emitters(self, guild_id: str, plugin_id: str, resource_type: str, resource_id: str) -> int:
        return sum(1 for item in self._read("emitters") if item.get("guild_id") == guild_id and item.get("plugin_id") == plugin_id
                   and item.get("resource_type") == resource_type and item.get("resource_id") == resource_id)

    def list_emitters_by_plugin(self, guild_id: str, plugin_id: str) -> list[dict[str, Any]]:
        return [item for item in self._read("emitters") if item.get("guild_id") == guild_id and item.get("plugin_id") == plugin_id]

    def start_grace_if_unemitted(self, guild_id: str, event_name: str, grace_until: float) -> bool:
        def update(state):
            if any(item.get("guild_id") == guild_id and item.get("event_name") == event_name for item in state["emitters"]):
                return False
            event = next((item for item in state["events"] if item.get("guild_id") == guild_id and item.get("event_name") == event_name
                          and item.get("status") == "active"), None)
            if event is None:
                return False
            event["status"] = "grace"
            event["grace_until"] = grace_until
            return True
        return bool(self._mutate(update))

    def upsert_subscription(self, *, guild_id: str, event_name: str, plugin_id: str, consumer_id: str,
                            schema_version: int, created_at: str) -> dict[str, Any]:
        key = (guild_id, event_name, plugin_id, consumer_id)
        def update(state):
            item = next((item for item in state["subscriptions"] if tuple(item.get(k) for k in
                ("guild_id", "event_name", "plugin_id", "consumer_id")) == key), None)
            if item is None:
                item = {"guild_id": guild_id, "event_name": event_name, "plugin_id": plugin_id,
                        "consumer_id": consumer_id, "subscription_id": str(uuid.uuid4()),
                        "created_at": created_at, "schema_version": schema_version}
                state["subscriptions"].append(item)
            return item
        return self._mutate(update)

    def list_subscriptions(self, guild_id: str, event_name: str) -> list[dict[str, Any]]:
        return [item for item in self._read("subscriptions") if item.get("guild_id") == guild_id and item.get("event_name") == event_name]

    def delete_subscription(self, subscription_id: str) -> bool:
        def update(state):
            before = len(state["subscriptions"])
            state["subscriptions"] = [item for item in state["subscriptions"] if item.get("subscription_id") != subscription_id]
            return len(state["subscriptions"]) < before
        return bool(self._mutate(update))

    def delete_subscriptions_by_plugin(self, guild_id: str, plugin_id: str) -> None:
        def update(state):
            state["subscriptions"] = [item for item in state["subscriptions"] if not
                (item.get("guild_id") == guild_id and item.get("plugin_id") == plugin_id)]
        self._mutate(update)

    def retire_expired(self, now: float) -> list[dict[str, Any]]:
        def update(state):
            retired = []
            keep = []
            for event in state["events"]:
                if event.get("status") == "grace" and event.get("grace_until") is not None and event["grace_until"] <= now:
                    if any(item.get("guild_id") == event.get("guild_id") and item.get("event_name") == event.get("event_name")
                           for item in state["emitters"]):
                        event["status"] = "active"
                        event["grace_until"] = None
                        keep.append(event)
                        continue
                    retired.append(deepcopy(event))
                else:
                    keep.append(event)
            state["events"] = keep
            retired_keys = {(item["guild_id"], item["event_name"]) for item in retired}
            state["subscriptions"] = [item for item in state["subscriptions"] if
                (item.get("guild_id"), item.get("event_name")) not in retired_keys]
            state["emitters"] = [item for item in state["emitters"] if
                (item.get("guild_id"), item.get("event_name")) not in retired_keys]
            return retired
        return self._mutate(update)

    def delete_guild_data(self, guild_id: str) -> None:
        def update(state):
            for collection in ("events", "emitters", "subscriptions"):
                state[collection] = [item for item in state[collection] if item.get("guild_id") != guild_id]
        self._mutate(update)


class CustomEventManager:
    # Plugin-facing Custom Events manager backed by the PDE JSON database.

    def __init__(self, store: Any | None = None, *, grace_period_seconds: float = 7 * 24 * 60 * 60,
                 rate_limit_count: int = 60, rate_limit_window_seconds: float = 60.0,
                 max_chain_depth: int = 8, emission_queue_size: int = 1000,
                 per_subscription_queue_size: int = 100, max_concurrent_deliveries: int = 64,
                 lifecycle_interval_seconds: float = 300.0, admin_notifier: AdminNotifier | None = None,
                 plugin_is_installed: Callable[[str, str], Any] | None = None) -> None:
        if min(rate_limit_count, emission_queue_size, per_subscription_queue_size, max_concurrent_deliveries) < 1:
            raise ValueError("rate limits, queue sizes and delivery concurrency must be positive")
        if rate_limit_window_seconds <= 0 or lifecycle_interval_seconds <= 0 or grace_period_seconds < 0:
            raise ValueError("invalid time interval")
        if max_chain_depth < 0:
            raise ValueError("max_chain_depth cannot be negative")
        self.store = store or JsonEventStore()
        self.grace_period_seconds = grace_period_seconds
        self.rate_limit_count = rate_limit_count
        self.rate_limit_window_seconds = rate_limit_window_seconds
        self.max_chain_depth = max_chain_depth
        self.per_subscription_queue_size = per_subscription_queue_size
        self._delivery_semaphore = asyncio.Semaphore(max_concurrent_deliveries)
        self.lifecycle_interval_seconds = lifecycle_interval_seconds
        self.admin_notifier = admin_notifier
        self.plugin_is_installed = plugin_is_installed
        self._queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=emission_queue_size)
        self._runtime: dict[str, _RuntimeSubscription] = {}
        self._disabled_plugins: set[tuple[str, str]] = set()
        self._rate_windows: dict[tuple[str, str, str, str], deque[float]] = defaultdict(deque)
        self._worker: asyncio.Task[None] | None = None
        self._lifecycle_worker_task: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def is_running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    async def _db(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)

    async def _is_installed(self, guild_id: str, plugin_id: str) -> bool:
        if self.plugin_is_installed is None:
            return True
        result = self.plugin_is_installed(guild_id, plugin_id)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def start(self) -> None:
        if self._closing:
            raise RuntimeError("CustomEventManager has already been closed")
        if self.is_running:
            return
        self._worker = asyncio.create_task(self._dispatch_loop(), name="pde-custom-events-dispatch")
        self._lifecycle_worker_task = asyncio.create_task(self._lifecycle_loop(), name="pde-custom-events-lifecycle")
        log.info("PDE Custom Events manager started")

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        tasks: list[asyncio.Task[Any]] = []
        for task in (self._worker, self._lifecycle_worker_task):
            if task is not None and not task.done():
                task.cancel()
                tasks.append(task)
        for runtime in self._runtime.values():
            if runtime.task is not None and not runtime.task.done():
                runtime.task.cancel()
                tasks.append(runtime.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runtime.clear()
        self._rate_windows.clear()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        log.info("PDE Custom Events manager stopped; pending emissions were discarded")

    async def get_event(self, *, guild_id: int | str, event_name: str) -> dict[str, Any] | None:
        try:
            guild = self._id(guild_id, "guild_id")
        except (TypeError, ValueError):
            return None
        if not isinstance(event_name, str) or not EVENT_NAME_RE.fullmatch(event_name):
            return None
        return await self._db("get_event", guild, event_name)

    async def list_events(self, *, guild_id: int | str, include_grace: bool = True) -> list[dict[str, Any]]:
        return await self._db("list_events", self._id(guild_id, "guild_id"), include_grace)

    async def register_emitter(self, *, guild_id: int | str, event_name: str, resource_type: str,
                               resource_id: int | str, plugin_id: str, schema_version: int,
                               schema: Mapping[str, Any]) -> None:
        guild = self._id(guild_id, "guild_id")
        resource = self._id(resource_id, "resource_id")
        kind = self._id(resource_type, "resource_type")
        plugin = self._id(plugin_id, "plugin_id")
        if not isinstance(event_name, str) or not EVENT_NAME_RE.fullmatch(event_name):
            raise ValueError("event_name must be namespaced, e.g. 'support.ticket_created'")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        normalized = deepcopy(dict(schema))
        validate_schema_definition(normalized)
        if normalized.get("type") != "object":
            raise ValueError("event payload schemas must be objects")
        if not await self._is_installed(guild, plugin):
            raise PermissionError(f"emitter plugin {plugin!r} is not installed in guild {guild}")
        await self._db("register_emitter", guild_id=guild, event_name=event_name, resource_type=kind,
                       resource_id=resource, plugin_id=plugin, schema_version=schema_version,
                       schema=normalized, created_at=_utc_iso())
        self._disabled_plugins.discard((guild, plugin))

    async def unregister_emitter(self, *, guild_id: int | str, event_name: str,
                                 resource_type: str, resource_id: int | str) -> bool:
        guild = self._id(guild_id, "guild_id")
        resource = self._id(resource_id, "resource_id")
        kind = self._id(resource_type, "resource_type")
        emitter = await self._db("get_emitter", guild, event_name, kind, resource)
        if emitter is None:
            return False
        plugin = emitter["plugin_id"]
        removed = await self._db("delete_emitter", guild, event_name, kind, resource)
        if not removed:
            return False
        if await self._db("count_resource_emitters", guild, plugin, kind, resource) == 0:
            self._rate_windows.pop((guild, plugin, kind, resource), None)
        deadline = time.time() + self.grace_period_seconds
        if await self._db("start_grace_if_unemitted", guild, event_name, deadline):
            await self._notify_admin(guild, f"Custom event '{event_name}' has no active emitters. Its subscriptions will be removed after the grace period unless an emitter is registered again.")
        return True

    async def unregister_resource_emitters(self, *, guild_id: int | str, resource_type: str,
                                           resource_id: int | str) -> int:
        guild = self._id(guild_id, "guild_id")
        kind = self._id(resource_type, "resource_type")
        resource = self._id(resource_id, "resource_id")
        emitters = await self._db("list_emitters_by_resource", guild, kind, resource)
        removed = 0
        for emitter in emitters:
            if await self.unregister_emitter(guild_id=guild, event_name=emitter["event_name"],
                    resource_type=kind, resource_id=resource):
                removed += 1
        return removed

    async def subscribe(self, *, guild_id: int | str, event_name: str, plugin_id: str,
                        consumer_id: str, schema_version: int, callback: EventCallback) -> Subscription:
        guild = self._id(guild_id, "guild_id")
        plugin = self._id(plugin_id, "plugin_id")
        consumer = self._id(consumer_id, "consumer_id")
        if not isinstance(event_name, str) or not EVENT_NAME_RE.fullmatch(event_name):
            raise ValueError("event_name must be namespaced, e.g. 'webembed.support_requested'")
        if self._closing or not self.is_running:
            raise RuntimeError("Custom Events manager must be running before plugins subscribe")
        if not callable(callback):
            raise TypeError("callback must be callable")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        if not await self._is_installed(guild, plugin):
            raise PermissionError(f"consumer plugin {plugin!r} is not installed in guild {guild}")

        event = await self._db("get_event", guild, event_name)
        stored = await self._db("upsert_subscription", guild_id=guild, event_name=event_name,
                plugin_id=plugin, consumer_id=consumer, schema_version=schema_version, created_at=_utc_iso())
        subscription_id = stored["subscription_id"]
        previous = self._runtime.pop(subscription_id, None)
        if previous and previous.task and not previous.task.done():
            previous.task.cancel()
            await asyncio.gather(previous.task, return_exceptions=True)

        pinned_version = int(stored["schema_version"])
        self._disabled_plugins.discard((guild, plugin))
        if pinned_version != schema_version:
            await self._notify_admin(guild, f"Plugin '{plugin}' requests schema v{schema_version} for '{event_name}', but the stored subscription is pinned to v{pinned_version}. Remove and recreate the subscription to migrate it.")
        else:
            runtime = _RuntimeSubscription(
                subscription=stored,
                callback=callback,
                queue=asyncio.Queue(maxsize=self.per_subscription_queue_size),
            )
            if event is not None and str(schema_version) not in event.get("schemas", {}):
                runtime.missing_schema_versions_notified.add(schema_version)
                await self._notify_admin(guild, f"Plugin '{plugin}' subscribed to '{event_name}' schema v{schema_version}, but that schema version is not registered yet.")
            self._runtime[subscription_id] = runtime
            runtime.task = asyncio.create_task(self._subscription_loop(subscription_id, runtime),
                                                name=f"pde-event-sub:{plugin}:{consumer}")
        return Subscription(
            subscription_id=stored["subscription_id"], guild_id=stored["guild_id"],
            event_name=stored["event_name"], plugin_id=stored["plugin_id"],
            consumer_id=stored["consumer_id"], schema_version=int(stored["schema_version"]),
            created_at=stored["created_at"],
        )

    async def unsubscribe(self, subscription_id: str) -> bool:
        runtime = self._runtime.pop(subscription_id, None)
        if runtime and runtime.task and not runtime.task.done():
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)
        return bool(await self._db("delete_subscription", subscription_id))

    async def uninstall_plugin(self, *, guild_id: int | str, plugin_id: str) -> None:
        guild = self._id(guild_id, "guild_id")
        plugin = self._id(plugin_id, "plugin_id")
        self._disabled_plugins.add((guild, plugin))
        matched = [(sid, runtime) for sid, runtime in self._runtime.items()
                   if runtime.subscription["guild_id"] == guild and runtime.subscription["plugin_id"] == plugin]
        tasks = []
        for subscription_id, runtime in matched:
            self._runtime.pop(subscription_id, None)
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
                tasks.append(runtime.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._db("delete_subscriptions_by_plugin", guild, plugin)
        for emitter in await self._db("list_emitters_by_plugin", guild, plugin):
            await self.unregister_emitter(guild_id=guild, event_name=emitter["event_name"],
                resource_type=emitter["resource_type"], resource_id=emitter["resource_id"])
        log.info("Custom Events registrations removed for plugin %s in guild %s", plugin, guild)

    async def emit(self, *, guild_id: int | str, event_name: str, emitter_plugin_id: str,
                   resource_type: str, resource_id: int | str, schema_version: int,
                   data: Mapping[str, Any], actor_user_id: int | str | None = None) -> bool:
        return await self._emit(guild_id=guild_id, event_name=event_name, emitter_plugin_id=emitter_plugin_id,
            resource_type=resource_type, resource_id=resource_id, schema_version=schema_version,
            data=data, actor_user_id=actor_user_id, depth=0, cause_event_id=None)

    async def emit_derived(self, *, parent: EventEnvelope, event_name: str, emitter_plugin_id: str,
                           resource_type: str, resource_id: int | str, schema_version: int,
                           data: Mapping[str, Any]) -> bool:
        depth = parent.context.depth + 1
        if depth > self.max_chain_depth:
            log.warning("Rejected derived Custom Event at max chain depth (%s)", depth)
            return False
        return await self._emit(guild_id=parent.guild_id, event_name=event_name,
            emitter_plugin_id=emitter_plugin_id, resource_type=resource_type, resource_id=resource_id,
            schema_version=schema_version, data=data, actor_user_id=parent.context.actor_user_id,
            depth=depth, cause_event_id=parent.event_id)

    async def _emit(self, *, guild_id: int | str, event_name: str, emitter_plugin_id: str,
                    resource_type: str, resource_id: int | str, schema_version: int,
                    data: Mapping[str, Any], actor_user_id: int | str | None,
                    depth: int, cause_event_id: str | None) -> bool:
        if self._closing or not self.is_running or not isinstance(data, Mapping):
            return False
        try:
            guild = self._id(guild_id, "guild_id")
            resource = self._id(resource_id, "resource_id")
            plugin = self._id(emitter_plugin_id, "emitter_plugin_id")
            kind = self._id(resource_type, "resource_type")
        except (TypeError, ValueError):
            return False
        if not isinstance(event_name, str) or not EVENT_NAME_RE.fullmatch(event_name):
            return False
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
            return False
        if (guild, plugin) in self._disabled_plugins or not await self._is_installed(guild, plugin):
            return False

        event = await self._db("get_event", guild, event_name)
        if event is None or event.get("status") != "active":
            return False
        emitter = await self._db("get_emitter", guild, event_name, kind, resource)
        if emitter is None or emitter.get("plugin_id") != plugin:
            return False
        schema = event.get("schemas", {}).get(str(schema_version))
        if schema is None or int(emitter.get("schema_version", -1)) != schema_version:
            return False
        try:
            validate_payload(data, schema)
        except (SchemaValidationError, TypeError, ValueError):
            return False

        now = time.monotonic()
        limiter_key = (guild, plugin, kind, resource)
        window = self._rate_windows[limiter_key]
        cutoff = now - self.rate_limit_window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self.rate_limit_count or self._queue.full():
            return False
        envelope = EventEnvelope(
            event_id=str(uuid.uuid4()), guild_id=guild, name=event_name, schema_version=schema_version,
            data=deepcopy(dict(data)),
            context=EventContext(guild_id=guild, emitter_plugin_id=plugin, emitter_resource_type=kind,
                emitter_resource_id=resource, emitted_at=_utc_iso(),
                actor_user_id=str(actor_user_id) if actor_user_id is not None else None,
                cause_event_id=cause_event_id, depth=depth),
        )
        try:
            self._queue.put_nowait(envelope)
            window.append(now)
            return True
        except asyncio.QueueFull:
            return False

    async def _dispatch_loop(self) -> None:
        while True:
            envelope = await self._queue.get()
            try:
                subscriptions = await self._db("list_subscriptions", envelope.guild_id, envelope.name)
                for stored in subscriptions:
                    runtime = self._runtime.get(stored.get("subscription_id"))
                    if runtime is None:
                        continue
                    if (envelope.guild_id, stored["plugin_id"]) in self._disabled_plugins:
                        continue
                    if int(stored["schema_version"]) != envelope.schema_version:
                        continue
                    try:
                        runtime.queue.put_nowait(deepcopy(envelope))
                    except asyncio.QueueFull:
                        log.error("Subscriber queue full for %s/%s", stored["plugin_id"], stored["consumer_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Custom Events dispatcher failed for %s", envelope.name)
            finally:
                self._queue.task_done()

    async def _subscription_loop(self, subscription_id: str, runtime: _RuntimeSubscription) -> None:
        while True:
            envelope = await runtime.queue.get()
            try:
                subscription = runtime.subscription
                if (envelope.guild_id, subscription["plugin_id"]) in self._disabled_plugins:
                    continue
                if envelope.schema_version != int(subscription["schema_version"]):
                    continue
                async with self._delivery_semaphore:
                    result = runtime.callback(envelope)
                    if inspect.isawaitable(result):
                        await result
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Custom Events subscriber failed: %s", subscription_id)
            finally:
                runtime.queue.task_done()

    async def _lifecycle_loop(self) -> None:
        while True:
            await asyncio.sleep(self.lifecycle_interval_seconds)
            try:
                expired = await self.retire_expired()
                for event in expired:
                    await self._notify_admin(event["guild_id"], f"Custom event '{event['event_name']}' was retired after its grace period.")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Custom Events lifecycle maintenance failed")

    async def retire_expired(self, *, now: float | None = None) -> list[dict[str, Any]]:
        retired = await self._db("retire_expired", time.time() if now is None else now)
        retired_keys = {(item["guild_id"], item["event_name"]) for item in retired}
        for subscription_id, runtime in list(self._runtime.items()):
            sub = runtime.subscription
            if (sub["guild_id"], sub["event_name"]) in retired_keys:
                self._runtime.pop(subscription_id, None)
                if runtime.task and not runtime.task.done():
                    runtime.task.cancel()
                    await asyncio.gather(runtime.task, return_exceptions=True)
        return retired

    async def delete_guild_data(self, guild_id: int | str) -> None:
        guild = self._id(guild_id, "guild_id")
        for subscription_id, runtime in list(self._runtime.items()):
            if runtime.subscription["guild_id"] == guild:
                self._runtime.pop(subscription_id, None)
                if runtime.task and not runtime.task.done():
                    runtime.task.cancel()
                    await asyncio.gather(runtime.task, return_exceptions=True)
        await self._db("delete_guild_data", guild)
        self._disabled_plugins = {item for item in self._disabled_plugins if item[0] != guild}

    async def _notify_admin(self, guild_id: str, message: str) -> None:
        if self.admin_notifier is None:
            log.warning("Custom Events notice for guild %s: %s", guild_id, message)
            return
        try:
            result = self.admin_notifier(guild_id, message)
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("Could not send Custom Events notice for guild %s", guild_id)

    @staticmethod
    def _id(value: Any, name: str) -> str:
        if isinstance(value, bool) or value is None:
            raise ValueError(f"{name} must be a non-empty string or integer ID")
        result = str(value).strip()
        if not result:
            raise ValueError(f"{name} must not be empty")
        return result


__all__ = [
    "CustomEventManager", "EventContext", "EventEnvelope", "Subscription",
    "SchemaValidationError", "validate_payload", "validate_schema_definition",
]

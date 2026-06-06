from __future__ import annotations

import asyncio
import contextvars
import inspect
import os
import copy
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Iterator, Literal, Optional


LaneName = Literal["fast", "io", "compute", "plan", "subagent"]
ResourceName = Literal["cpu", "io", "llm", "unsafe", "wasm"]
LANE_NAMES: tuple[LaneName, ...] = ("fast", "io", "compute", "plan", "subagent")
RESOURCE_NAMES: tuple[ResourceName, ...] = ("cpu", "io", "llm", "unsafe", "wasm")


@dataclass(frozen=True)
class LaneConfig:
    permits: int
    budget_ops: int
    timeout_ms: int


@dataclass(frozen=True)
class ResourceConfig:
    permits: int
    timeout_ms: int


DEFAULT_LANE_CONFIGS: dict[LaneName, LaneConfig] = {
    "fast": LaneConfig(permits=8, budget_ops=200, timeout_ms=3_000),
    "io": LaneConfig(permits=4, budget_ops=40, timeout_ms=30_000),
    "compute": LaneConfig(permits=2, budget_ops=30, timeout_ms=15_000),
    "plan": LaneConfig(permits=1, budget_ops=40, timeout_ms=90_000),
    "subagent": LaneConfig(permits=2, budget_ops=8, timeout_ms=60_000),
}

DEFAULT_RESOURCE_FOR_LANE: dict[LaneName, ResourceName] = {
    "fast": "cpu",
    "io": "io",
    "compute": "cpu",
    "plan": "llm",
    "subagent": "io",
}

DEFAULT_RESOURCE_CONFIGS: dict[ResourceName, ResourceConfig] = {
    "cpu": ResourceConfig(permits=4, timeout_ms=15_000),
    "io": ResourceConfig(permits=8, timeout_ms=60_000),
    "llm": ResourceConfig(permits=4, timeout_ms=90_000),
    "unsafe": ResourceConfig(permits=2, timeout_ms=60_000),
    "wasm": ResourceConfig(permits=8, timeout_ms=10_000),
}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except Exception:
        return max(minimum, int(default))


def load_lane_configs() -> dict[LaneName, LaneConfig]:
    configs: dict[LaneName, LaneConfig] = {}
    for lane, default in DEFAULT_LANE_CONFIGS.items():
        prefix = f"LONGCAT_LANE_{lane.upper()}"
        configs[lane] = LaneConfig(
            permits=_env_int(f"{prefix}_PERMITS", default.permits),
            budget_ops=_env_int(f"{prefix}_OPS", default.budget_ops),
            timeout_ms=_env_int(f"{prefix}_TIMEOUT_MS", default.timeout_ms, minimum=100),
        )
    return configs


def load_resource_configs() -> dict[ResourceName, ResourceConfig]:
    configs: dict[ResourceName, ResourceConfig] = {}
    for resource, default in DEFAULT_RESOURCE_CONFIGS.items():
        prefix = f"LONGCAT_RESOURCE_{resource.upper()}"
        configs[resource] = ResourceConfig(
            permits=_env_int(f"{prefix}_PERMITS", default.permits),
            timeout_ms=_env_int(f"{prefix}_TIMEOUT_MS", default.timeout_ms, minimum=100),
        )
    return configs


def derive_resource_configs(
    lane_configs: dict[LaneName, LaneConfig],
    defaults: Optional[dict[ResourceName, ResourceConfig]] = None,
) -> dict[ResourceName, ResourceConfig]:
    defaults = defaults or load_resource_configs()
    derived = dict(defaults)
    grouped: dict[ResourceName, list[LaneConfig]] = {name: [] for name in RESOURCE_NAMES}
    for lane, lane_config in (lane_configs or {}).items():
        resource = DEFAULT_RESOURCE_FOR_LANE.get(lane)
        if resource:
            grouped[resource].append(lane_config)
    for resource, lane_group in grouped.items():
        if not lane_group:
            continue
        default = defaults.get(resource, DEFAULT_RESOURCE_CONFIGS[resource])
        derived[resource] = ResourceConfig(
            permits=max(1, int(default.permits), max(int(item.permits) for item in lane_group)),
            timeout_ms=max(100, int(default.timeout_ms), max(int(item.timeout_ms) for item in lane_group)),
        )
    return derived


class CancellationToken:
    """Small cooperative cancellation token with parent-to-child propagation."""

    def __init__(self, parent: Optional["CancellationToken"] = None) -> None:
        self._parent = parent
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._children: list[CancellationToken] = []
        self.reason = ""
        if parent is not None:
            parent._add_child(self)

    def _add_child(self, child: "CancellationToken") -> None:
        with self._lock:
            self._children.append(child)
        if self.cancelled:
            child.cancel(self.reason)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(self._parent and self._parent.cancelled)

    def throw_if_cancelled(self) -> None:
        if self.cancelled:
            raise RuntimeError(self.reason or "operation cancelled")

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason or "cancelled"
            self._event.set()
            children = list(self._children)
        for child in children:
            child.cancel(self.reason)


@dataclass
class LaneBudget:
    limit: int
    used: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def consume(self, cost: int = 1) -> bool:
        cost = max(0, int(cost or 0))
        with self.lock:
            if self.used + cost > self.limit:
                return False
            self.used += cost
            return True

    def refund(self, cost: int = 1) -> None:
        cost = max(0, int(cost or 0))
        with self.lock:
            self.used = max(0, self.used - cost)

    @property
    def remaining(self) -> int:
        with self.lock:
            return max(0, self.limit - self.used)


@dataclass
class OpNode:
    op_id: str
    name: str
    lane: LaneName
    parent_op_id: str = ""
    trajectory_id: str = ""
    cascade_id: str = ""
    status: str = "queued"
    started_at_ms: int = 0
    completed_at_ms: int = 0
    error: str = ""
    children: list[str] = field(default_factory=list)


@dataclass
class OpEvent:
    event_id: str
    seq: int
    session_id: str
    run_id: str
    op_id: str
    parent_op_id: str
    trajectory_id: str
    cascade_id: str
    lane: str
    type: str
    status: str
    ts_ms: int
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "seq": self.seq,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "op_id": self.op_id,
            "parent_op_id": self.parent_op_id,
            "trajectory_id": self.trajectory_id,
            "cascade_id": self.cascade_id,
            "lane": self.lane,
            "type": self.type,
            "status": self.status,
            "ts_ms": self.ts_ms,
            "payload": self.payload,
            "error": self.error,
        }


class OpEventBus:
    def __init__(
        self,
        session_id: str,
        run_id: str,
        sink: Optional[Callable[[OpEvent], None]] = None,
    ) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self._sink = sink
        self._seq = 0
        self._lock = threading.Lock()
        self._listeners: list[Callable[[OpEvent], None]] = []
        self._listener_keys: set[str] = set()

    def subscribe(self, listener: Callable[[OpEvent], None], key: str = "") -> None:
        with self._lock:
            if key:
                if key in self._listener_keys:
                    return
                self._listener_keys.add(key)
            self._listeners.append(listener)

    def emit(
        self,
        event_type: str,
        status: str,
        *,
        op_id: str = "",
        parent_op_id: str = "",
        trajectory_id: str = "",
        cascade_id: str = "",
        lane: str = "",
        payload: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> OpEvent:
        with self._lock:
            self._seq += 1
            seq = self._seq
            listeners = list(self._listeners)
        event = OpEvent(
            event_id=f"evt_{uuid.uuid4().hex[:16]}",
            seq=seq,
            session_id=self.session_id,
            run_id=self.run_id,
            op_id=op_id,
            parent_op_id=parent_op_id,
            trajectory_id=trajectory_id,
            cascade_id=cascade_id,
            lane=lane,
            type=event_type,
            status=status,
            ts_ms=int(time.time() * 1000),
            payload=payload or {},
            error=error or "",
        )
        if self._sink is not None:
            try:
                self._sink(event)
            except Exception:
                pass
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                pass
        return event


class ResourceController:
    def __init__(self, name: ResourceName, config: ResourceConfig) -> None:
        self.name = name
        self.config = config
        self._sem = threading.BoundedSemaphore(config.permits)
        self._lock = threading.Lock()
        self._active = 0

    def acquire(self, timeout_ms: int) -> bool:
        return self._sem.acquire(timeout=max(0.001, timeout_ms / 1000.0))

    async def acquire_async(self, timeout_ms: int) -> bool:
        return await asyncio.to_thread(
            self._sem.acquire,
            timeout=max(0.001, timeout_ms / 1000.0),
        )

    def mark_active(self, delta: int) -> int:
        with self._lock:
            self._active = max(0, self._active + int(delta))
            return self._active

    def release(self) -> int:
        active = self.mark_active(-1)
        self._sem.release()
        return active

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self._active
        return {
            "permits": self.config.permits,
            "active": active,
            "timeoutMs": self.config.timeout_ms,
        }


class LaneController:
    def __init__(
        self,
        name: LaneName,
        config: LaneConfig,
        resource_name: ResourceName,
        resource: ResourceController,
        event_bus: OpEventBus,
    ) -> None:
        self.name = name
        self.config = config
        self.resource_name = resource_name
        self.resource = resource
        self.budget = LaneBudget(config.budget_ops)
        self._event_bus = event_bus

    @contextmanager
    def lease(
        self,
        *,
        op_id: str,
        parent_op_id: str = "",
        trajectory_id: str = "",
        cascade_id: str = "",
        budget_cost: int = 1,
        token: Optional[CancellationToken] = None,
    ) -> Iterator[None]:
        token = token or CancellationToken()
        token.throw_if_cancelled()
        self._event_bus.emit(
            "lane.wait",
            "waiting",
            op_id=op_id,
            parent_op_id=parent_op_id,
            trajectory_id=trajectory_id,
            cascade_id=cascade_id,
            lane=self.name,
            payload={
                "timeout_ms": self.config.timeout_ms,
                "budget_cost": budget_cost,
                "resource": self.resource_name,
                "resource_timeout_ms": self.resource.config.timeout_ms,
            },
        )
        acquired = self.resource.acquire(min(self.config.timeout_ms, self.resource.config.timeout_ms))
        if not acquired:
            self._event_bus.emit(
                "lane.timeout",
                "timeout",
                op_id=op_id,
                parent_op_id=parent_op_id,
                trajectory_id=trajectory_id,
                cascade_id=cascade_id,
                lane=self.name,
                error=f"lane {self.name} wait timeout",
            )
            raise TimeoutError(f"lane {self.name} wait timeout")
        consumed = False
        try:
            token.throw_if_cancelled()
            if not self.budget.consume(budget_cost):
                self._event_bus.emit(
                    "lane.budget_exhausted",
                    "failed",
                    op_id=op_id,
                    parent_op_id=parent_op_id,
                    trajectory_id=trajectory_id,
                    cascade_id=cascade_id,
                    lane=self.name,
                    payload={"budget_limit": self.budget.limit, "budget_used": self.budget.used},
                    error=f"lane {self.name} budget exhausted",
                )
                raise RuntimeError(f"lane {self.name} budget exhausted")
            consumed = True
            active = self.resource.mark_active(1)
            self._event_bus.emit(
                "lane.acquired",
                "running",
                op_id=op_id,
                parent_op_id=parent_op_id,
                trajectory_id=trajectory_id,
                cascade_id=cascade_id,
                lane=self.name,
                payload={
                    "active": active,
                    "remaining_budget": self.budget.remaining,
                    "resource": self.resource_name,
                },
            )
            yield
        except Exception:
            raise
        finally:
            if consumed:
                active = self.resource.release()
                self._event_bus.emit(
                    "lane.released",
                    "released",
                    op_id=op_id,
                    parent_op_id=parent_op_id,
                    trajectory_id=trajectory_id,
                    cascade_id=cascade_id,
                    lane=self.name,
                    payload={
                        "active": active,
                        "remaining_budget": self.budget.remaining,
                        "resource": self.resource_name,
                    },
                )
            elif acquired:
                self.resource._sem.release()

    @asynccontextmanager
    async def lease_async(
        self,
        *,
        op_id: str,
        parent_op_id: str = "",
        trajectory_id: str = "",
        cascade_id: str = "",
        budget_cost: int = 1,
        token: Optional[CancellationToken] = None,
    ) -> AsyncIterator[None]:
        token = token or CancellationToken()
        token.throw_if_cancelled()
        self._event_bus.emit(
            "lane.wait",
            "waiting",
            op_id=op_id,
            parent_op_id=parent_op_id,
            trajectory_id=trajectory_id,
            cascade_id=cascade_id,
            lane=self.name,
            payload={
                "timeout_ms": self.config.timeout_ms,
                "budget_cost": budget_cost,
                "resource": self.resource_name,
                "resource_timeout_ms": self.resource.config.timeout_ms,
            },
        )
        acquired = await self.resource.acquire_async(min(self.config.timeout_ms, self.resource.config.timeout_ms))
        if not acquired:
            self._event_bus.emit(
                "lane.timeout",
                "timeout",
                op_id=op_id,
                parent_op_id=parent_op_id,
                trajectory_id=trajectory_id,
                cascade_id=cascade_id,
                lane=self.name,
                error=f"lane {self.name} wait timeout",
            )
            raise TimeoutError(f"lane {self.name} wait timeout")
        consumed = False
        try:
            token.throw_if_cancelled()
            if not self.budget.consume(budget_cost):
                self._event_bus.emit(
                    "lane.budget_exhausted",
                    "failed",
                    op_id=op_id,
                    parent_op_id=parent_op_id,
                    trajectory_id=trajectory_id,
                    cascade_id=cascade_id,
                    lane=self.name,
                    payload={"budget_limit": self.budget.limit, "budget_used": self.budget.used},
                    error=f"lane {self.name} budget exhausted",
                )
                raise RuntimeError(f"lane {self.name} budget exhausted")
            consumed = True
            active = self.resource.mark_active(1)
            self._event_bus.emit(
                "lane.acquired",
                "running",
                op_id=op_id,
                parent_op_id=parent_op_id,
                trajectory_id=trajectory_id,
                cascade_id=cascade_id,
                lane=self.name,
                payload={
                    "active": active,
                    "remaining_budget": self.budget.remaining,
                    "resource": self.resource_name,
                },
            )
            yield
        finally:
            if consumed:
                active = self.resource.release()
                self._event_bus.emit(
                    "lane.released",
                    "released",
                    op_id=op_id,
                    parent_op_id=parent_op_id,
                    trajectory_id=trajectory_id,
                    cascade_id=cascade_id,
                    lane=self.name,
                    payload={
                        "active": active,
                        "remaining_budget": self.budget.remaining,
                        "resource": self.resource_name,
                    },
                )
            elif acquired:
                self.resource._sem.release()

    def status(self) -> dict[str, Any]:
        resource_status = self.resource.status()
        return {
            "permits": resource_status["permits"],
            "active": resource_status["active"],
            "budgetUsed": self.budget.used,
            "budgetLimit": self.budget.limit,
            "budgetRemaining": self.budget.remaining,
            "timeoutMs": self.config.timeout_ms,
            "resource": self.resource_name,
            "resourceTimeoutMs": resource_status["timeoutMs"],
        }


class ForkedContext(dict):
    """Isolated child context with lazy copy-on-write fields."""

    DEFAULT_READ_KEYS = {
        "user",
        "session_id",
        "latest_user_text",
        "recent_messages",
        "client_location",
        "client_location_name",
        "client_location_source",
        "client_location_error",
        "system_time",
        "client_timezone",
        "longcat_key",
        "personalization",
        "agent_plan",
        "card_artifacts",
        "card_artifact_order",
        "tool_cache",
        "tool_observation_count",
        "tool_observation_history",
        "agent_teams",
        "agent_mailbox",
        "allow_test_tools",
        "merchant_state",
    }
    MERGE_KEYS = {
        "card_artifacts",
        "card_artifact_order",
        "tool_cache",
        "tool_observation_count",
        "tool_observation_history",
        "agent_teams",
        "agent_mailbox",
        "merchant_state",
    }

    def __init__(self, parent: dict[str, Any], read_keys: Optional[Iterable[str]] = None) -> None:
        keys = set(read_keys or self.DEFAULT_READ_KEYS)
        values: dict[str, Any] = {}
        self._base_values: dict[str, Any] = {}
        self._materialized_keys: set[str] = set()
        self._dirty_keys: set[str] = set()
        for key in keys:
            if key not in parent:
                continue
            value = parent.get(key)
            values[key] = value
            self._base_values[key] = value
        super().__init__(values)
        self.parent_context = parent
        self["_parent_session_id"] = parent.get("session_id") if isinstance(parent, dict) else ""

    @staticmethod
    def _needs_materialize(value: Any) -> bool:
        return isinstance(value, (dict, list, set))

    def _materialize_key(self, key: str) -> None:
        if key in self._materialized_keys or key not in self:
            return
        value = dict.__getitem__(self, key)
        if not self._needs_materialize(value):
            return
        try:
            value = copy.deepcopy(value)
        except Exception:
            if isinstance(value, dict):
                value = dict(value)
            elif isinstance(value, list):
                value = list(value)
            elif isinstance(value, set):
                value = set(value)
        dict.__setitem__(self, key, value)
        self._materialized_keys.add(key)
        self._dirty_keys.add(key)

    def __getitem__(self, key: str) -> Any:
        self._materialize_key(key)
        return dict.__getitem__(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self:
            self._materialize_key(key)
            return dict.__getitem__(self, key)
        return default

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key in self:
            return self[key]
        self._dirty_keys.add(str(key))
        return dict.setdefault(self, key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        self._dirty_keys.add(str(key))
        dict.__setitem__(self, key, value)

    def merge_back(self, keys: Optional[Iterable[str]] = None) -> None:
        if not isinstance(self.parent_context, dict):
            return
        merge_keys = set(keys or self.MERGE_KEYS) & set(self._dirty_keys)
        lock = self.parent_context.get("_runtime_lock")
        if lock is None or not hasattr(lock, "acquire"):
            lock = threading.RLock()
            self.parent_context["_runtime_lock"] = lock
        with lock:
            for key in merge_keys:
                if key not in self:
                    continue
                if key == "tool_observation_count":
                    current = int(self.parent_context.get(key) or 0)
                    base = int(self._base_values.get(key) or 0)
                    value = int(dict.get(self, key) or 0)
                    self.parent_context[key] = current + max(0, value - base)
                elif isinstance(dict.get(self, key), dict) and isinstance(self.parent_context.get(key), dict):
                    self.parent_context.setdefault(key, {}).update(self[key])
                elif isinstance(dict.get(self, key), list) and isinstance(self.parent_context.get(key), list):
                    existing = self.parent_context.setdefault(key, [])
                    for item in self[key]:
                        if item not in existing:
                            existing.append(item)
                else:
                    self.parent_context[key] = self[key]


class ResultCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        with self._lock:
            return self._values.get(key)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._values[key] = value

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"entries": len(self._values)}


class AgentTaskStore:
    """Central AppState-style store for LocalAgentTask lifecycle and output."""

    TERMINAL_STATUSES = {"completed", "failed", "killed"}

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}

    def create(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = str(task.get("task_id") or "")
        if not task_id:
            raise ValueError("agent task requires task_id")
        with self._lock:
            self._tasks[task_id] = task
            return task

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._tasks.get(str(task_id or ""))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._tasks.values())

    def update(self, task_id: str, **updates: Any) -> tuple[dict[str, Any], Optional[threading.Event]]:
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            if not isinstance(task, dict):
                return {}, None
            task.update(updates)
            if task.get("started_ms") and task.get("completed_ms"):
                task["duration_ms"] = max(
                    0,
                    int(task.get("completed_ms") or 0) - int(task.get("started_ms") or 0),
                )
            event = task.get("_completion_event")
            return task, event if hasattr(event, "set") else None

    def append_output(self, task_id: str, content: str) -> dict[str, Any]:
        text = str(content or "")
        if not text:
            return {}
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            if not isinstance(task, dict):
                return {}
            old_offset = len(str(task.get("output") or ""))
            task["output"] = str(task.get("output") or "") + text
            task["output_offset"] = len(task["output"])
            task.setdefault("output_events", []).append({
                "offset": old_offset,
                "content": text,
                "ts_ms": int(time.time() * 1000),
            })
            task["output_events"] = task["output_events"][-200:]
            return {
                "taskId": task.get("task_id"),
                "offset": old_offset,
                "nextOffset": task.get("output_offset"),
                "content": text,
                "status": task.get("status") or "running",
                "name": task.get("name") or "",
            }

    def set_runtime_refs(self, task_id: str, **refs: Any) -> None:
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            if isinstance(task, dict):
                task.update(refs)

    def clear_runtime_refs(self, task_id: str, *keys: str) -> None:
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            if not isinstance(task, dict):
                return
            for key in keys:
                task.pop(key, None)

    def status(self) -> dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = {}
            ids = []
            for task_id, task in self._tasks.items():
                ids.append(task_id)
                status = str(task.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
            return {
                "total": len(self._tasks),
                "counts": counts,
                "taskIds": ids[:50],
            }


@dataclass
class RuntimeState:
    session_id: str
    pending_tool_calls: dict[str, Any] = field(default_factory=dict)
    loaded_tool_names: set[str] = field(default_factory=set)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    mailbox: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    policy_approvals: dict[str, Any] = field(default_factory=dict)
    checkpoints: dict[str, Any] = field(default_factory=dict)
    sandbox_workers: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "pendingToolCalls": len(self.pending_tool_calls or {}),
            "loadedToolNames": sorted(self.loaded_tool_names),
            "notifications": len(self.notifications or []),
            "mailboxes": len(self.mailbox or {}),
            "policyApprovals": len(self.policy_approvals or {}),
            "checkpoints": len(self.checkpoints or {}),
            "sandboxWorkers": len(self.sandbox_workers or {}),
        }


class SessionRuntime:
    def __init__(
        self,
        session_id: str,
        *,
        run_id: Optional[str] = None,
        event_sink: Optional[Callable[[OpEvent], None]] = None,
        lane_configs: Optional[dict[LaneName, LaneConfig]] = None,
        resource_configs: Optional[dict[ResourceName, ResourceConfig]] = None,
        parent_token: Optional[CancellationToken] = None,
    ) -> None:
        self.session_id = session_id or f"session_{uuid.uuid4().hex[:12]}"
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        self.started_at_ms = int(time.time() * 1000)
        self.token = CancellationToken(parent_token)
        self.event_bus = OpEventBus(self.session_id, self.run_id, sink=event_sink)
        configs = lane_configs or load_lane_configs()
        if resource_configs is None:
            resource_configs = derive_resource_configs(configs)
        self.resources = {
            name: ResourceController(name, resource_configs[name])
            for name in RESOURCE_NAMES
        }
        self.lanes = {
            name: LaneController(
                name,
                configs[name],
                DEFAULT_RESOURCE_FOR_LANE.get(name, "cpu"),
                self.resources[DEFAULT_RESOURCE_FOR_LANE.get(name, "cpu")],
                self.event_bus,
            )
            for name in LANE_NAMES
        }
        self.result_cache = ResultCache()
        self.state = RuntimeState(self.session_id)
        self._lock = threading.RLock()
        self._nodes: dict[str, OpNode] = {}
        self._local = threading.local()
        self._current_op_var = contextvars.ContextVar(f"longcat_current_op:{self.run_id}", default="")

    def child_token(self) -> CancellationToken:
        return CancellationToken(self.token)

    def new_subagent_budget(self) -> LaneBudget:
        return LaneBudget(self.lanes["subagent"].config.budget_ops)

    def cancel(self, reason: str = "session cancelled") -> None:
        self.token.cancel(reason)
        self.event_bus.emit("session.cancelled", "cancelled", payload={"reason": reason})

    def _new_op_id(self, name: str) -> str:
        normalized = "".join(ch if ch.isalnum() else "_" for ch in str(name or "op"))[:32].strip("_") or "op"
        return f"op_{normalized}_{uuid.uuid4().hex[:10]}"

    def create_node(
        self,
        name: str,
        lane: LaneName,
        *,
        parent_op_id: str = "",
        trajectory_id: str = "",
        cascade_id: str = "",
    ) -> OpNode:
        op_id = self._new_op_id(name)
        node = OpNode(
            op_id=op_id,
            name=name or op_id,
            lane=lane,
            parent_op_id=parent_op_id or "",
            trajectory_id=trajectory_id or op_id,
            cascade_id=cascade_id or self.run_id,
        )
        with self._lock:
            self._nodes[op_id] = node
            if parent_op_id and parent_op_id in self._nodes:
                self._nodes[parent_op_id].children.append(op_id)
        self.event_bus.emit(
            "op.queued",
            "queued",
            op_id=node.op_id,
            parent_op_id=node.parent_op_id,
            trajectory_id=node.trajectory_id,
            cascade_id=node.cascade_id,
            lane=node.lane,
            payload={"name": node.name},
        )
        return node

    def run_op(
        self,
        lane: LaneName,
        name: str,
        fn: Callable[[], Any],
        *,
        parent_op_id: str = "",
        trajectory_id: str = "",
        cascade_id: str = "",
        budget_cost: int = 1,
        cache_key: str = "",
        token: Optional[CancellationToken] = None,
    ) -> Any:
        if lane not in self.lanes:
            lane = "compute"
        token = token or self.token
        token.throw_if_cancelled()
        node = self.create_node(
            name,
            lane,
            parent_op_id=parent_op_id,
            trajectory_id=trajectory_id,
            cascade_id=cascade_id,
        )
        if cache_key:
            cached = self.result_cache.get(cache_key)
            if cached is not None:
                node.status = "completed"
                self.event_bus.emit(
                    "op.cache_hit",
                    "completed",
                    op_id=node.op_id,
                    parent_op_id=node.parent_op_id,
                    trajectory_id=node.trajectory_id,
                    cascade_id=node.cascade_id,
                    lane=node.lane,
                    payload={"name": node.name},
                )
                return cached
        try:
            with self.lanes[lane].lease(
                op_id=node.op_id,
                parent_op_id=node.parent_op_id,
                trajectory_id=node.trajectory_id,
                cascade_id=node.cascade_id,
                budget_cost=budget_cost,
                token=token,
            ):
                node.status = "running"
                node.started_at_ms = int(time.time() * 1000)
                self.event_bus.emit(
                    "op.started",
                    "running",
                    op_id=node.op_id,
                    parent_op_id=node.parent_op_id,
                    trajectory_id=node.trajectory_id,
                    cascade_id=node.cascade_id,
                    lane=node.lane,
                    payload={"name": node.name},
                )
                token.throw_if_cancelled()
                previous_op_id = getattr(self._local, "current_op_id", "")
                self._local.current_op_id = node.op_id
                op_token = self._current_op_var.set(node.op_id)
                try:
                    result = fn()
                finally:
                    self._current_op_var.reset(op_token)
                    self._local.current_op_id = previous_op_id
                token.throw_if_cancelled()
                if cache_key:
                    self.result_cache.set(cache_key, result)
                node.status = "completed"
                node.completed_at_ms = int(time.time() * 1000)
                self.event_bus.emit(
                    "op.completed",
                    "completed",
                    op_id=node.op_id,
                    parent_op_id=node.parent_op_id,
                    trajectory_id=node.trajectory_id,
                    cascade_id=node.cascade_id,
                    lane=node.lane,
                    payload={"name": node.name, "duration_ms": node.completed_at_ms - node.started_at_ms},
                )
                return result
        except Exception as exc:
            node.status = "cancelled" if token.cancelled else "failed"
            node.error = str(exc)
            node.completed_at_ms = int(time.time() * 1000)
            self.event_bus.emit(
                "op.cancelled" if token.cancelled else "op.failed",
                node.status,
                op_id=node.op_id,
                parent_op_id=node.parent_op_id,
                trajectory_id=node.trajectory_id,
                cascade_id=node.cascade_id,
                lane=node.lane,
                payload={"name": node.name},
                error=str(exc),
            )
            raise

    async def run_op_async(
        self,
        lane: LaneName,
        name: str,
        fn: Callable[[], Any],
        *,
        parent_op_id: str = "",
        trajectory_id: str = "",
        cascade_id: str = "",
        budget_cost: int = 1,
        cache_key: str = "",
        token: Optional[CancellationToken] = None,
    ) -> Any:
        if lane not in self.lanes:
            lane = "compute"
        token = token or self.token
        token.throw_if_cancelled()
        node = self.create_node(
            name,
            lane,
            parent_op_id=parent_op_id,
            trajectory_id=trajectory_id,
            cascade_id=cascade_id,
        )
        if cache_key:
            cached = self.result_cache.get(cache_key)
            if cached is not None:
                node.status = "completed"
                self.event_bus.emit(
                    "op.cache_hit",
                    "completed",
                    op_id=node.op_id,
                    parent_op_id=node.parent_op_id,
                    trajectory_id=node.trajectory_id,
                    cascade_id=node.cascade_id,
                    lane=node.lane,
                    payload={"name": node.name},
                )
                return cached
        try:
            async with self.lanes[lane].lease_async(
                op_id=node.op_id,
                parent_op_id=node.parent_op_id,
                trajectory_id=node.trajectory_id,
                cascade_id=node.cascade_id,
                budget_cost=budget_cost,
                token=token,
            ):
                node.status = "running"
                node.started_at_ms = int(time.time() * 1000)
                self.event_bus.emit(
                    "op.started",
                    "running",
                    op_id=node.op_id,
                    parent_op_id=node.parent_op_id,
                    trajectory_id=node.trajectory_id,
                    cascade_id=node.cascade_id,
                    lane=node.lane,
                    payload={"name": node.name},
                )
                token.throw_if_cancelled()
                previous_op_id = getattr(self._local, "current_op_id", "")
                self._local.current_op_id = node.op_id
                op_token = self._current_op_var.set(node.op_id)
                try:
                    result = fn()
                    if inspect.isawaitable(result):
                        result = await result
                finally:
                    self._current_op_var.reset(op_token)
                    self._local.current_op_id = previous_op_id
                token.throw_if_cancelled()
                if cache_key:
                    self.result_cache.set(cache_key, result)
                node.status = "completed"
                node.completed_at_ms = int(time.time() * 1000)
                self.event_bus.emit(
                    "op.completed",
                    "completed",
                    op_id=node.op_id,
                    parent_op_id=node.parent_op_id,
                    trajectory_id=node.trajectory_id,
                    cascade_id=node.cascade_id,
                    lane=node.lane,
                    payload={"name": node.name, "duration_ms": node.completed_at_ms - node.started_at_ms},
                )
                return result
        except asyncio.CancelledError as exc:
            node.status = "cancelled"
            node.error = str(exc)
            node.completed_at_ms = int(time.time() * 1000)
            self.event_bus.emit(
                "op.cancelled",
                node.status,
                op_id=node.op_id,
                parent_op_id=node.parent_op_id,
                trajectory_id=node.trajectory_id,
                cascade_id=node.cascade_id,
                lane=node.lane,
                payload={"name": node.name},
                error=str(exc),
            )
            raise
        except Exception as exc:
            node.status = "cancelled" if token.cancelled else "failed"
            node.error = str(exc)
            node.completed_at_ms = int(time.time() * 1000)
            self.event_bus.emit(
                "op.cancelled" if token.cancelled else "op.failed",
                node.status,
                op_id=node.op_id,
                parent_op_id=node.parent_op_id,
                trajectory_id=node.trajectory_id,
                cascade_id=node.cascade_id,
                lane=node.lane,
                payload={"name": node.name},
                error=str(exc),
            )
            raise

    def current_op_id(self) -> str:
        return str(self._current_op_var.get("") or getattr(self._local, "current_op_id", "") or "")

    def fork_subagent_context(self, parent_context: dict[str, Any]) -> ForkedContext:
        child = ForkedContext(parent_context)
        child["_session_runtime"] = self
        child["_cancellation_token"] = self.child_token()
        child["_subagent_budget"] = self.new_subagent_budget()
        return child

    def status(self) -> dict[str, Any]:
        with self._lock:
            node_counts: dict[str, int] = {}
            for node in self._nodes.values():
                node_counts[node.status] = node_counts.get(node.status, 0) + 1
        return {
            "sessionId": self.session_id,
            "runId": self.run_id,
            "startedAtMs": self.started_at_ms,
            "cancelled": self.token.cancelled,
            "lanes": {name: lane.status() for name, lane in self.lanes.items()},
            "resources": {name: resource.status() for name, resource in self.resources.items()},
            "nodes": node_counts,
            "cache": self.result_cache.status(),
            "state": self.state.snapshot(),
        }


class AgentRuntime:
    """Process-local registry for active session runtimes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, Any] = {}
        self._sessions: Dict[str, SessionRuntime] = {}
        self.agent_tasks = AgentTaskStore()

    def register(self, session_id: str, task: Any) -> None:
        with self._lock:
            self._tasks[session_id] = task

    def register_session(self, runtime: SessionRuntime, task: Any = None) -> None:
        with self._lock:
            self._sessions[runtime.session_id] = runtime
            if task is not None:
                self._tasks[runtime.session_id] = task

    def get_session(self, session_id: str) -> Optional[SessionRuntime]:
        with self._lock:
            return self._sessions.get(session_id)

    def discard(self, session_id: str) -> None:
        with self._lock:
            self._tasks.pop(session_id, None)
            self._sessions.pop(session_id, None)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            sessions = list(self._sessions.values())
            task_ids_all = list(self._tasks.keys())
            task_ids = task_ids_all[:20]
            session_ids = list(self._sessions.keys())[:20]
        return {
            "mode": "asgi-in-process-session-lanes",
            "activeTasks": len(task_ids_all),
            "activeSessions": len(sessions),
            "tasks": task_ids,
            "sessions": session_ids,
            "sessionDetails": [session.status() for session in sessions[:20]],
            "agentTasks": self.agent_tasks.status(),
        }


agent_runtime = AgentRuntime()

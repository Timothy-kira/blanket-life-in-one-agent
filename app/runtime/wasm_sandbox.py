from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import wasmtime as _wasmtime  # type: ignore
except Exception:
    _wasmtime = None


class WasmSandboxUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class WasmPythonConfig:
    runtime_path: str
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    preopen_dirs: dict[str, str] = field(default_factory=dict)
    stdin_text: str = ""
    timeout_ms: int = 10_000
    fuel_budget: int = 1_000_000_000


@dataclass
class WasmPythonResult:
    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    timeout: bool = False
    timings_ms: dict[str, float] = field(default_factory=dict)
    module_cached: bool = False


class WasmPythonSandbox:
    """
    Minimal WASI execution bridge for a Python-in-WASM runtime such as
    MicroPython WASI. The runtime binary path is supplied by configuration so
    LongCat can switch runtimes without rewriting the agent loop.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._engine = None
        self._module_cache: dict[str, Any] = {}
        self._epoch_lock = threading.Lock()
        self._epoch_users = 0
        self._epoch_stop = threading.Event()
        self._epoch_thread: Optional[threading.Thread] = None
        self._epoch_interval_ms = 10

    @property
    def available(self) -> bool:
        return _wasmtime is not None

    def can_execute(self, runtime_path: str) -> bool:
        return bool(self.available and runtime_path and os.path.isfile(runtime_path))

    def _engine_instance(self):
        if _wasmtime is None:
            raise WasmSandboxUnavailable("wasmtime not installed")
        with self._lock:
            if self._engine is None:
                config = _wasmtime.Config()
                try:
                    config.consume_fuel = True
                except Exception as exc:
                    raise WasmSandboxUnavailable(f"wasm fuel metering unavailable: {exc}") from exc
                try:
                    config.epoch_interruption = True
                except Exception:
                    pass
                self._engine = _wasmtime.Engine(config)
            return self._engine

    def _start_epoch_driver(self, engine) -> None:
        with self._epoch_lock:
            self._epoch_users += 1
            if self._epoch_thread is not None and self._epoch_thread.is_alive():
                return
            self._epoch_stop.clear()

            def _drive_epoch() -> None:
                while not self._epoch_stop.wait(self._epoch_interval_ms / 1000.0):
                    try:
                        engine.increment_epoch()
                    except Exception:
                        break

            self._epoch_thread = threading.Thread(
                target=_drive_epoch,
                name="longcat-wasm-epoch-driver",
                daemon=True,
            )
            self._epoch_thread.start()

    def _stop_epoch_driver(self) -> None:
        with self._epoch_lock:
            self._epoch_users = max(0, self._epoch_users - 1)
            if self._epoch_users == 0:
                self._epoch_stop.set()

    def _load_module(self, runtime_path: str):
        runtime_path = os.path.abspath(runtime_path)
        with self._lock:
            cached = self._module_cache.get(runtime_path)
            if cached is not None:
                return cached, True
            engine = self._engine_instance()
            module = _wasmtime.Module.from_file(engine, runtime_path)
            self._module_cache[runtime_path] = module
            return module, False

    def execute(self, config: WasmPythonConfig) -> WasmPythonResult:
        total_started = time.perf_counter()
        timings_ms: dict[str, float] = {}

        def mark(name: str, started: float) -> None:
            timings_ms[name] = round((time.perf_counter() - started) * 1000.0, 3)

        if _wasmtime is None:
            raise WasmSandboxUnavailable("wasmtime not installed")
        runtime_path = os.path.abspath(str(config.runtime_path or "").strip())
        if not os.path.isfile(runtime_path):
            raise WasmSandboxUnavailable(f"missing wasm runtime: {runtime_path}")

        started = time.perf_counter()
        module, module_cached = self._load_module(runtime_path)
        mark("module_load", started)
        engine = self._engine_instance()
        stdout_path = ""
        stderr_path = ""
        stdin_path = ""
        epoch_driver_started = False
        try:
            started = time.perf_counter()
            with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as stdin_file:
                stdin_file.write(config.stdin_text or "")
                stdin_path = stdin_file.name
            with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as stdout_file:
                stdout_path = stdout_file.name
            with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as stderr_file:
                stderr_path = stderr_file.name

            store = _wasmtime.Store(engine)
            if not hasattr(store, "set_fuel"):
                raise WasmSandboxUnavailable("wasm fuel budget unavailable")
            try:
                store.set_fuel(max(1_000_000, int(config.fuel_budget or 1_000_000_000)))
            except Exception as exc:
                raise WasmSandboxUnavailable(f"wasm fuel budget unavailable: {exc}") from exc
            if hasattr(store, "set_epoch_deadline"):
                try:
                    deadline_ticks = max(1, int((max(1, int(config.timeout_ms or 10_000)) + self._epoch_interval_ms - 1) / self._epoch_interval_ms))
                    store.set_epoch_deadline(deadline_ticks)
                    self._start_epoch_driver(engine)
                    epoch_driver_started = True
                except Exception:
                    pass

            wasi = _wasmtime.WasiConfig()
            wasi.argv = list(config.argv or [os.path.basename(runtime_path)])
            wasi.env = [(str(key), str(value)) for key, value in (config.env or {}).items()]
            for guest_path, host_path in (config.preopen_dirs or {}).items():
                if host_path and os.path.isdir(host_path):
                    dir_perms = getattr(_wasmtime, "DirPerms", None)
                    file_perms = getattr(_wasmtime, "FilePerms", None)
                    if dir_perms is not None and file_perms is not None:
                        wasi.preopen_dir(
                            os.path.abspath(host_path),
                            guest_path,
                            dir_perms.READ_ONLY,
                            file_perms.READ_ONLY,
                        )
                    else:
                        wasi.preopen_dir(os.path.abspath(host_path), guest_path)
            wasi.stdin_file = stdin_path
            wasi.stdout_file = stdout_path
            wasi.stderr_file = stderr_path
            store.set_wasi(wasi)
            mark("store_setup", started)

            started = time.perf_counter()
            linker = _wasmtime.Linker(engine)
            linker.define_wasi()
            instance = linker.instantiate(store, module)
            start = instance.exports(store)["_start"]
            mark("instantiate", started)

            exit_code = 0
            error = ""
            timeout = False
            started = time.perf_counter()
            try:
                start(store)
            except Exception as exc:
                trap = getattr(_wasmtime, "ExitTrap", None)
                if trap is not None and isinstance(exc, trap):
                    exit_code = int(getattr(exc, "code", 1) or 1)
                else:
                    trap_type = getattr(_wasmtime, "Trap", None)
                    if trap_type is not None and isinstance(exc, trap_type):
                        message = str(exc)
                        lower_message = message.lower()
                        timeout = (
                            "fuel" in lower_message
                            or "deadline" in lower_message
                            or "interrupt" in lower_message
                            or "epoch" in lower_message
                        )
                        exit_code = 124 if timeout else 1
                        error = "wasm_execution_timeout: execution deadline exceeded" if timeout else message
                    else:
                        raise
            finally:
                mark("run", started)

            stdout_text = ""
            stderr_text = ""
            if stdout_path and os.path.isfile(stdout_path):
                with open(stdout_path, "r", encoding="utf-8", errors="replace") as handle:
                    stdout_text = handle.read()
            if stderr_path and os.path.isfile(stderr_path):
                with open(stderr_path, "r", encoding="utf-8", errors="replace") as handle:
                    stderr_text = handle.read()
            if hasattr(store, "get_fuel"):
                try:
                    timings_ms["fuel_remaining"] = int(store.get_fuel())
                except Exception:
                    pass
            timings_ms["total"] = round((time.perf_counter() - total_started) * 1000.0, 3)
            return WasmPythonResult(
                ok=exit_code == 0 and not timeout,
                exit_code=exit_code,
                stdout=stdout_text,
                stderr=stderr_text,
                error=error or (stderr_text.strip() if exit_code else ""),
                timeout=timeout,
                timings_ms=timings_ms,
                module_cached=module_cached,
            )
        finally:
            if epoch_driver_started:
                self._stop_epoch_driver()
            for path in (stdin_path, stdout_path, stderr_path):
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass


wasm_python_sandbox = WasmPythonSandbox()

from __future__ import annotations

import os
import asyncio
import threading
import time
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.staticfiles import StaticFiles

from app.compat.asgi import legacy_routes, legacy_websocket_routes
from app.runtime.agent_runtime import agent_runtime
from app.services import legacy_server


BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIST_DIR = BASE_DIR / "dist"
API_PREFIX = "/api/v1"

_httpx_async_client = httpx.AsyncClient(
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    timeout=httpx.Timeout(10.0, read=60.0, write=20.0, connect=10.0)
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        try:
            await _httpx_async_client.aclose()
        except Exception:
            pass
        try:
            await legacy_server._close_longcat_async_client()
        except Exception:
            pass
        shutdown = getattr(legacy_server, "_shutdown_browser_loop", None)
        if shutdown:
            shutdown()


def _file_or_404(path: Path, media_type: str | None = None):
    if not path.exists() or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type=media_type)


def _frontend_index_path() -> Path:
    built_index = FRONTEND_DIST_DIR / "index.html"
    if built_index.exists():
        return built_index
    return BASE_DIR / "index.html"


async def _native_agent_sse_bytes(event_iter):
    yield b"retry: 1000\n\n"
    async for event_name, data in event_iter:
        if event_name == "__ping__":
            yield b": ping\n\n"
            continue
        yield legacy_server._sse_message(event_name, data).encode("utf-8")


def create_app() -> FastAPI:
    app = FastAPI(
        title="LongCat Backend",
        version="2.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get(f"{API_PREFIX}/health")
    async def health_check():
        takeover_status = legacy_server.bridge.get_status()
        harness_status = legacy_server.harness.get_status()
        return JSONResponse(
            {
                "status": "ok",
                "service": "LongCat FastAPI Backend",
                "apiPrefix": API_PREFIX,
                "supported_formats": list(legacy_server.SUPPORTED_EXTENSIONS),
                "browserTakeover": takeover_status,
                "browserHarness": harness_status,
                "browserAvailable": bool(harness_status.get("ready") or harness_status.get("starting")),
                "agentRuntime": agent_runtime.status(),
                "searchProviders": legacy_server._agent_search_provider_status(),
            }
        )

    @app.post(f"{API_PREFIX}/longcat/chat")
    async def async_longcat_chat_proxy(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"success": False, "error": "请求体必须是 JSON 对象"}, status_code=400)
        
        if not isinstance(payload, dict):
            return JSONResponse({"success": False, "error": "请求体必须是 JSON 对象"}, status_code=400)

        payload["model"] = legacy_server.LONGCAT_PROXY_MODEL
        custom_key = request.headers.get("X-LongCat-Api-Key", "")
        keys = legacy_server._longcat_proxy_keys(custom_key)
        if not keys:
            return JSONResponse({"success": False, "error": "服务端缺少 LongCat API Key"}, status_code=503)

        wants_stream = bool(payload.get("stream"))

        if wants_stream:
            async def _async_stream_generator():
                yield b": connected\n\n"
                stream_error = ""
                for index, key in enumerate(keys):
                    headers = {
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                        "Accept-Encoding": "identity",
                        "Cache-Control": "no-cache",
                        "Authorization": f"Bearer {key}",
                    }
                    url = legacy_server.LONGCAT_PROXY_API_URL
                    timeout = legacy_server.LONGCAT_PROXY_TIMEOUT_SECONDS
                    try:
                        async with _httpx_async_client.stream(
                            "POST", url, json=payload, headers=headers, timeout=timeout
                        ) as upstream_response:
                            if upstream_response.status_code >= 400:
                                raise httpx.HTTPStatusError(
                                    f"Upstream error {upstream_response.status_code}",
                                    request=upstream_response.request,
                                    response=upstream_response,
                                )
                            async for chunk in upstream_response.aiter_bytes():
                                yield chunk
                        return
                    except httpx.HTTPStatusError as exc:
                        try:
                            error_body = await exc.response.aread()
                            stream_error = error_body.decode("utf-8", errors="replace") or str(exc)
                        except Exception:
                            stream_error = str(exc)
                        
                        if index < len(keys) - 1 and legacy_server._longcat_proxy_should_retry(exc.response.status_code):
                            continue
                        
                        yield legacy_server._sse_message("error", {
                            "success": False,
                            "status": exc.response.status_code,
                            "error": stream_error,
                        }).encode("utf-8")
                        return
                    except Exception as exc:
                        stream_error = str(exc)
                        if index < len(keys) - 1:
                            continue
                
                yield legacy_server._sse_message("error", {
                    "success": False,
                    "error": stream_error or "LongCat 代理请求失败",
                }).encode("utf-8")

            return StreamingResponse(
                _async_stream_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                }
            )
        else:
            last_error = ""
            for index, key in enumerate(keys):
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                    "Authorization": f"Bearer {key}",
                }
                url = legacy_server.LONGCAT_PROXY_API_URL
                timeout = legacy_server.LONGCAT_PROXY_TIMEOUT_SECONDS
                try:
                    upstream_response = await _httpx_async_client.post(
                        url, json=payload, headers=headers, timeout=timeout
                    )
                    if upstream_response.status_code >= 400:
                        raise httpx.HTTPStatusError(
                            f"Upstream error {upstream_response.status_code}",
                            request=upstream_response.request,
                            response=upstream_response,
                        )
                    return Response(
                        content=upstream_response.content,
                        status_code=upstream_response.status_code,
                        media_type=upstream_response.headers.get("Content-Type", "application/json"),
                    )
                except httpx.HTTPStatusError as exc:
                    try:
                        error_body = await exc.response.aread()
                    except Exception:
                        error_body = b""
                    if index < len(keys) - 1 and legacy_server._longcat_proxy_should_retry(exc.response.status_code):
                        continue
                    return Response(
                        content=error_body,
                        status_code=exc.response.status_code,
                        media_type=exc.response.headers.get("Content-Type", "application/json"),
                    )
                except Exception as exc:
                    last_error = str(exc)
                    if index < len(keys) - 1:
                        continue
            
            return JSONResponse({"success": False, "error": last_error or "LongCat 代理请求失败"}, status_code=502)

    @app.post(f"{API_PREFIX}/agent/search/stream")
    async def async_agent_search_stream(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        loop = asyncio.get_running_loop()
        async_queue = asyncio.Queue()
        done = object()

        def emit(event_name, data):
            loop.call_soon_threadsafe(async_queue.put_nowait, (event_name, data))

        def worker():
            try:
                result = legacy_server._agent_search_agent_payload(payload, emit=emit)
                loop.call_soon_threadsafe(async_queue.put_nowait, ("result", result))
            except Exception as exc:
                loop.call_soon_threadsafe(async_queue.put_nowait, ("error", {"success": False, "error": str(exc)}))
            finally:
                loop.call_soon_threadsafe(async_queue.put_nowait, done)

        threading.Thread(target=worker, daemon=True).start()

        async def _async_generator():
            yield b"retry: 1000\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(async_queue.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    yield b": ping\n\n"
                    continue
                if item is done:
                    break
                event_name, data = item
                yield legacy_server._sse_message(event_name, data).encode("utf-8")

        return StreamingResponse(
            _async_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )

    @app.post(f"{API_PREFIX}/agent/native-chat")
    async def async_native_agent_chat(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"success": False, "error": "请求体必须是 JSON 对象"}, status_code=400)
        
        if not isinstance(payload, dict):
            return JSONResponse({"success": False, "error": "请求体必须是 JSON 对象"}, status_code=400)
            
        custom_key = request.headers.get("X-LongCat-Api-Key", "")
        keys = legacy_server._longcat_proxy_keys(custom_key)
        if not keys:
            return JSONResponse({"success": False, "error": "服务端缺少 LongCat API Key"}, status_code=503)

        context = {
            "user": payload.get("user") if isinstance(payload.get("user"), dict) else {},
            "session_id": str(payload.get("session_id") or payload.get("sessionId") or "").strip()[:160],
            "recent_messages": payload.get("recent_messages") if isinstance(payload.get("recent_messages"), list) else [],
            "latest_user_text": legacy_server._native_strip_explicit_tool_prefix(payload.get("latest_user_text") or payload.get("query") or "")[:2000],
            "client_location": str(payload.get("client_location") or "").strip()[:80],
            "client_location_name": str(payload.get("client_location_name") or payload.get("clientLocationName") or "").strip()[:120],
            "client_location_source": str(payload.get("client_location_source") or payload.get("clientLocationSource") or "").strip()[:80],
            "client_location_error": str(payload.get("client_location_error") or payload.get("clientLocationError") or "").strip()[:240],
            "system_time": str(payload.get("system_time") or payload.get("systemTime") or "").strip()[:120],
            "client_timezone": str(payload.get("client_timezone") or payload.get("clientTimezone") or "").strip()[:80],
        }
        messages = legacy_server._native_prepare_messages(payload)
        scoped_tool_names = legacy_server._native_explicit_tool_scope(payload.get("latest_user_text") or payload.get("query") or "")
        tools = legacy_server._native_filter_tool_definitions(
            scoped_tool_names,
            legacy_server._native_tool_definitions(),
            include_plan=False,
        )

        budget = legacy_server._NativeIterationBudget(
            legacy_server.LONGCAT_NATIVE_TOOL_MAX_ROUNDS,
            legacy_server.LONGCAT_NATIVE_TOOL_WALL_TIMEOUT_MS,
            legacy_server.LONGCAT_NATIVE_TOOL_NO_PLAN_SOFT_ROUNDS,
            legacy_server.LONGCAT_NATIVE_CONTEXT_BUDGET_TOKENS,
        )
        run = legacy_server._native_create_agent_run(
            keys=keys,
            context=context,
            payload=payload,
            messages=messages,
            tools=tools,
            budget=budget,
            scoped_tool_names=scoped_tool_names,
            is_resume=False,
            session_id=context.get("session_id"),
        )

        return StreamingResponse(
            _native_agent_sse_bytes(run.events()),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )

    @app.post(f"{API_PREFIX}/agent/native-chat/resume")
    async def async_resume_agent_chat(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"success": False, "error": "请求体必须是 JSON 对象"}, status_code=400)

        if not isinstance(payload, dict):
            return JSONResponse({"success": False, "error": "请求体必须是 JSON 对象"}, status_code=400)

        session_id = payload.get("session_id") or payload.get("sessionId")
        tool_call_id = payload.get("tool_call_id") or payload.get("toolCallId")
        answers = payload.get("answers") or {}

        if not session_id:
            return JSONResponse({"success": False, "error": "缺少 session_id"}, status_code=400)

        custom_key = request.headers.get("X-LongCat-Api-Key", "")
        keys = legacy_server._longcat_proxy_keys(custom_key)
        if not keys:
            return JSONResponse({"success": False, "error": "服务端缺少 LongCat API Key"}, status_code=503)

        checkpoint_resume = False
        if tool_call_id:
            state = legacy_server._agent_load_suspended_session(session_id)
            if not state:
                return JSONResponse({"success": False, "error": "未找到挂起的会话或会话已超时"}, status_code=404)

            if state.get("tool_call_id") != tool_call_id:
                return JSONResponse({"success": False, "error": "tool_call_id 不匹配"}, status_code=400)

            legacy_server._agent_delete_suspended_session(session_id)
        else:
            state = legacy_server._agent_load_latest_checkpoint(session_id)
            checkpoint_resume = True
            if not state:
                return JSONResponse({"success": False, "error": "缺少 tool_call_id，且未找到可恢复 checkpoint"}, status_code=404)

        # Reconstruct state
        messages = state["messages"]
        context = state["context"]
        orig_payload = state["payload"]
        budget_rounds = state["budget_rounds"]
        budget_elapsed = state["budget_elapsed"]

        scoped_tool_names = legacy_server._native_explicit_tool_scope(orig_payload.get("latest_user_text") or orig_payload.get("query") or "")
        tools = legacy_server._native_filter_tool_definitions(
            scoped_tool_names,
            legacy_server._native_tool_definitions(),
            include_plan=False,
        )

        # Reconstruct budget using restored metrics
        budget = legacy_server._NativeIterationBudget(
            budget_rounds,
            legacy_server.LONGCAT_NATIVE_TOOL_WALL_TIMEOUT_MS - budget_elapsed,
            legacy_server.LONGCAT_NATIVE_TOOL_NO_PLAN_SOFT_ROUNDS,
            legacy_server.LONGCAT_NATIVE_CONTEXT_BUDGET_TOKENS,
        )

        run = legacy_server._native_create_agent_run(
            keys=keys,
            context=context,
            payload=orig_payload,
            messages=messages,
            tools=tools,
            budget=budget,
            scoped_tool_names=scoped_tool_names,
            is_resume=True,
            session_id=session_id,
            resume_tool_call_id="" if checkpoint_resume else tool_call_id,
            resume_answers=answers,
        )

        return StreamingResponse(
            _native_agent_sse_bytes(run.events()),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )

    legacy_skip_paths = {
        f"{API_PREFIX}/health",
        f"{API_PREFIX}/longcat/chat",
        f"{API_PREFIX}/agent/native-chat",
        f"{API_PREFIX}/agent/native-chat/resume",
        f"{API_PREFIX}/agent/search/stream",
    }
    for route in legacy_routes(legacy_server.app):
        if route.path in legacy_skip_paths:
            continue
        app.router.routes.append(route)

    for route in legacy_websocket_routes(legacy_server.app):
        app.router.routes.append(route)

    static_dir = BASE_DIR / "static"
    icons_dir = BASE_DIR / "icons"
    assets_dir = FRONTEND_DIST_DIR / "assets"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    if icons_dir.exists():
        app.mount("/icons", StaticFiles(directory=icons_dir), name="icons")
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    async def root():
        return _file_or_404(_frontend_index_path(), "text/html")

    @app.get("/index.html")
    async def index_html():
        return _file_or_404(_frontend_index_path(), "text/html")

    @app.get("/style.css")
    async def style_css():
        return _file_or_404(BASE_DIR / "style.css", "text/css")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return _file_or_404(BASE_DIR / "manifest.webmanifest", "application/manifest+json")

    @app.get("/sw.js")
    async def service_worker():
        return _file_or_404(BASE_DIR / "sw.js", "application/javascript")

    @app.get("/{filename:path}")
    async def static_fallback(filename: str):
        normalized = Path(os.path.normpath("/" + filename).lstrip("/"))
        candidate = (BASE_DIR / normalized).resolve()
        try:
            candidate.relative_to(BASE_DIR.resolve())
        except ValueError:
            return Response(status_code=404)
        return _file_or_404(candidate)

    return app


app = create_app()

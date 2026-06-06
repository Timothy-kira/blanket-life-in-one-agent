from __future__ import annotations

import asyncio
import inspect
import json
import queue
import threading
from typing import Any, Callable

from fastapi import WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response as StarletteResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from app.compat.flaskish import (
    FileResponseCompat,
    JsonResponseCompat,
    RequestAdapter,
    Response,
    UploadFileCompat,
    bind_request,
    reset_request,
)


def flask_rule_to_starlette(rule: str) -> str:
    """Convert a Flask-style rule into Starlette path syntax."""
    converted = rule
    while "<path:" in converted:
        start = converted.index("<path:")
        end = converted.index(">", start)
        name = converted[start + len("<path:"):end]
        converted = f"{converted[:start]}{{{name}:path}}{converted[end + 1:]}"
    while "<" in converted and ">" in converted:
        start = converted.index("<")
        end = converted.index(">", start)
        name = converted[start + 1:end]
        converted = f"{converted[:start]}{{{name}}}{converted[end + 1:]}"
    return converted


def api_v1_path(rule: str) -> str | None:
    """Map legacy public paths to the new /api/v1 namespace."""
    if rule in {"/", "/manifest.webmanifest", "/sw.js", "/<path:filename>"}:
        return None
    if rule.startswith("/api/"):
        return f"/api/v1{rule[4:]}"
    return f"/api/v1{rule}"


async def build_request_adapter(request: Request) -> RequestAdapter:
    json_data = None
    form_data: FormData | None = None
    files: dict[str, UploadFileCompat] = {}
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            json_data = await request.json()
        except Exception:
            json_data = None

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form_data = await request.form()
        for key, value in form_data.multi_items():
            if isinstance(value, UploadFile):
                files[key] = UploadFileCompat(value)

    url = str(request.url)
    host_url = f"{request.url.scheme}://{request.url.netloc}/"
    return RequestAdapter(
        method=request.method,
        url=url,
        host_url=host_url,
        headers=request.headers,
        args=request.query_params,
        json_data=json_data,
        form=form_data,
        files=files,
    )


def _media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip() or None


def _looks_streaming(body: Any) -> bool:
    if body is None:
        return False
    if isinstance(body, (bytes, bytearray, str, dict, list, tuple)):
        return False
    return hasattr(body, "__iter__")


def convert_legacy_result(result: Any) -> StarletteResponse:
    status_code: int | None = None
    headers: dict[str, str] = {}

    if isinstance(result, tuple):
        if len(result) >= 1:
            body = result[0]
        else:
            body = b""
        if len(result) >= 2 and result[1] is not None:
            status_code = int(result[1])
        if len(result) >= 3 and isinstance(result[2], dict):
            headers.update(result[2])
    else:
        body = result

    if isinstance(body, JsonResponseCompat):
        headers.update(body.headers)
        return JSONResponse(body.data, status_code=status_code or body.status_code, headers=headers)

    if isinstance(body, FileResponseCompat):
        headers.update(body.headers)
        if not body.path:
            return PlainTextResponse("", status_code=404, headers=headers)
        return FileResponse(body.path, status_code=status_code or body.status_code, headers=headers)

    if isinstance(body, Response):
        headers.update(body.headers)
        content_type = body.content_type or body.mimetype
        media_type = _media_type(content_type)
        if _looks_streaming(body.response):
            return StreamingResponse(
                body.response,
                status_code=status_code or body.status_code,
                media_type=media_type,
                headers=headers,
            )
        return StarletteResponse(
            content=body.response,
            status_code=status_code or body.status_code,
            media_type=media_type,
            headers=headers,
        )

    if isinstance(body, StarletteResponse):
        return body

    if isinstance(body, (dict, list)):
        return JSONResponse(body, status_code=status_code or 200, headers=headers)

    if body is None:
        body = b""
    return StarletteResponse(content=body, status_code=status_code or 200, headers=headers)


def make_legacy_http_endpoint(endpoint: Callable[..., Any]) -> Callable[[Request], Any]:
    async def legacy_http_endpoint(request: Request):
        adapter = await build_request_adapter(request)
        token = bind_request(adapter)
        try:
            kwargs = dict(request.path_params)
            result = await run_in_threadpool(endpoint, **kwargs)
        finally:
            reset_request(token)
        return convert_legacy_result(result)

    legacy_http_endpoint.__name__ = getattr(endpoint, "__name__", "legacy_http_endpoint")
    return legacy_http_endpoint


class SyncWebSocketAdapter:
    def __init__(self) -> None:
        self._incoming: queue.Queue[str | None] = queue.Queue()
        self._outgoing: queue.Queue[str | None] = queue.Queue()

    def receive(self) -> str | None:
        return self._incoming.get()

    def send(self, message: Any) -> None:
        if not isinstance(message, str):
            message = json.dumps(message, ensure_ascii=False)
        self._outgoing.put(message)

    def close(self) -> None:
        self._incoming.put(None)
        self._outgoing.put(None)

    async def put_incoming(self, message: str | None) -> None:
        await asyncio.to_thread(self._incoming.put, message)

    async def get_outgoing(self) -> str | None:
        return await asyncio.to_thread(self._outgoing.get)


def make_legacy_websocket_endpoint(endpoint: Callable[..., Any]) -> Callable[[WebSocket], Any]:
    async def legacy_websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        adapter = SyncWebSocketAdapter()
        done = asyncio.Event()

        def run_handler() -> None:
            try:
                if inspect.signature(endpoint).parameters:
                    endpoint(adapter)
                else:
                    endpoint()
            finally:
                adapter.close()
                loop.call_soon_threadsafe(done.set)

        loop = asyncio.get_running_loop()
        thread = threading.Thread(target=run_handler, name=f"ws-{getattr(endpoint, '__name__', 'legacy')}", daemon=True)
        thread.start()

        async def receive_loop() -> None:
            try:
                while True:
                    message = await websocket.receive_text()
                    await adapter.put_incoming(message)
            except WebSocketDisconnect:
                await adapter.put_incoming(None)
            except RuntimeError:
                await adapter.put_incoming(None)

        async def send_loop() -> None:
            while True:
                message = await adapter.get_outgoing()
                if message is None:
                    return
                await websocket.send_text(message)

        receive_task = asyncio.create_task(receive_loop())
        send_task = asyncio.create_task(send_loop())
        done_task = asyncio.create_task(done.wait())
        try:
            await asyncio.wait({receive_task, send_task, done_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            adapter.close()
            for task in (receive_task, send_task, done_task):
                task.cancel()
            try:
                await websocket.close()
            except Exception:
                pass

    legacy_websocket_endpoint.__name__ = getattr(endpoint, "__name__", "legacy_websocket_endpoint")
    return legacy_websocket_endpoint


def legacy_routes(legacy_app: Any) -> list[Route]:
    routes: list[Route] = []
    for spec in getattr(legacy_app, "routes", []):
        mapped = api_v1_path(spec.rule)
        if not mapped:
            continue
        routes.append(
            Route(
                flask_rule_to_starlette(mapped),
                make_legacy_http_endpoint(spec.endpoint),
                methods=[m.upper() for m in spec.methods],
                name=getattr(spec.endpoint, "__name__", None),
            )
        )
    return routes


def legacy_websocket_routes(legacy_app: Any) -> list[WebSocketRoute]:
    routes: list[WebSocketRoute] = []
    for spec in getattr(legacy_app, "websocket_routes", []):
        mapped = api_v1_path(spec.rule)
        if not mapped:
            continue
        routes.append(
            WebSocketRoute(
                flask_rule_to_starlette(mapped),
                make_legacy_websocket_endpoint(spec.endpoint),
                name=getattr(spec.endpoint, "__name__", None),
            )
        )
    return routes

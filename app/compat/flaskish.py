from __future__ import annotations

import contextvars
import json
import os
import posixpath
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from starlette.datastructures import FormData, Headers, QueryParams, UploadFile


@dataclass
class RouteSpec:
    rule: str
    methods: List[str]
    endpoint: Callable[..., Any]


@dataclass
class WebSocketRouteSpec:
    rule: str
    endpoint: Callable[..., Any]


class Flask:
    """Small route registry with the Flask methods used by the legacy module."""

    def __init__(self, import_name: str, static_folder: str | None = None, static_url_path: str | None = None):
        self.import_name = import_name
        self.static_folder = static_folder
        self.static_url_path = static_url_path
        self.config: Dict[str, Any] = {}
        self.routes: List[RouteSpec] = []
        self.websocket_routes: List[WebSocketRouteSpec] = []

    def route(self, rule: str, methods: Optional[Iterable[str]] = None, **_: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.routes.append(RouteSpec(rule=rule, methods=list(methods or ["GET"]), endpoint=func))
            return func

        return decorator

    def register_blueprint(self, blueprint: "Blueprint", url_prefix: str = "") -> None:
        prefix = (url_prefix or "").rstrip("/")
        for spec in blueprint.routes:
            rule = f"{prefix}{spec.rule if spec.rule.startswith('/') else '/' + spec.rule}"
            self.routes.append(RouteSpec(rule=rule, methods=spec.methods, endpoint=spec.endpoint))

    @contextmanager
    def test_request_context(self, path: str, method: str = "GET", json: Any = None, headers: Optional[Dict[str, str]] = None):
        adapter = RequestAdapter(
            method=method,
            url=path,
            host_url="",
            headers=Headers(headers or {}),
            args=QueryParams(""),
            json_data=json,
        )
        token = bind_request(adapter)
        try:
            yield adapter
        finally:
            reset_request(token)

    def run(self, *_: Any, **__: Any) -> None:
        raise RuntimeError("The FastAPI backend must be started with uvicorn.")


class Blueprint:
    def __init__(self, name: str, import_name: str):
        self.name = name
        self.import_name = import_name
        self.routes: List[RouteSpec] = []

    def route(self, rule: str, methods: Optional[Iterable[str]] = None, **_: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.routes.append(RouteSpec(rule=rule, methods=list(methods or ["GET"]), endpoint=func))
            return func

        return decorator


class Sock:
    def __init__(self, app: Flask):
        self.app = app

    def route(self, rule: str, **_: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.app.websocket_routes.append(WebSocketRouteSpec(rule=rule, endpoint=func))
            return func

        return decorator


def CORS(*_: Any, **__: Any) -> None:
    return None


class JsonResponseCompat:
    def __init__(self, data: Any):
        self.data = data
        self.status_code = 200
        self.headers: Dict[str, str] = {}


class Response:
    def __init__(
        self,
        response: Any = b"",
        status: int = 200,
        mimetype: str | None = None,
        content_type: str | None = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.response = response
        self.status_code = status
        self.mimetype = mimetype
        self.content_type = content_type or mimetype
        self.headers: Dict[str, str] = dict(headers or {})

    def get_data(self, as_text: bool = False) -> bytes | str:
        data = self.response
        if isinstance(data, str):
            return data if as_text else data.encode("utf-8")
        if isinstance(data, bytearray):
            data = bytes(data)
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace") if as_text else data
        text = json.dumps(data, ensure_ascii=False, default=str)
        return text if as_text else text.encode("utf-8")


class FileResponseCompat:
    def __init__(self, path: str, status_code: int = 200):
        self.path = path
        self.status_code = status_code
        self.headers: Dict[str, str] = {}


class UploadFileCompat:
    def __init__(self, upload: UploadFile):
        self._upload = upload
        self.filename = upload.filename or ""
        self.content_type = upload.content_type

    def save(self, destination: str) -> None:
        self._upload.file.seek(0)
        with open(destination, "wb") as out:
            shutil.copyfileobj(self._upload.file, out)
        self._upload.file.seek(0)

    @property
    def stream(self):
        return self._upload.file


class RequestAdapter:
    def __init__(
        self,
        *,
        method: str,
        url: str,
        host_url: str,
        headers: Headers,
        args: QueryParams,
        json_data: Any = None,
        form: FormData | None = None,
        files: Optional[Dict[str, UploadFileCompat]] = None,
    ):
        self.method = method
        self.url = url
        self.host_url = host_url
        self.headers = headers
        self.args = args
        self._json_data = json_data
        self.form = form or FormData()
        self.files = files or {}

    def get_json(self, force: bool = False, silent: bool = False, **_: Any) -> Any:
        if self._json_data is not None:
            return self._json_data
        if silent:
            return None
        if force:
            raise ValueError("Request body does not contain valid JSON")
        return None


_request_var: contextvars.ContextVar[RequestAdapter | None] = contextvars.ContextVar("longcat_request", default=None)


class RequestProxy:
    def _current(self) -> RequestAdapter:
        current = _request_var.get()
        if current is None:
            raise RuntimeError("request is only available while handling an HTTP request")
        return current

    def __getattr__(self, name: str) -> Any:
        return getattr(self._current(), name)

    def get_json(self, *args: Any, **kwargs: Any) -> Any:
        return self._current().get_json(*args, **kwargs)


request = RequestProxy()


def bind_request(adapter: RequestAdapter):
    return _request_var.set(adapter)


def reset_request(token: contextvars.Token) -> None:
    _request_var.reset(token)


def jsonify(*args: Any, **kwargs: Any) -> JsonResponseCompat:
    if args and kwargs:
        raise TypeError("jsonify cannot mix positional and keyword arguments")
    if kwargs:
        return JsonResponseCompat(kwargs)
    if len(args) == 1:
        return JsonResponseCompat(args[0])
    return JsonResponseCompat(list(args))


def stream_with_context(generator):
    return generator


def send_from_directory(directory: str, path: str, **_: Any) -> FileResponseCompat:
    safe_path = posixpath.normpath("/" + path).lstrip("/")
    full_path = Path(directory, safe_path)
    base = Path(directory).resolve()
    resolved = full_path.resolve()
    if os.path.commonpath([str(base), str(resolved)]) != str(base):
        return FileResponseCompat(str(resolved), status_code=404)
    return FileResponseCompat(str(resolved), status_code=200)

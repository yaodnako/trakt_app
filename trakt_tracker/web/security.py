from __future__ import annotations

import os
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response


CSRF_COOKIE_NAME = "trakt_csrf"
CSRF_FORM_FIELD = "_csrf"
CSRF_HEADER_NAME = "X-Trakt-CSRF"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def _host_name(request: Request) -> str:
    host = request.headers.get("host", "").strip()
    if host.startswith("["):
        return host.split("]", 1)[0].lstrip("[").casefold()
    return host.split(":", 1)[0].casefold()


def _expected_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', '')}".rstrip("/")


def _error_response(request: Request, detail: str, status_code: int) -> Response:
    accepts_json = "application/json" in request.headers.get("accept", "").casefold()
    sends_json = "application/json" in request.headers.get("content-type", "").casefold()
    if accepts_json or sends_json:
        response: Response = JSONResponse({"detail": detail}, status_code=status_code)
        response.headers["Cache-Control"] = "no-store"
    else:
        response = PlainTextResponse(detail, status_code=status_code)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


async def portal_security_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    allow_lan = os.environ.get("TRAKT_TRACKER_ALLOW_LAN") == "1"
    if not allow_lan and _host_name(request) not in LOCAL_HOSTS:
        return _error_response(request, "Invalid host", 400)

    cookie_token = str(request.cookies.get(CSRF_COOKIE_NAME, "") or "")
    request_token = cookie_token or secrets.token_urlsafe(32)
    request.state.csrf_token = request_token

    if request.method.upper() in UNSAFE_METHODS:
        origin = request.headers.get("origin", "").rstrip("/")
        if origin and not secrets.compare_digest(origin, _expected_origin(request)):
            return _error_response(request, "CSRF validation failed", 403)

        supplied_token = request.headers.get(CSRF_HEADER_NAME, "")
        if not supplied_token:
            try:
                await request.body()
                form = await request.form()
                supplied_token = str(form.get(CSRF_FORM_FIELD, "") or "")
            except Exception:
                supplied_token = ""
        if not cookie_token or not supplied_token or not secrets.compare_digest(cookie_token, supplied_token):
            return _error_response(request, "CSRF validation failed", 403)

    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")

    content_type = response.headers.get("content-type", "").casefold()
    if request.url.path != "/static" and not request.url.path.startswith("/static/"):
        if "text/html" in content_type or "application/json" in content_type:
            response.headers["Cache-Control"] = "no-store"
        if not cookie_token and request.method.upper() in {"GET", "HEAD"}:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                request_token,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
    return response

"""Simple cookie-based session auth for VitalForge services."""

import os
import hmac
import time
import logging
from functools import wraps

from fastapi import Request, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logger = logging.getLogger(__name__)

_SECRET = os.environ.get("VITALFORGE_SECRET", "default-dev-secret")
_USER = os.environ.get("VITALFORGE_USER", "admin")
_PASS = os.environ.get("VITALFORGE_PASS", "")
_COOKIE_NAME = "vf_session"
_MAX_AGE = 30 * 24 * 3600  # 30 days

_serializer = URLSafeTimedSerializer(_SECRET)


def _is_auth_configured() -> bool:
    return bool(_PASS)


def create_session_cookie(username: str) -> str:
    return _serializer.dumps({"user": username, "t": int(time.time())})


def validate_session(cookie: str) -> str | None:
    try:
        data = _serializer.loads(cookie, max_age=_MAX_AGE)
        return data.get("user")
    except (BadSignature, SignatureExpired):
        return None


def get_current_user(request: Request) -> str | None:
    if not _is_auth_configured():
        return "anonymous"
    cookie = request.cookies.get(_COOKIE_NAME)
    if not cookie:
        return None
    return validate_session(cookie)


def require_auth(request: Request) -> str:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def check_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, _USER) and hmac.compare_digest(password, _PASS)


LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#1a1a2e">
    <title>VitalForge Login</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #e0e0e0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-box {
            background: #16213e;
            border-radius: 12px;
            padding: 2rem;
            width: 320px;
        }
        h1 { font-size: 1.3rem; color: #c0c0e0; margin-bottom: 1.5rem; text-align: center; }
        input {
            width: 100%;
            padding: 0.7rem;
            margin-bottom: 0.8rem;
            border: 1px solid #2a2a4a;
            border-radius: 6px;
            background: #1a1a2e;
            color: #e0e0e0;
            font-size: 0.95rem;
        }
        input:focus { outline: none; border-color: #5c6bc0; }
        button {
            width: 100%;
            padding: 0.7rem;
            background: #5c6bc0;
            color: #fff;
            border: none;
            border-radius: 6px;
            font-size: 0.95rem;
            cursor: pointer;
        }
        button:hover { background: #7c4dff; }
        .error { color: #ef5350; font-size: 0.85rem; margin-bottom: 0.8rem; text-align: center; }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>VitalForge</h1>
        <div class="error" id="error"></div>
        <form onsubmit="return doLogin(event)">
            <input type="text" id="user" placeholder="Username" autocomplete="username" required>
            <input type="password" id="pass" placeholder="Password" autocomplete="current-password" required>
            <button type="submit">Sign In</button>
        </form>
    </div>
    <script>
        async function doLogin(e) {
            e.preventDefault();
            const res = await fetch("/auth/login", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({username: document.getElementById("user").value, password: document.getElementById("pass").value})
            });
            if (res.ok) {
                window.location.href = "/";
            } else {
                document.getElementById("error").textContent = "Invalid credentials";
            }
            return false;
        }
    </script>
</body>
</html>"""


def add_auth_routes(app):
    """Add login/logout routes to a FastAPI app."""
    from fastapi.responses import JSONResponse

    @app.get("/auth/login")
    async def login_page(request: Request):
        if get_current_user(request):
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(LOGIN_PAGE_HTML)

    @app.post("/auth/login")
    async def login(request: Request):
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
        if not check_credentials(username, password):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        cookie = create_session_cookie(username)
        response = JSONResponse({"success": True})
        response.set_cookie(_COOKIE_NAME, cookie, max_age=_MAX_AGE, httponly=True, samesite="lax")
        return response

    @app.get("/auth/logout")
    async def logout():
        response = RedirectResponse("/auth/login", status_code=302)
        response.delete_cookie(_COOKIE_NAME)
        return response

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # Skip auth for login routes, health check, static files, and service worker
        path = request.url.path
        if path.startswith("/auth/") or path == "/health" or path.startswith("/static/"):
            return await call_next(request)

        if not _is_auth_configured():
            return await call_next(request)

        user = get_current_user(request)
        if user is None:
            if path.startswith("/api/"):
                raise HTTPException(status_code=401, detail="Not authenticated")
            return RedirectResponse("/auth/login", status_code=302)

        return await call_next(request)

"""Runs a FastAPI app on a real socket, in a background thread.

Playwright drives a real browser, so it needs an actual HTTP server — unlike
the API tests in this repo, which talk to the ASGI app in-process via
`httpx.ASGITransport` and never run lifespan events. `LiveServer` runs uvicorn
for real, so `app.py`'s `lifespan` (init_db, authenticate, background sync)
executes exactly as it would in production; callers must monkeypatch
`authenticate`/`scheduled_sync` the same way the existing `weight_app_module`/
`dashboard_app_module` fixtures already do before starting one.
"""

import socket
import threading
import time

import httpx
import uvicorn


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, app, host: str = "127.0.0.1"):
        self.port = _free_port()
        self.base_url = f"http://{host}:{self.port}"
        config = uvicorn.Config(app, host=host, port=self.port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self, timeout: float = 10.0):
        self.thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("live server did not start in time")

        while time.time() < deadline:
            try:
                httpx.get(f"{self.base_url}/health", timeout=0.5)
                return
            except httpx.TransportError:
                time.sleep(0.05)
        raise RuntimeError("live server did not become ready")

    def stop(self, timeout: float = 5.0):
        self.server.should_exit = True
        self.thread.join(timeout=timeout)

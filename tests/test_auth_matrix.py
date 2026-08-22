"""The 40-cell auth behavior matrix from docs/prp/00-design.md §2.5.

Four configs (VITALFORGE_PASS set/unset x VITALFORGE_API_TOKEN set/unset) x
ten credential forms (C0-C9), run against a throwaway FastAPI app so auth
behavior is isolated from either real service's routes. `ids=` is set to the
cell name (e.g. "A1-C3") so a failure names the exact matrix cell.

The A3/A4 rows (PASS unset -> auth fully off) are generated, not hand-written:
per §2.5, a configured token never becomes load-bearing when PASS is empty --
PASS is the single master switch for whether auth exists at all.
"""

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from httpx import ASGITransport, AsyncClient, Headers

from shared import auth as shared_auth
from shared.auth import add_auth_routes, create_session_cookie


def _build_matrix_app() -> FastAPI:
    app = FastAPI()
    add_auth_routes(app)

    @app.get("/api/thing")
    async def api_thing():
        return {"ok": True}

    @app.get("/page")
    async def page():
        return HTMLResponse("<html></html>")

    return app


@pytest.fixture
async def matrix_client():
    transport = ASGITransport(app=_build_matrix_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@dataclass(frozen=True)
class Config:
    pass_value: str
    token_value: str


CORRECT_TOKEN = "correct-token"
WRONG_TOKEN = "wrong-token"

A1_CONFIG = Config(pass_value="correct-pass", token_value=CORRECT_TOKEN)
A2_CONFIG = Config(pass_value="correct-pass", token_value="")
A3_CONFIG = Config(pass_value="", token_value=CORRECT_TOKEN)
A4_CONFIG = Config(pass_value="", token_value="")


def _valid_cookie() -> str:
    return create_session_cookie("testuser")


# Credential forms take no args -- they always present CORRECT_TOKEN/WRONG_TOKEN,
# and it's each row's *config* (whether _API_TOKEN equals CORRECT_TOKEN) that
# determines whether "the correct-looking token" actually matches.
CREDENTIAL_FORMS = {
    "C0": lambda: ({}, {}),
    "C1": lambda: ({}, {"vf_session": _valid_cookie()}),
    "C2": lambda: ({}, {"vf_session": "garbage-not-a-real-cookie"}),
    "C3": lambda: ({"Authorization": f"Bearer {CORRECT_TOKEN}"}, {}),
    "C4": lambda: ({"Authorization": f"Bearer {WRONG_TOKEN}"}, {}),
    "C5": lambda: ({"Authorization": "Bearer "}, {}),
    "C6": lambda: ({"Authorization": f"Basic {CORRECT_TOKEN}"}, {}),
    "C7": lambda: ({"Authorization": f"Bearer {CORRECT_TOKEN}"}, {"vf_session": "garbage-not-a-real-cookie"}),
    "C8": lambda: ({"Authorization": f"Bearer {WRONG_TOKEN}"}, {"vf_session": _valid_cookie()}),
    # Raw bytes, not a plain str dict: httpx's header dict encodes values as
    # strict ASCII and rejects "ö"/"é" outright, even though both are within
    # latin-1's range. Building via Headers(bytes) matches what Starlette
    # actually does on the wire -- latin-1-decode raw header bytes -- so this
    # is "Bearer tökén" UTF-8-encoded, exactly as a real client would send it.
    "C9": lambda: (Headers([(b"authorization", "Bearer tökén".encode())]), {}),
}

ALLOW = 200
DENY = 401

MATRIX = [
    # A1 -- PASS set, TOKEN set
    ("A1-C0", A1_CONFIG, "C0", DENY),
    ("A1-C1", A1_CONFIG, "C1", ALLOW),
    ("A1-C2", A1_CONFIG, "C2", DENY),
    ("A1-C3", A1_CONFIG, "C3", ALLOW),
    ("A1-C4", A1_CONFIG, "C4", DENY),
    ("A1-C5", A1_CONFIG, "C5", DENY),
    ("A1-C6", A1_CONFIG, "C6", DENY),
    ("A1-C7", A1_CONFIG, "C7", ALLOW),
    ("A1-C8", A1_CONFIG, "C8", ALLOW),
    ("A1-C9", A1_CONFIG, "C9", DENY),
    # A2 -- PASS set, TOKEN unset/empty/whitespace (all collapse: stripped at import)
    ("A2-C0", A2_CONFIG, "C0", DENY),
    ("A2-C1", A2_CONFIG, "C1", ALLOW),
    ("A2-C2", A2_CONFIG, "C2", DENY),
    ("A2-C3", A2_CONFIG, "C3", DENY),  # guard 1: no token configured, not a value mismatch
    ("A2-C4", A2_CONFIG, "C4", DENY),
    ("A2-C5", A2_CONFIG, "C5", DENY),
    ("A2-C6", A2_CONFIG, "C6", DENY),
    ("A2-C7", A2_CONFIG, "C7", DENY),
    ("A2-C8", A2_CONFIG, "C8", ALLOW),  # header ignored entirely, cookie still works
    ("A2-C9", A2_CONFIG, "C9", DENY),
]

# A3/A4 -- PASS unset -> auth off entirely. get_current_user() returns "anonymous"
# at step 1, before the bearer check ever runs, regardless of what's presented.
MATRIX += [(f"A3-{c}", A3_CONFIG, c, ALLOW) for c in CREDENTIAL_FORMS]
MATRIX += [(f"A4-{c}", A4_CONFIG, c, ALLOW) for c in CREDENTIAL_FORMS]

assert len(MATRIX) == 40


@pytest.mark.parametrize(
    "cell_id,config,credential_form,expected_status",
    MATRIX,
    ids=[row[0] for row in MATRIX],
)
async def test_behavior_matrix(cell_id, config, credential_form, expected_status, monkeypatch, matrix_client):
    monkeypatch.setattr(shared_auth, "_PASS", config.pass_value)
    monkeypatch.setattr(shared_auth, "_API_TOKEN", config.token_value)

    headers, cookies = CREDENTIAL_FORMS[credential_form]()
    resp = await matrix_client.get("/api/thing", headers=headers, cookies=cookies)
    assert resp.status_code == expected_status, f"{cell_id}: expected {expected_status}, got {resp.status_code}"

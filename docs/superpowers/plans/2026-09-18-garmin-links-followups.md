# Per-person Garmin links — review follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every MEDIUM/LOW finding left open by the 2026-09-18 review of PR #63 on the same branch, then get the branch merged and prod actually running it.

**Architecture:** All Garmin traffic already flows through `shared/garmin_registry.call()`; the remaining defects are small gaps in that boundary (an op that hands back an exception instead of raising it, one unbounded wait, a chmod that reaches one directory too high), one contract break, three cleanups, and the deploy. Each task is a self-contained commit with its own test. Nothing here touches the schema or a migration body.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, pytest (`asyncio_mode=auto`), `garminconnect==0.3.11`, Docker Compose on the prod box.

**Spec:** the review findings, recorded in `docs/superpowers/plans/2026-09-15-per-person-garmin-links.md` (feature plan) and the review transcript summarised in the PR #63 discussion; the four fix commits `ec40462..055b2aa` are the state this plan starts from.

## Global Constraints

- Branch `feat/per-person-garmin-links`. **Never push** and never merge without the user's explicit word; never `git checkout <file>`, `git stash`, or `git reset` while another agent may hold uncommitted work.
- PRIVACY: never read `.env`, any `*.db`, or anything under a `.garth/` directory — schema and file *names* only.
- Migrations are immutable; `003`/`004` bodies are not touched. No new markers.
- `shared/` must not import from `vitalforge_weight/` or `vitalforge_dashboard/`.
- Every commit: `.venv/bin/ruff check .` clean, `.venv/bin/pytest -q` green; anything that touches templates/conftest also `.venv/bin/pytest -q -m playwright` **as a separate process**.
- Files ≤ 800 lines (`tests/test_module_size.py` enforces), functions ≤ ~50 lines, type hints on changed signatures, never log `str(exc)` of a provider error (type name or bounded code only).
- Commit format `<type>: <description>`; types feat/fix/refactor/docs/test/chore.
- Run order matters: Tasks 1–8 are serial (same files), Task 9 gates 10–11.

---

## Task 0: Gate on the fix re-review (already dispatched)

**Files:** none (read `<scratchpad>/rereview-security.jsonl`, `<scratchpad>/rereview-code.jsonl`).

- [ ] **Step 1:** Read both files. Any CRITICAL/HIGH → fix on the branch first (own commit, own test), re-run `ruff` + `pytest -q`, and re-dispatch a fresh security reviewer for that commit only.
- [ ] **Step 2:** MEDIUM/LOW from the re-review that overlap Tasks 1–8 fold into those tasks; the rest go to Task 12's issue list.
- [ ] **Step 3:** Only when both re-reviewers say APPROVE (or their blockers are fixed and re-approved), start Task 1.

---

## Task 1: Registry evicts on auth failure even when the op returns the exception

`vitalforge_weight/activity_garmin.py:302-328` returns the provider exception as a *value* (so it can classify ambiguous post-send failures), which bypasses `_run_operation`'s `except` — a 401 from an activity push never evicts the cached client or records `last_auth_error`.

**Files:**
- Modify: `shared/garmin_registry.py:722-740` (`_run_operation`)
- Test: `tests/test_garmin_registry.py`

**Interfaces:**
- Produces: `_run_operation` treats a returned `Exception` exactly like a raised one for eviction/health, then returns it unchanged (callers keep their classification).

- [ ] **Step 1: Write the failing test** (append to `tests/test_garmin_registry.py`, next to `test_rate_limited_operation_keeps_the_cached_client_and_status`):

```python
async def test_returned_auth_exception_evicts_the_client_like_a_raised_one(initialized_db):
    person_id = await get_primary_person_id()
    await _link(person_id)
    cached = _FakeClient(1)
    garmin_client._clients[(person_id, 1)] = cached
    await garmin_registry._record_auth_success(person_id, 1)
    rejected = GarminConnectAuthenticationError("Portal login POST returned 401")

    def returns_the_error(_client):
        # activity_garmin hands provider errors back as values so it can
        # classify ambiguous post-send failures itself.
        return rejected

    result = await garmin_registry.call(person_id, returns_the_error)

    assert result is rejected, "the value contract must survive: the caller still classifies it"
    assert not garmin_client.is_authenticated(person_id, 1)
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT last_auth_error FROM garmin_links WHERE person_id = ?", (person_id,))
        ).fetchone()
    finally:
        await db.close()
    assert row["last_auth_error"] == "auth_failed"
```

`GarminConnectAuthenticationError` is already imported in that test file (used by `test_error_code_classifies_real_garminconnect_exceptions`); if not, `from garminconnect import GarminConnectAuthenticationError`.

- [ ] **Step 2: Run it, expect FAIL** — `.venv/bin/pytest -q tests/test_garmin_registry.py -k returned_auth_exception` → `assert not garmin_client.is_authenticated(...)` fails.

- [ ] **Step 3: Implement** — replace `_run_operation` in `shared/garmin_registry.py`:

```python
async def _note_operation_failure(person_id: int, generation: int, exc: BaseException) -> str:
    """Classify a provider failure and drop a session it says is dead.

    A 401/403 from a normal operation means this cached session is no
    longer safe to reuse.  Persist only the bounded code; the next
    operation resumes under this same lock protocol.
    """
    code = _error_code(exc)
    if code == "auth_failed":
        garmin_client.forget(person_id, generation)
        await _record_auth_failure(person_id, generation, code)
    return code


async def _run_operation(
    person_id: int, generation: int, client: Garmin, op: Callable[[Garmin], _T | Awaitable[_T]]
) -> _T:
    try:
        result = op(client)
        if inspect.isawaitable(result):
            result = await result
    except GarminRegistryError:
        raise
    except Exception as exc:
        code = await _note_operation_failure(person_id, generation, exc)
        raise GarminOperationError(code) from None
    if isinstance(result, Exception):
        # Some callers return the provider exception instead of raising it so
        # they can tell an ambiguous post-send failure from a safe one.  The
        # session-health side effects must not depend on that choice.
        await _note_operation_failure(person_id, generation, result)
    return result
```

- [ ] **Step 4: Run the registry + activity suites** — `.venv/bin/pytest -q tests/test_garmin_registry.py tests/test_activity_failure.py tests/test_activity_garmin_guard.py tests/test_activity_unknown_outcome.py` → all pass (the ambiguous-outcome tests still get their exception back).

- [ ] **Step 5: Commit**

```bash
git add shared/garmin_registry.py tests/test_garmin_registry.py
git commit -m "fix: evict a rejected Garmin session even when the op returns its exception"
```

---

## Task 2: Bound the post-login permit wait by the caller's remaining budget

`_resume_link` (`shared/garmin_registry.py:693-714`) waits for the second permit with no deadline, so a *cold* interactive push can exceed its `max_wait_seconds` while the sync holds the slot.

**Files:**
- Modify: `shared/garmin_registry.py` (`call`, `_resume_link`)
- Test: `tests/test_garmin_registry.py`

**Interfaces:**
- Produces: `_resume_link(person_id, generation, email, *, deadline_seconds: float | None) -> Garmin`. `call()` passes `max_wait_seconds - elapsed` when a budget was given, `None` otherwise (batch callers keep the unbounded wait).

- [ ] **Step 1: Write the failing test** (next to `test_call_waits_for_a_permit_only_within_its_budget`; reuse its `_set_next_allowed_at` helper and monkeypatched clock pattern):

```python
async def test_cold_call_bounds_its_post_login_permit_by_the_remaining_budget(
    initialized_db, monkeypatch, tmp_path
):
    person_id = await get_primary_person_id()
    await _link(person_id)
    _write_fake_token_store(tmp_path / "garth" / f"person-{person_id}" / "generation-1")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(garmin_registry.garmin_client, "authenticate", _fake_auth(calls))
    monkeypatch.setenv("GARMIN_MIN_CALL_INTERVAL_SECONDS", "60")
    now = 1_000.0
    monkeypatch.setattr(garmin_registry.time, "time", lambda: now)
    slept: list[float] = []

    async def fake_sleep(seconds):
        nonlocal now
        slept.append(seconds)
        now += seconds

    monkeypatch.setattr(garmin_registry.asyncio, "sleep", fake_sleep)
    await _set_next_allowed_at(0.0)  # first permit is free; the login consumes it

    with pytest.raises(garmin_registry.GarminRateLimited):
        await garmin_registry.call(person_id, lambda _c: "never", max_wait_seconds=5.0)

    assert calls == [(person_id, 1)], "the cold login happened"
    assert sum(slept) <= 5.0, f"post-login wait exceeded the budget: slept {slept}"
    assert garmin_client.is_authenticated(person_id, 1), "the warm client is kept for the retry"
```

- [ ] **Step 2: Run it, expect FAIL** — `.venv/bin/pytest -q tests/test_garmin_registry.py -k post_login_permit` → fails because the unbounded wait sleeps 60 s of fake time and then succeeds.

- [ ] **Step 3: Implement** — in `call()`, measure the budget once and pass the remainder:

```python
            started = time.monotonic()
            if max_wait_seconds > 0.0:
                await _wait_for_call_permit(deadline_seconds=max_wait_seconds)
            else:
                await reserve_call_permit()
            if garmin_client.is_authenticated(person_id, generation):
                client = garmin_client.get_client(person_id, generation)
            else:
                remaining = (
                    max(0.0, max_wait_seconds - (time.monotonic() - started)) if max_wait_seconds > 0.0 else None
                )
                client = await _resume_link(person_id, generation, email, deadline_seconds=remaining)
            return await _run_operation(person_id, generation, client, op)
```

and in `_resume_link` change the signature to `async def _resume_link(person_id: int, generation: int, email: str, *, deadline_seconds: float | None = None) -> Garmin:` and the last line to `await _wait_for_call_permit(deadline_seconds=deadline_seconds)`. Note `time.monotonic()` here is the real clock deliberately (the test's fake `time.time` only drives the permit table); `remaining == 0.0` makes `_wait_for_call_permit` raise on the first `GarminRateLimited`, which is the intended "budget spent" answer. Update `_resume_link`'s docstring: "…takes its second permit within the caller's remaining budget."

- [ ] **Step 4: Run** — `.venv/bin/pytest -q tests/test_garmin_registry.py` → pass.

- [ ] **Step 5: Commit**

```bash
git add shared/garmin_registry.py tests/test_garmin_registry.py
git commit -m "fix: bound a cold interactive Garmin call's second permit by its remaining budget"
```

---

## Task 3: `_ensure_token_dir` never tightens the token root's parent

`shared/garmin_client.py:31-32` chmods `token_dir.parent` to 0700. On the one-time adoption path `token_dir` is `/app/data/.garth` itself, so the parent is `/app/data` — the whole volume root, including `fitness.db`.

**Files:**
- Modify: `shared/garmin_client.py:23-35`, `shared/garmin_registry_runtime.py` (`_move_token_file`)
- Test: `tests/test_garmin_client_api.py`

**Interfaces:**
- Produces: `_ensure_token_dir(token_dir)` creates missing ancestors with mode 0700 but only chmods `token_dir` itself. The registry tightens `person-<id>/` explicitly where it creates it (`_install_staged_token_dir` already does; `_move_token_file` gains the same two lines).

- [ ] **Step 1: Write the failing test** (append to `tests/test_garmin_client_api.py`):

```python
def test_ensure_token_dir_leaves_an_existing_parent_mode_alone(tmp_path):
    from shared.garmin_client import _ensure_token_dir

    data_root = tmp_path / "data"
    data_root.mkdir(mode=0o755)
    token_root = data_root / ".garth"

    _ensure_token_dir(token_root)

    assert oct(data_root.stat().st_mode & 0o777) == oct(0o755), "the volume root must not be tightened"
    assert oct(token_root.stat().st_mode & 0o777) == oct(0o700)


def test_ensure_token_dir_creates_missing_ancestors_privately(tmp_path):
    from shared.garmin_client import _ensure_token_dir

    generation = tmp_path / "garth" / "person-7" / "generation-2"

    _ensure_token_dir(generation)

    assert oct((tmp_path / "garth" / "person-7").stat().st_mode & 0o777) == oct(0o700)
    assert oct(generation.stat().st_mode & 0o777) == oct(0o700)
```

- [ ] **Step 2: Run, expect the first to FAIL** — `.venv/bin/pytest -q tests/test_garmin_client_api.py -k ensure_token_dir` → `0o700 != 0o755`.

- [ ] **Step 3: Implement** — `shared/garmin_client.py`:

```python
def _ensure_token_dir(token_dir: Path) -> Path:
    """Create a token-store directory privately without touching what exists above it.

    Missing ancestors are created 0700 (``mkdir`` honours ``mode`` for
    every level it creates), but an ancestor that already exists is left
    alone: on the one-time legacy adoption ``token_dir`` is the token root
    itself and its parent is the data volume that also holds the database.
    ``exist_ok`` does not tighten an existing ``token_dir``, so that one
    level is chmod'ed explicitly.
    """
    token_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    token_dir.chmod(0o700)
    return token_dir
```

and in `shared/garmin_registry_runtime.py::_move_token_file` make the registry own the person root it creates:

```python
def _move_token_file(source: Path, target: Path) -> None:
    """Move the flat store into its private generation directory atomically."""
    garmin_client._ensure_token_dir(target.parent)
    target.parent.parent.chmod(0o700)  # person-<id>/ is registry-owned; tighten it even if it pre-existed
    os.replace(source, target)
```

- [ ] **Step 4: Run** — `.venv/bin/pytest -q tests/test_garmin_client_api.py tests/test_garmin_registry.py` → pass (the adoption tests check `person-N` is 0700; confirm one of them asserts it, else add `assert oct((root / f"person-{person_id}").stat().st_mode & 0o777) == oct(0o700)` to `test_bootstrap_adopts_a_verified_flat_store_once_by_moving_it`).

- [ ] **Step 5: Commit**

```bash
git add shared/garmin_client.py shared/garmin_registry_runtime.py tests/test_garmin_client_api.py tests/test_garmin_registry.py
git commit -m "fix: stop tightening the data volume root when the legacy token store is verified"
```

---

## Task 4: Accept `acknowledge_garmin_reassignment` for one release

`UpdatePersonIn` has `extra="forbid"`; the field was removed, so a script written against the old 409 guidance now 422s on the whole PATCH.

**Files:**
- Modify: `shared/persons_admin.py:74-91`
- Test: `tests/test_persons_admin.py`

- [ ] **Step 1: Write the failing test** (append; reuse the file's existing admin-client helpers — read its promotion test and copy its setup verbatim):

```python
async def test_patch_still_accepts_the_retired_acknowledge_flag(client):
    # Setup copied from the existing promotion test in this file (admin user,
    # second person, admin session cookie).  Keep it identical.
    ...
    response = await client.patch(
        f"/api/persons/{second_person_id}",
        json={"is_primary": True, "acknowledge_garmin_reassignment": True},
        cookies=admin_cookies,
    )
    assert response.status_code == 200, response.text
    assert "acknowledge_garmin_reassignment" not in response.json()
```

- [ ] **Step 2: Run, expect FAIL** — 422.

- [ ] **Step 3: Implement** — add to `UpdatePersonIn` after `is_primary`:

```python
    # Retired in Phase 3 (per-person links made the cross-person Garmin
    # reassignment impossible).  Accepted and ignored for one release so a
    # PATCH written against the old 409 guidance ("re-send with
    # acknowledge_garmin_reassignment: true") does not now fail as a whole
    # under extra="forbid".  Remove after the next release.
    acknowledge_garmin_reassignment: bool | None = None
```

Confirm nothing reads it: `grep -rn acknowledge_garmin_reassignment shared vitalforge_*` shows only the model.

- [ ] **Step 4: Run** — `.venv/bin/pytest -q tests/test_persons_admin.py` → pass.

- [ ] **Step 5: Commit**

```bash
git add shared/persons_admin.py tests/test_persons_admin.py
git commit -m "fix: accept the retired acknowledge_garmin_reassignment field for one release"
```

---

## Task 5: One source of truth for the bounded `garmin_error` codes

The same whitelist lives in `shared/migrations.py:72` (`_SAFE_STRENGTH_GARMIN_ERRORS`), `vitalforge_weight/activity_garmin.py:69` (`_SAFE_GARMIN_ERROR_CODES`), and the four-code set in `shared/garmin_routes.py:22` / `shared/garmin_registry_errors.py:12`. Drift is silent: a code added only in `activity_garmin.py` would be rewritten to `unknown` by migration 004 on any database that has not run it yet.

**Files:**
- Modify: `shared/garmin_registry_errors.py`, `shared/migrations.py:71-80`, `vitalforge_weight/activity_garmin.py:62-84`, `vitalforge_weight/activity_routes.py:71`, `shared/garmin_routes.py:22`
- Test: `tests/test_garmin_registry.py` (or a new `tests/test_garmin_error_codes.py`)

**Interfaces:**
- Produces, in `shared/garmin_registry_errors.py`:
  - `REGISTRY_ERROR_CODES: frozenset[str]` = `{"auth_failed", "rate_limited", "network", "unknown"}` (keep `_ERROR_CODES` as an alias for one release).
  - `LEGACY_GARMIN_TARGET_RETIRED_ERROR = "legacy_target_retired"`.
  - `STRENGTH_GARMIN_ERROR_CODES: tuple[str, ...]` — the 13-entry list, **append-only** (documented).

- [ ] **Step 1: Write the failing test** (new file `tests/test_garmin_error_codes.py`):

```python
"""The bounded garmin_error vocabulary must have exactly one definition."""

from shared import garmin_registry_errors, garmin_routes, migrations
from vitalforge_weight import activity_garmin, activity_routes


def test_activity_module_and_migration_004_share_one_code_set():
    assert activity_garmin._SAFE_GARMIN_ERROR_CODES == frozenset(garmin_registry_errors.STRENGTH_GARMIN_ERROR_CODES)
    assert migrations._SAFE_STRENGTH_GARMIN_ERRORS is garmin_registry_errors.STRENGTH_GARMIN_ERROR_CODES


def test_registry_codes_are_a_subset_of_the_strength_codes():
    assert garmin_registry_errors.REGISTRY_ERROR_CODES <= set(garmin_registry_errors.STRENGTH_GARMIN_ERROR_CODES)
    assert garmin_routes._SAFE_AUTH_ERRORS is garmin_registry_errors.REGISTRY_ERROR_CODES


def test_retired_target_code_has_one_definition():
    assert activity_routes._LEGACY_GARMIN_TARGET_RETIRED_ERROR is garmin_registry_errors.LEGACY_GARMIN_TARGET_RETIRED_ERROR
    assert migrations._LEGACY_GARMIN_TARGET_RETIRED_ERROR is garmin_registry_errors.LEGACY_GARMIN_TARGET_RETIRED_ERROR
```

- [ ] **Step 2: Run, expect FAIL** — `AttributeError: STRENGTH_GARMIN_ERROR_CODES`.

- [ ] **Step 3: Implement** — in `shared/garmin_registry_errors.py` add:

```python
REGISTRY_ERROR_CODES = frozenset({"auth_failed", "rate_limited", "network", "unknown"})
_ERROR_CODES = REGISTRY_ERROR_CODES  # alias; remove after the next release

LEGACY_GARMIN_TARGET_RETIRED_ERROR = "legacy_target_retired"

# Every value strength_sessions.garmin_error may hold.  APPEND-ONLY: migration
# 004 rewrites any historic value outside this tuple to 'unknown' on databases
# that have not run it yet, so removing an entry would redact live rows.
STRENGTH_GARMIN_ERROR_CODES: tuple[str, ...] = (
    "auth_failed",
    "link_required",
    "rate_limited",
    "network",
    "unknown",
    LEGACY_GARMIN_TARGET_RETIRED_ERROR,
    "activity_preparation_failed",
    "activity_push_failed",
    "activity_push_outcome_unknown",
    "activity_sets_upload_failed",
    "activity_reconciliation_failed",
    "activity_reconciliation_pending",
    "activity_outcome_record_failed",
)
```

Then replace the local definitions with imports:
- `shared/migrations.py:71-80` → `from shared.garmin_registry_errors import LEGACY_GARMIN_TARGET_RETIRED_ERROR as _LEGACY_GARMIN_TARGET_RETIRED_ERROR, STRENGTH_GARMIN_ERROR_CODES as _SAFE_STRENGTH_GARMIN_ERRORS` (keeps every existing reference in the migration bodies byte-identical — the bodies are not edited).
- `vitalforge_weight/activity_garmin.py:69-83` → `_SAFE_GARMIN_ERROR_CODES = frozenset(STRENGTH_GARMIN_ERROR_CODES)` with the import; keep the seven `_PREPARATION_FAILED…` names as they are (they are the same strings).
- `vitalforge_weight/activity_routes.py:71` → import the shared name.
- `shared/garmin_routes.py:22` → `from shared.garmin_registry_errors import REGISTRY_ERROR_CODES as _SAFE_AUTH_ERRORS`.

Check the import direction: `shared/migrations.py` importing `shared.garmin_registry_errors` must not create a cycle — `garmin_registry_errors.py` imports nothing from `shared` (verify with `grep -n "^from\|^import" shared/garmin_registry_errors.py`).

- [ ] **Step 4: Run** — `.venv/bin/ruff check . && .venv/bin/pytest -q` → pass (migration tests prove 004 still redacts the same set).

- [ ] **Step 5: Commit**

```bash
git add shared/garmin_registry_errors.py shared/migrations.py shared/garmin_routes.py vitalforge_weight/activity_garmin.py vitalforge_weight/activity_routes.py tests/test_garmin_error_codes.py
git commit -m "refactor: define the bounded garmin_error vocabulary once"
```

---

## Task 6: Delete the dead `(person_id, generation)` wrappers in `shared/garmin_client.py`

`push_activity`, `push_activity_sets`, `find_activities_by_date` (`:183-250`) and the eight pull wrappers (`:331-368`) have no production callers — `sync.py` calls client methods inside `call_paced` lambdas and `activity_garmin.py` has its own ContextVar-backed versions. `push_weight(person_id, generation, …)` (`:98-124`) is only called by `tests/test_garmin_mapping.py`.

**Files:**
- Modify: `shared/garmin_client.py`
- Modify: `tests/test_garmin_mapping.py` (retarget to `push_weight_to_client`)
- Test: `tests/test_garmin_client_api.py` (keep; it asserts library signatures, not the wrappers)

- [ ] **Step 1: Prove they are dead** — run and paste the output into the commit body:

```bash
for n in push_activity push_activity_sets find_activities_by_date get_sleep_data get_user_summary get_hrv_data get_body_battery get_stress_data get_max_metrics get_weight_range get_training_status; do
  printf '%s: ' "$n"; grep -rn "garmin_client\.$n\b" shared vitalforge_weight vitalforge_dashboard tests | grep -v "^shared/garmin_client.py" | wc -l
done
```
Expected: every count is 0 (a docstring mention in `activity_garmin.py:253` is text, not a call — fix that docstring in this task: it should say tests patch `activity_garmin.push_activity`).

- [ ] **Step 2: Retarget the mapping tests** — in `tests/test_garmin_mapping.py` replace every `garmin_client.push_weight(1, 1, 81600, …)` with `garmin_client.push_weight_to_client(fake_garmin_client, 81600, None, …)` (the fixture already returns the fake; the positional-call test becomes `push_weight_to_client(fake_garmin_client, 81600, None)`), and update the module docstring's first line. Run `.venv/bin/pytest -q tests/test_garmin_mapping.py` → pass *before* deleting anything.

- [ ] **Step 3: Delete** `push_weight` (`:98-124`), the `push_activity`/`push_activity_sets`/`find_activities_by_date` block, and the "Pull methods" section in `shared/garmin_client.py`. Keep `STRENGTH_ACTIVITY_TYPE_KEY`, `_ensure_token_dir`, `authenticate`, `is_authenticated`, `get_client`, `forget`, `forget_stale_generations`, `push_weight_to_client`, `extract_activity_id`, `build_exercise_sets_payload`.

- [ ] **Step 4: Run** — `.venv/bin/ruff check . && .venv/bin/pytest -q` → pass; `grep -rn "monkeypatch.setitem(garmin_client._clients, (1, 1)" tests/conftest.py` — that shortcut existed only for the deleted `push_weight`; remove the two lines and their comment if nothing else fails without them (run the suite again to confirm).

- [ ] **Step 5: Commit**

```bash
git add shared/garmin_client.py tests/test_garmin_mapping.py tests/conftest.py vitalforge_weight/activity_garmin.py
git commit -m "refactor: drop the unused person/generation Garmin wrappers"
```

---

## Task 7: Run synchronous Garmin operations off the event loop

`_run_operation` calls `op(client)` on the loop; every garminconnect request (a blocking `requests` call, with the library's own retries up to ~10 s) freezes the whole service. The justification comment ("existing write-race reasoning relies on that behavior") is stale — `weight_routes.py:426-448` documents that the `garmin_claimed_at` claim, not loop blocking, serialises duplicate pushes.

**Files:**
- Modify: `shared/garmin_registry.py` (`_run_operation`, the stale comment in `_resume_link`)
- Test: `tests/test_garmin_registry.py`

**Interfaces:**
- Produces: a synchronous `op` runs in `asyncio.to_thread` (contextvars are copied, so the `_operation_client` ContextVar seams in `weight_routes`/`activity_garmin` still work because they set/reset *inside* `op`); an awaitable result is awaited on the loop as today.

- [ ] **Step 1: Write the failing test**:

```python
async def test_blocking_operation_does_not_freeze_the_event_loop(initialized_db):
    person_id = await get_primary_person_id()
    await _link(person_id)
    garmin_client._clients[(person_id, 1)] = _FakeClient(1)
    started = threading.Event()
    release = threading.Event()
    ticks = 0

    def blocking_op(_client):
        started.set()
        release.wait(timeout=5)
        return "done"

    async def ticker():
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    tick_task = asyncio.create_task(ticker())
    call_task = asyncio.create_task(garmin_registry.call(person_id, blocking_op))
    await asyncio.to_thread(started.wait, 5)
    await asyncio.sleep(0.1)  # the loop must keep ticking while the op blocks
    release.set()
    assert await call_task == "done"
    await tick_task
    assert ticks >= 5, f"event loop was blocked by the synchronous op (ticks={ticks})"
```

Add `import threading` at the top of the test file.

- [ ] **Step 2: Run, expect FAIL** — on the current code the ticker cannot run while `op` blocks on the loop; the `started.wait` in a thread also never returns until `release`… make sure the test *fails* (times out at 5 s, or `ticks == 0`) rather than hanging: `.venv/bin/pytest -q tests/test_garmin_registry.py -k freeze --timeout 30` (install `pytest-timeout` in the venv if absent; do not add it to requirements).

- [ ] **Step 3: Implement** — in `_run_operation` replace `result = op(client)` with:

```python
        result = await asyncio.to_thread(op, client)
        if inspect.isawaitable(result):
            result = await result
```

`asyncio.to_thread` copies the current context, so an `op` that sets a ContextVar and resets it inside its own body behaves identically. Delete the three-line "Login alone moves off the event loop… this change does not widen it" comment in `_resume_link`. Update `call()`'s docstring sentence "can be synchronous (the normal garminconnect case) or async" to add "a synchronous op runs in a worker thread so the service keeps serving requests during the provider round-trip".

- [ ] **Step 4: Run the full suite** — `.venv/bin/pytest -q` → pass. Pay attention to `tests/test_weight_api.py`, `tests/test_activity_*.py` (they use the conftest `fake_call`, which bypasses the registry — unaffected) and every registry test that passes a synchronous fake `op` (now threaded; any test that relied on `op` mutating loop-bound state synchronously will show up here — fix the test, not the threading).

- [ ] **Step 5: Commit**

```bash
git add shared/garmin_registry.py tests/test_garmin_registry.py
git commit -m "perf: run synchronous Garmin operations in a worker thread"
```

---

## Task 8: Throttle failed step-up attempts per user (optional; do last)

`auth._require_step_up` runs scrypt with no attempt limit; a stolen `vf_session` cookie could brute-force the account password against `/api/garmin/{link,relink,unlink}` and the existing password-bearing admin routes. Same posture as login today, so this is hardening, not a regression; keep it small and in-process (no schema).

**Files:**
- Modify: `shared/auth.py:504-511`
- Test: `tests/test_auth_step_up.py` (new)

**Interfaces:**
- Produces: `_STEP_UP_FAILURE_LIMIT = 5`, `_STEP_UP_WINDOW_SECONDS = 15 * 60`, module-level `_step_up_failures: dict[int, deque[float]]`; `_require_step_up` raises `HTTPException(429, detail="Too many attempts", headers={"Retry-After": ...})` once a user has 5 failures inside the window, *before* running scrypt; a success clears the user's entry. Tests reset the dict via a fixture.

- [ ] **Step 1: Write the failing test**:

```python
import pytest
from fastapi import HTTPException

from shared import auth
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def _reset_step_up_window():
    auth._step_up_failures.clear()
    yield
    auth._step_up_failures.clear()


async def test_sixth_failed_step_up_in_a_window_is_throttled_before_scrypt(initialized_db, monkeypatch):
    user_id = await seed_user("owner", "correct horse")
    identity = auth._Identity("owner", user_id, 1, "admin", "cookie")
    now = 10_000.0
    monkeypatch.setattr(auth.time, "time", lambda: now)
    scrypt_calls = []
    real = auth._authenticate_credentials

    async def counting(username, password):
        scrypt_calls.append(username)
        return await real(username, password)

    monkeypatch.setattr(auth, "_authenticate_credentials", counting)

    for _ in range(5):
        with pytest.raises(HTTPException) as exc:
            await auth._require_step_up(identity, "wrong")
        assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        await auth._require_step_up(identity, "correct horse")
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"].isdigit()
    assert len(scrypt_calls) == 5, "the throttled attempt must not reach scrypt"

    now += 15 * 60 + 1
    await auth._require_step_up(identity, "correct horse")  # window expired: success, and it clears the entry
    assert user_id not in auth._step_up_failures
```

Check `shared/auth.py` already imports `time`; if not, add `import time` and `from collections import deque`.

- [ ] **Step 2: Run, expect FAIL** — the sixth call returns 200-equivalent (no exception) and `scrypt_calls == 6`.

- [ ] **Step 3: Implement** — replace `_require_step_up`:

```python
_STEP_UP_FAILURE_LIMIT = 5
_STEP_UP_WINDOW_SECONDS = 15 * 60
# Per-process, per-user timestamps of failed step-ups.  In-memory on purpose:
# a step-up already requires a valid session, so this only slows a stolen
# cookie down; the durable per-user window in garmin_link_attempts still
# bounds what a successful step-up can do with Garmin.
_step_up_failures: dict[int, deque[float]] = {}


def _step_up_retry_after(user_id: int, now: float) -> int | None:
    failures = _step_up_failures.get(user_id)
    if not failures:
        return None
    while failures and failures[0] <= now - _STEP_UP_WINDOW_SECONDS:
        failures.popleft()
    if len(failures) < _STEP_UP_FAILURE_LIMIT:
        return None
    return max(1, int(failures[0] + _STEP_UP_WINDOW_SECONDS - now) + 1)


async def _require_step_up(identity: _Identity, current_password: str):
    if identity.user_id is None:
        raise HTTPException(status_code=401, detail="Current password incorrect")
    now = time.time()
    retry_after = _step_up_retry_after(identity.user_id, now)
    if retry_after is not None:
        raise HTTPException(status_code=429, detail="Too many attempts", headers={"Retry-After": str(retry_after)})
    verified = await _authenticate_credentials(identity.username, current_password)
    if (
        verified is None
        or verified[0] != identity.user_id
        or verified[1] != identity.session_version
    ):
        _step_up_failures.setdefault(identity.user_id, deque()).append(now)
        raise HTTPException(status_code=401, detail="Current password incorrect")
    _step_up_failures.pop(identity.user_id, None)
```

- [ ] **Step 4: Run** — `.venv/bin/pytest -q tests/test_auth_step_up.py tests/test_garmin_routes.py tests/test_persons_admin.py tests/test_user_management.py` → pass (existing step-up tests use distinct users or succeed; if one hits 429 because it deliberately fails >5 times, add the reset fixture to that file).

- [ ] **Step 5: Commit**

```bash
git add shared/auth.py tests/test_auth_step_up.py
git commit -m "fix: throttle repeated failed step-up password checks per user"
```

---

## Task 9: Full verification and a fresh re-review of tonight's commits

- [ ] **Step 1:** `.venv/bin/ruff check .` → clean. `.venv/bin/pytest -q` → 0 failures. `.venv/bin/pytest -q -m playwright` (separate process) → 4 passed.
- [ ] **Step 2:** `git diff --stat 055b2aa..HEAD` and read every hunk once, looking for stale comments that still describe pre-fix behaviour (`grep -rn "legacy_bound\|tombstone\|does not widen" shared vitalforge_weight vitalforge_dashboard` should return only the CHECK comment in `shared/database.py` and the "retired" notes).
- [ ] **Step 3:** Dispatch two fresh-context reviewers on `055b2aa..HEAD` only — `security-reviewer` (Tasks 1, 2, 3, 8: eviction correctness, deadline arithmetic, chmod scope, throttle bypass) and `code-reviewer` (Tasks 5, 6, 7: import direction, dead-code proof, threading + ContextVar, and a mutation of Task 7's test). Both write findings to the scratchpad and reply with a verdict.
- [ ] **Step 4:** Fix anything CRITICAL/HIGH in its own commit; re-run Step 1.

---

## Task 10: Push, CI, merge — user gates

- [ ] **Step 1:** Ask the user: "push `feat/per-person-garmin-links` (N commits since `db7a9bd`)?" — only a plain yes pushes: `git push origin feat/per-person-garmin-links`.
- [ ] **Step 2:** Watch CI: `gh pr checks 63 --watch`. The `test` job runs ruff → pip-audit → pytest → playwright; a red job blocks the image push.
- [ ] **Step 3:** Update the PR body with a "Review follow-ups" section: the two reproduced CRITICALs and how they were closed, the migration one-way-door note, and the prod HTTPS prerequisite (`gh pr edit 63 --body-file …`).
- [ ] **Step 4:** Ask the user: "merge #63?" — only an explicit yes: `gh pr merge 63 --merge` (no branch deletion unless asked).

---

## Task 11: Prod — TLS front, then deploy

Prod (`knowledge`, 192.168.1.21 / 100.74.76.39 on the tailnet) exposes :8085/:8086 over plain HTTP with no proxy. Linking a second person there is impossible until HTTPS exists; the primary person keeps working through the one-time adoption.

- [ ] **Step 1 (user decision):** present the two options and get a pick before doing anything:
  - **A) `tailscale serve`** on the box: `tailscale serve --bg --https=8445 http://127.0.0.1:8085` and `--https=8446 http://127.0.0.1:8086` → `https://knowledge.manx-velociraptor.ts.net:8445`. Tailnet-only, real certs, no new container. Then set `VITALFORGE_TRUSTED_PROXY_IPS=127.0.0.1` in prod's `.env` (tailscale serve connects from loopback and sets `X-Forwarded-Proto: https`; **verify the header with a `curl -v` before relying on it**).
  - **B) Caddy container** in the compose file with the existing `nginx/nginx.conf` semantics — more moving parts, LAN-reachable.
- [ ] **Step 2: Backup** (memory-verified procedure): `docker exec vitalforge-vitalforge-weight-1 python -c "import sqlite3; sqlite3.connect('/app/data/fitness.db').execute(\"VACUUM INTO '/app/data/pre.db'\")"` → `docker cp … backups/predeploy-$(date +%Y%m%d-%H%M%S).db` → `docker exec … rm /app/data/pre.db` → `PRAGMA integrity_check` + `sqlite_master` count on the copy (never rows). Keep two snapshots; the release also takes `fitness.pre-003-strength-sessions.db` itself.
- [ ] **Step 3: Record old digests** `docker inspect --format '{{.Image}}' <both>`.
- [ ] **Step 4: Stop both, pull, up:** `cd /home/user/docker/vitalforge && docker compose down && docker compose pull && docker compose up -d`. Wait for `(healthy)` on both (`docker compose ps`; `grep -c "(healthy)" || true` in loops).
- [ ] **Step 5: Verify the release, not the image tag:** `docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' <container>` equals the merge commit; `docker compose logs --since 5m | grep -iE "adopt|legacy|Applied schema migration 00[34]|snapshot"` shows 003/004 applied and exactly one "adopted" line; `ls /app/data/.garth/person-1/generation-1/` (names only) shows `garmin_tokens.json` and `/app/data/.garth/garmin_tokens.json` is gone; `curl -s http://localhost:8086/health`.
- [ ] **Step 6:** Trigger a manual sync for the primary person from the dashboard and confirm `sync_status` advances; nothing else needs a live Garmin call.

---

## Task 12: Issues for what is deliberately not done tonight

- [ ] Open one GitHub issue each (`gh issue create --title … --body …`), labelled `tech-debt`, linking PR #63:
  1. `registry↔runtime` late-binding coupling (`_registry()` shim; move constants/helpers to a leaf module).
  2. Exercise-set upload is never retried once the activity row is `synced` (flag-gated feature).
  3. `run_sync` still runs on the primary person only (Phase 4 scheduling/fairness).
  4. README API tables still list un-prefixed `/api/...` paths from before Phase 2.
  5. `CLAUDE.md`/`AGENTS.md` pre-existing drift ("No test step exists" vs CI's `test` job).
  6. A link UI (the four routes have no frontend caller).

---

## Self-review

- **Coverage:** review items #11 (T1), #7 (T2), #13 (T3), #16/#5 (T4), whitelist duplication (T5), dead wrappers (T6), #18 (T7), #15 (T8), re-review gate (T0/T9), push/merge (T10), HTTPS + deploy (T11), deferred (T12). Exercise-sets second permit (#12) was already closed by `max_wait_seconds=10.0` on `attach_sets` in `ec40462`; only the no-retry-on-synced part remains → T12.
- **Placeholders:** Task 4 Step 1 has a `...` for setup that must be copied from the file's existing promotion test — the executor must paste it verbatim, not invent it. Everything else is concrete.
- **Type consistency:** `_note_operation_failure(person_id: int, generation: int, exc: BaseException) -> str` (T1) is what T1's `except` and value paths both call; `_resume_link(..., *, deadline_seconds: float | None = None)` (T2) matches `call()`'s keyword; `STRENGTH_GARMIN_ERROR_CODES` / `REGISTRY_ERROR_CODES` / `LEGACY_GARMIN_TARGET_RETIRED_ERROR` (T5) are the only new public names and are used with those exact spellings in the imports.

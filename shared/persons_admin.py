"""Admin CRUD over the person *collection* (multi-tenancy Phase 2, PR 3).

Separate from `shared/auth.py` for two reasons. The obvious one is size --
auth.py is already past this project's file-length guidance and the admin page
adds a few hundred lines of HTML. The load-bearing one is that these routes are
the deliberate exception to Phase 2's routing rule: everything person-scoped
lives under `/p/{slug}/` and is authorized by `require_person`, but the person
*collection* is addressed **by id** at the root, because it must reach archived
persons and `require_person` deliberately cannot (design spec f.2, plan
constraint 7). Keeping that exception in one file, named for what it is, is
better than scattering it through the module whose whole subject is the rule.

Both services register these routes, mirroring `add_auth_routes` -- one login
covers both, so an admin who happens to have opened the weight service should
not have to switch ports to add a person.

Authorization, stated once:

* Person CRUD (`create`, `list`, `PATCH`, `archive`) is **admin only**, gated
  by the same `require_admin` every `/auth/admin/*` route already uses. That
  includes the open-access (empty `users` table) mode, where the anonymous
  sentinel's role is `None` and so is refused -- exactly as `/auth/admin/users`
  refuses it today. One superuser story, not two. Open-access mode also has no
  users to grant anything to, so the surface has nothing to do there;
  `scripts/seed_db.py --person` is the supported way to get a second person
  into a development database.
* Grant management is **admin, or `own` on that person** (spec f.6), and
  answers **404** to everyone else -- not 403. Plan constraint 2's reason ("a
  403 confirms the person exists and leaks household membership") applies
  directly to a by-id route over persons. The plan's 403 carve-out is for
  *account-scoped* resources such as goals; a person is not one.

SQLite foreign keys are not enabled in this project (see
`shared/auth.py:admin_delete_user`), so every write here that references
another table's id verifies that row inside the same transaction rather than
trusting a `REFERENCES` clause that does not fire.
"""

import logging
from datetime import datetime, timezone
from typing import Literal

import aiosqlite
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from shared.auth import Identity, get_current_identity, require_admin
from shared.database import get_db
from shared.persons_admin_page import ADMIN_PERSONS_PAGE_HTML
from shared.slugs import RESERVED_SLUGS, SLUG_RE, slugify

logger = logging.getLogger(__name__)


class CreatePersonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    # Optional: omit it and the slug is derived from display_name. Supplying
    # one explicitly is the escape hatch for a display name that slugifies to
    # something unwanted (or to nothing at all, for a name with no ASCII).
    slug: str | None = None


class UpdatePersonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    # `slug` is deliberately NOT patchable. Plan constraint 6 makes slugs
    # globally unique *including archived persons* so that a stale bookmark or
    # a cached service-worker URL can never resolve to a different human --
    # "the worst failure this design can produce". A rename frees the old slug
    # for a later create to claim, which reopens exactly that. Renaming needs a
    # slug-history table that keeps retired slugs permanently reserved; until
    # that exists, slugs are immutable after creation.
    #
    # bool rather than Literal[True] so that `is_primary: false` fails with a
    # sentence explaining why instead of a schema error the caller has to
    # decode. There must always be exactly one primary person --
    # get_primary_person_id() raises when there is none, and scheduled_sync
    # still depends on it through Phase 3 (plan D3).
    is_primary: bool | None = None


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Literal, not a plain str: a typo'd level should be a 422 from the schema,
    # not an IntegrityError from person_grants' CHECK constraint surfacing as a
    # 500.
    access: Literal["view", "manage", "own"]


def _validate_slug(raw: str | None, display_name: str) -> str:
    """The slug that will be minted, or a 422 explaining why none can be.

    This is the first *route* that creates a person. Until now SLUG_RE was
    enforced only in `shared/migrations.py` and `scripts/seed_db.py`, neither
    of which takes external input -- so this is where an unvalidated slug would
    actually enter the system, and where validation belongs.
    """
    if raw is None:
        slug = slugify(display_name)
        if not slug:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Could not derive a URL-safe slug from that display name. "
                    "Supply `slug` explicitly."
                ),
            )
    else:
        slug = raw.strip()
    if not SLUG_RE.match(slug):
        raise HTTPException(
            status_code=422,
            detail=(
                "Slug must be 1-32 characters of lowercase letters, digits and hyphens, "
                "starting and ending with a letter or digit."
            ),
        )
    if slug in RESERVED_SLUGS:
        raise HTTPException(status_code=422, detail=f"'{slug}' is a reserved slug")
    return slug


async def _identity_and_grant_by_id(
    request: Request, person_id: int
) -> tuple[Identity | None, str | None, bool]:
    """Resolve the caller AND their grant on person `person_id` in ONE query.

    The by-id sibling of `shared.auth._identity_and_grant`, and it exists for
    the same reason that one does: two queries (resolve the person, then look
    up the grant) leave a window in which a grant revoked between them still
    authorizes the request (plan constraint 3).

    Two deliberate differences from the slug version:

    * **No `archived_at IS NULL`.** Grants on an archived person must stay
      manageable -- an archived person is unreachable through `require_person`
      by design, which is exactly why the admin surface addresses by id.
    * It returns whether the person **exists** rather than its id, because the
      caller already has the id. `require_person`'s "no person_id without
      authorizing" rule (constraint 1) is about *request paths obtaining a
      subject*; this function authorizes and returns no new subject.

    Returns `(identity, access-or-None, person-exists)`. `identity` is None
    only when auth is configured and the caller presented nothing valid.
    """
    identity = await get_current_identity(request)
    if identity is None:
        return None, None, False
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT p.id AS person_id, g.access AS access "
                "FROM persons p "
                "LEFT JOIN person_grants g "
                "  ON g.person_id = p.id AND g.user_id = ? "
                "WHERE p.id = ?",
                (identity.user_id, person_id),
            )
        ).fetchone()
    finally:
        await db.close()
    if row is None:
        return identity, None, False
    return identity, row["access"], True


async def _require_person_owner(request: Request, person_id: int) -> Identity:
    """Authorize a grant-management request: admin, or `own` on this person.

    404 for everyone else, including "no such person" -- the two answers are
    identical on purpose (plan constraint 2). A 403 here would confirm the
    person exists to any logged-in household member who guessed an id, which
    is the leak the 404 convention closes.

    The anonymous sentinel of open-access mode lands in the 404 branch: it has
    no user_id, so it can hold no grant, and it is not an admin. That mode has
    an empty `users` table and therefore nobody to grant anything to, so the
    surface is unreachable rather than mis-authorized -- see this module's
    docstring.
    """
    identity, access, exists = await _identity_and_grant_by_id(request, person_id)
    if identity is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not exists:
        raise HTTPException(status_code=404, detail="Person not found")
    if identity.role == "admin":
        return identity
    if access == "own":
        return identity
    raise HTTPException(status_code=404, detail="Person not found")


def add_person_routes(app):
    """Register the person-collection admin surface on a FastAPI app."""

    @app.get("/auth/admin/persons")
    async def admin_persons_page(request: Request):
        await require_admin(request)
        return HTMLResponse(ADMIN_PERSONS_PAGE_HTML)

    @app.get("/api/persons")
    async def list_persons(request: Request):
        """Every person, archived included -- this is the surface that must be
        able to see them (spec f.2). `grant_count` is here so the admin page
        can show the zero-grant state, which is a reachable and deliberate one
        (spec f.6): a person whose last grant was revoked, or whose only
        grantee was deleted, is reachable by admins and by nobody else."""
        await require_admin(request)
        db = await get_db()
        try:
            rows = await (
                await db.execute(
                    "SELECT p.id, p.slug, p.display_name, p.created_at, p.archived_at, "
                    "p.is_primary, COUNT(g.user_id) AS grant_count "
                    "FROM persons p "
                    "LEFT JOIN person_grants g ON g.person_id = p.id "
                    "GROUP BY p.id "
                    "ORDER BY p.archived_at IS NOT NULL, p.slug"
                )
            ).fetchall()
        finally:
            await db.close()
        return [dict(row) for row in rows]

    @app.post("/api/persons")
    async def create_person(request: Request, data: CreatePersonIn):
        """Create a person; the creating admin gets an automatic `own` grant
        (spec f.6). Without it a freshly created person would be reachable only
        through the admin bypass, and the creator's own landing page would not
        offer them."""
        identity = await require_admin(request)
        display_name = data.display_name.strip()
        if not display_name:
            raise HTTPException(status_code=422, detail="display_name is required")
        slug = _validate_slug(data.slug, display_name)
        now = datetime.now(timezone.utc).isoformat()
        db = await get_db()
        try:
            # BEGIN IMMEDIATE so the person and the creator's grant land
            # together: a crash between them leaves a person nobody but an
            # admin can open, which is a state this API otherwise only reaches
            # deliberately.
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "INSERT INTO persons (slug, display_name, created_at) VALUES (?, ?, ?)",
                    (slug, display_name, now),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                # UNIQUE on persons.slug, and archived persons keep their row
                # -- so this is also what enforces constraint 6's "an archived
                # person's slug is permanently taken".
                raise HTTPException(status_code=409, detail=f"Slug '{slug}' is already taken")
            person_id = cursor.lastrowid
            await db.execute(
                "INSERT INTO person_grants (person_id, user_id, access, granted_at, granted_by) "
                "VALUES (?, ?, 'own', ?, ?)",
                (person_id, identity.user_id, now, identity.user_id),
            )
            await db.commit()
        finally:
            await db.close()
        return {"id": person_id, "slug": slug, "display_name": display_name}

    @app.patch("/api/persons/{person_id}")
    async def update_person(request: Request, person_id: int, data: UpdatePersonIn):
        """Rename (display name only) and promote to primary.

        See UpdatePersonIn for why `slug` is not patchable.
        """
        await require_admin(request)
        if data.is_primary is False:
            raise HTTPException(
                status_code=422,
                detail=(
                    "There must always be exactly one primary person. Promote another "
                    "person with is_primary: true instead of demoting this one."
                ),
            )
        display_name = data.display_name.strip() if data.display_name is not None else None
        if data.display_name is not None and not display_name:
            raise HTTPException(status_code=422, detail="display_name cannot be blank")
        if display_name is None and data.is_primary is None:
            raise HTTPException(status_code=422, detail="Nothing to update")
        db = await get_db()
        try:
            # BEGIN IMMEDIATE for the promotion: `idx_persons_primary` is a
            # partial UNIQUE index over is_primary = 1, so the swap has to
            # clear the old primary before setting the new one, and two
            # concurrent promotions interleaved between those statements would
            # otherwise leave zero primaries -- which makes
            # get_primary_person_id() raise for scheduled_sync.
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT archived_at, is_primary FROM persons WHERE id = ?", (person_id,)
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise HTTPException(status_code=404, detail="Person not found")
            if data.is_primary and row["archived_at"] is not None:
                await db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="An archived person cannot be made primary.",
                )
            if display_name is not None:
                await db.execute(
                    "UPDATE persons SET display_name = ? WHERE id = ?", (display_name, person_id)
                )
            if data.is_primary and not row["is_primary"]:
                await db.execute("UPDATE persons SET is_primary = 0 WHERE is_primary = 1")
                await db.execute("UPDATE persons SET is_primary = 1 WHERE id = ?", (person_id,))
            await db.commit()
        finally:
            await db.close()
        return {"success": True}

    @app.post("/api/persons/{person_id}/archive")
    async def archive_person(request: Request, person_id: int):
        """Archive, never delete (spec f.6). Deleting a person means deleting
        years of health data across 11 tables with no FK cascade to help.

        Archiving is idempotent: re-archiving returns the original
        `archived_at` rather than moving it, so a double-click cannot rewrite
        history.
        """
        await require_admin(request)
        db = await get_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT archived_at, is_primary FROM persons WHERE id = ?", (person_id,)
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise HTTPException(status_code=404, detail="Person not found")
            if row["archived_at"] is not None:
                await db.rollback()
                return {"success": True, "archived_at": row["archived_at"]}
            if row["is_primary"]:
                await db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The primary person cannot be archived. Promote another person "
                        "first (PATCH /api/persons/{id} with is_primary: true), then "
                        "archive this one."
                    ),
                )
            archived_at = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "UPDATE persons SET archived_at = ? WHERE id = ?", (archived_at, person_id)
            )
            # An archived person drops out of _reachable_persons in both
            # services, so a default_person_id still pointing at them makes
            # GET / fall through to the "more than one person, no default" 400
            # -- a dead end a non-admin cannot clear for themselves. Same
            # reasoning as NULLing person_grants.granted_by on user delete:
            # a pointer to a row that can no longer be reached is worse than
            # no pointer.
            await db.execute(
                "UPDATE users SET default_person_id = NULL WHERE default_person_id = ?",
                (person_id,),
            )
            await db.commit()
        finally:
            await db.close()
        return {"success": True, "archived_at": archived_at}

    @app.get("/api/persons/{person_id}/grants")
    async def list_grants(request: Request, person_id: int):
        await _require_person_owner(request, person_id)
        db = await get_db()
        try:
            rows = await (
                await db.execute(
                    "SELECT g.user_id, u.username, g.access, g.granted_at, "
                    "g.granted_by, b.username AS granted_by_username "
                    "FROM person_grants g "
                    "JOIN users u ON u.id = g.user_id "
                    "LEFT JOIN users b ON b.id = g.granted_by "
                    "WHERE g.person_id = ? ORDER BY u.username",
                    (person_id,),
                )
            ).fetchall()
        finally:
            await db.close()
        return [dict(row) for row in rows]

    @app.put("/api/persons/{person_id}/grants/{user_id}")
    async def upsert_grant(request: Request, person_id: int, user_id: int, data: GrantIn):
        """Grant or change one user's access to one person. Idempotent by id
        pair, so re-issuing at a different level is how a level is changed."""
        actor = await _require_person_owner(request, person_id)
        db = await get_db()
        try:
            # The target user is verified INSIDE the transaction because SQLite
            # foreign keys are off in this project: person_grants' REFERENCES
            # clause does not fire, so a grant to a nonexistent (or
            # concurrently deleted) user would otherwise persist as a dangling
            # row whose user_id an AUTOINCREMENT reuse could later resurrect --
            # the same hazard admin_delete_user's cascade exists to prevent,
            # arriving from the other side.
            await db.execute("BEGIN IMMEDIATE")
            target = await (
                await db.execute("SELECT id FROM users WHERE id = ?", (user_id,))
            ).fetchone()
            if target is None:
                await db.rollback()
                raise HTTPException(status_code=404, detail="User not found")
            await db.execute(
                "INSERT INTO person_grants (person_id, user_id, access, granted_at, granted_by) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(person_id, user_id) DO UPDATE SET "
                "access = excluded.access, granted_at = excluded.granted_at, "
                "granted_by = excluded.granted_by",
                (
                    person_id,
                    user_id,
                    data.access,
                    datetime.now(timezone.utc).isoformat(),
                    actor.user_id,
                ),
            )
            await db.commit()
        finally:
            await db.close()
        return {"success": True}

    @app.delete("/api/persons/{person_id}/grants/{user_id}")
    async def revoke_grant(request: Request, person_id: int, user_id: int):
        """Revoke one user's access to one person.

        **Revoking your own last `own` grant is permitted** (spec f.6), and is
        deliberately NOT modeled on `admin_update_user`'s "cannot demote the
        last remaining admin" guard. That guard exists because there is no
        higher authority to recover from zero admins; here there is -- any
        admin can re-grant. The asymmetry is intentional, not an oversight.

        **A person with zero grants is a reachable, deliberate state**, for the
        same reason and also because deleting a *user* cascades their grants
        away. The rejected alternative -- "cannot delete the last grant" --
        would make deleting a user fail for reasons an admin cannot see from
        the users page. A zero-grant person stays reachable by any admin.
        """
        await _require_person_owner(request, person_id)
        db = await get_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "DELETE FROM person_grants WHERE person_id = ? AND user_id = ?",
                (person_id, user_id),
            )
            if cursor.rowcount == 0:
                await db.rollback()
                raise HTTPException(status_code=404, detail="Grant not found")
            # A revoked grant can leave default_person_id pointing at a person
            # this user can no longer reach -- the same dead end archiving
            # creates, from the other direction.
            await db.execute(
                "UPDATE users SET default_person_id = NULL "
                "WHERE id = ? AND default_person_id = ?",
                (user_id, person_id),
            )
            await db.commit()
        finally:
            await db.close()
        return {"success": True}

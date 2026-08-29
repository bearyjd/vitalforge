"""The /auth/admin/persons page.

A module of its own only so shared/persons_admin.py stays about routing and
authorization. The markup deliberately mirrors ADMIN_USERS_PAGE_HTML in
shared/auth.py -- same palette, same box, same table shape -- rather than
introducing a second admin house style ahead of Phase 5's UI work (plan D7).

XSS: this page is a raw HTMLResponse with client-side rendering, so there is no
Jinja autoescape backstop. `display_name` is the untrusted field -- unlike
`slug` it is arbitrary TEXT with no SLUG_RE to constrain it, and an admin
typing a name is still typing into a field that renders back to other admins.
Every cell built from server data therefore uses textContent or option.value,
never innerHTML, exactly as the users page's load-bearing comment says.
"""

ADMIN_PERSONS_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#1a1a2e">
    <title>VitalForge People</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #e0e0e0;
            min-height: 100vh;
            padding: 2rem;
        }
        .box {
            background: #16213e;
            border-radius: 12px;
            padding: 2rem;
            max-width: 760px;
            margin: 0 auto;
        }
        h1 { font-size: 1.3rem; color: #c0c0e0; margin-bottom: 1.5rem; }
        h2 { font-size: 1rem; color: #c0c0e0; margin: 1.5rem 0 0.8rem; }
        table { width: 100%; border-collapse: collapse; margin-bottom: 1rem; }
        th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #2a2a4a; font-size: 0.9rem; }
        input, select {
            width: 100%;
            padding: 0.7rem;
            margin-bottom: 0.8rem;
            border: 1px solid #2a2a4a;
            border-radius: 6px;
            background: #1a1a2e;
            color: #e0e0e0;
            font-size: 0.95rem;
        }
        button {
            padding: 0.5rem 1rem;
            background: #5c6bc0;
            color: #fff;
            border: none;
            border-radius: 6px;
            font-size: 0.85rem;
            cursor: pointer;
        }
        button:hover { background: #7c4dff; }
        button.danger { background: #ef5350; }
        button.danger:hover { background: #e53935; }
        .error { color: #ef5350; font-size: 0.85rem; margin-bottom: 0.8rem; }
        .success { color: #66bb6a; font-size: 0.85rem; margin-bottom: 0.8rem; }
        .muted { color: #8a8ab0; font-size: 0.8rem; }
        .archived td { opacity: 0.55; }
        a { color: #5c6bc0; text-decoration: none; }
    </style>
</head>
<body>
    <div class="box">
        <h1>Manage People</h1>
        <div class="error" id="error"></div>
        <div class="success" id="success"></div>
        <table>
            <thead><tr>
                <th>Slug</th><th>Name</th><th>Grants</th><th>Status</th><th></th>
            </tr></thead>
            <tbody id="persons-body"></tbody>
        </table>
        <p class="muted">A person with no grants is reachable by administrators only. That is a
        deliberate state, not an error &mdash; deleting a user removes their grants, and the last
        <code>own</code> grant may be revoked. Any admin can restore access.</p>
        <p class="muted">Slugs appear in every URL and cannot be changed after creation: freeing a
        slug would let a stale bookmark resolve to a different person.</p>

        <h2>Add Person</h2>
        <form onsubmit="return createPerson(event)">
            <input type="text" id="new-display-name" placeholder="Display name" required>
            <input type="text" id="new-slug" placeholder="Slug (optional &mdash; derived from the name)">
            <button type="submit">Create</button>
        </form>

        <h2 id="grants-heading">Access</h2>
        <p class="muted" id="grants-subject">Select a person above to manage who can see their data.</p>
        <table>
            <thead><tr><th>User</th><th>Access</th><th>Granted by</th><th></th></tr></thead>
            <tbody id="grants-body"></tbody>
        </table>
        <form id="grant-form" style="display:none" onsubmit="return addGrant(event)">
            <select id="grant-user"></select>
            <select id="grant-access">
                <option value="view">view</option>
                <option value="manage">manage</option>
                <option value="own">own</option>
            </select>
            <button type="submit">Grant</button>
        </form>

        <p style="margin-top:1rem"><a href="/auth/admin/users">Users</a> &middot; <a href="/">Back</a></p>
    </div>
    <script>
        // Every cell built from server data uses textContent/option.value,
        // never innerHTML -- a display name is untrusted input as far as this
        // page is concerned, and innerHTML would execute markup in it.
        let selectedPerson = null;

        function setError(message) { document.getElementById("error").textContent = message; }
        function setSuccess(message) { document.getElementById("success").textContent = message; }
        function clearMessages() { setError(""); setSuccess(""); }

        async function failureDetail(res, fallback) {
            try {
                const body = await res.json();
                return body.detail || fallback;
            } catch (e) {
                return fallback;
            }
        }

        function cell(text) {
            const td = document.createElement("td");
            td.textContent = text;
            return td;
        }

        async function loadPersons() {
            const res = await fetch("/api/persons");
            if (!res.ok) {
                setError(await failureDetail(res, "Failed to load people."));
                return;
            }
            const persons = await res.json();
            const body = document.getElementById("persons-body");
            body.textContent = "";
            for (const p of persons) {
                const row = document.createElement("tr");
                if (p.archived_at) row.className = "archived";

                row.appendChild(cell(p.slug));
                row.appendChild(cell(p.display_name));
                row.appendChild(cell(String(p.grant_count)));
                row.appendChild(cell(
                    p.archived_at ? "archived" : (p.is_primary ? "primary" : "active")
                ));

                const actions = document.createElement("td");

                const accessBtn = document.createElement("button");
                accessBtn.type = "button";
                accessBtn.textContent = "Access";
                accessBtn.onclick = () => selectPerson(p);
                actions.appendChild(accessBtn);

                const renameBtn = document.createElement("button");
                renameBtn.type = "button";
                renameBtn.textContent = "Rename";
                renameBtn.onclick = () => renamePerson(p);
                actions.appendChild(renameBtn);

                if (!p.archived_at && !p.is_primary) {
                    const promoteBtn = document.createElement("button");
                    promoteBtn.type = "button";
                    promoteBtn.textContent = "Make primary";
                    promoteBtn.onclick = () => promotePerson(p);
                    actions.appendChild(promoteBtn);

                    const archiveBtn = document.createElement("button");
                    archiveBtn.type = "button";
                    archiveBtn.className = "danger";
                    archiveBtn.textContent = "Archive";
                    archiveBtn.onclick = () => archivePerson(p);
                    actions.appendChild(archiveBtn);
                }

                row.appendChild(actions);
                body.appendChild(row);
            }
        }

        async function createPerson(e) {
            e.preventDefault();
            clearMessages();
            const slug = document.getElementById("new-slug").value.trim();
            const payload = { display_name: document.getElementById("new-display-name").value };
            if (slug) payload.slug = slug;
            const res = await fetch("/api/persons", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                setSuccess("Person created.");
                document.getElementById("new-display-name").value = "";
                document.getElementById("new-slug").value = "";
                loadPersons();
            } else {
                setError(await failureDetail(res, "Failed to create person."));
            }
            return false;
        }

        async function patchPerson(id, payload, okMessage, failMessage) {
            clearMessages();
            const res = await fetch(`/api/persons/${id}`, {
                method: "PATCH",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                setSuccess(okMessage);
            } else {
                setError(await failureDetail(res, failMessage));
            }
            loadPersons();
        }

        async function renamePerson(p) {
            const name = prompt("New display name:", p.display_name);
            if (!name) return;
            patchPerson(p.id, {display_name: name}, "Renamed.", "Failed to rename.");
        }

        async function promotePerson(p) {
            if (!confirm(
                `Make ${p.display_name} the primary person? Scheduled Garmin syncs follow the ` +
                `primary person until per-person Garmin linking ships.`
            )) return;
            patchPerson(p.id, {is_primary: true}, "Primary person changed.", "Failed to promote.");
        }

        async function archivePerson(p) {
            if (!confirm(
                `Archive ${p.display_name}? Their data is kept, but the person disappears from ` +
                `every dashboard and their URL stops resolving. Slugs are never reused.`
            )) return;
            clearMessages();
            const res = await fetch(`/api/persons/${p.id}/archive`, { method: "POST" });
            if (res.ok) {
                setSuccess("Person archived.");
                if (selectedPerson && selectedPerson.id === p.id) selectPerson(p);
            } else {
                setError(await failureDetail(res, "Failed to archive person."));
            }
            loadPersons();
        }

        async function selectPerson(p) {
            selectedPerson = p;
            document.getElementById("grants-subject").textContent =
                `Who can see ${p.display_name}'s data`;
            document.getElementById("grant-form").style.display = "block";
            await loadUserOptions();
            await loadGrants();
        }

        async function loadUserOptions() {
            const res = await fetch("/auth/admin/users/list");
            const select = document.getElementById("grant-user");
            select.textContent = "";
            if (!res.ok) return;
            for (const u of await res.json()) {
                const opt = document.createElement("option");
                opt.value = String(u.id);
                opt.textContent = u.username;
                select.appendChild(opt);
            }
        }

        async function loadGrants() {
            const body = document.getElementById("grants-body");
            body.textContent = "";
            if (!selectedPerson) return;
            const res = await fetch(`/api/persons/${selectedPerson.id}/grants`);
            if (!res.ok) {
                setError(await failureDetail(res, "Failed to load access."));
                return;
            }
            for (const g of await res.json()) {
                const row = document.createElement("tr");
                row.appendChild(cell(g.username));

                const accessCell = document.createElement("td");
                const select = document.createElement("select");
                for (const level of ["view", "manage", "own"]) {
                    const opt = document.createElement("option");
                    opt.value = level;
                    opt.textContent = level;
                    if (level === g.access) opt.selected = true;
                    select.appendChild(opt);
                }
                select.onchange = () => setGrant(g.user_id, select.value);
                accessCell.appendChild(select);
                row.appendChild(accessCell);

                row.appendChild(cell(g.granted_by_username || "\\u2014"));

                const actions = document.createElement("td");
                const revokeBtn = document.createElement("button");
                revokeBtn.type = "button";
                revokeBtn.className = "danger";
                revokeBtn.textContent = "Revoke";
                revokeBtn.onclick = () => revokeGrant(g.user_id);
                actions.appendChild(revokeBtn);
                row.appendChild(actions);

                body.appendChild(row);
            }
        }

        async function setGrant(userId, access) {
            clearMessages();
            const res = await fetch(`/api/persons/${selectedPerson.id}/grants/${userId}`, {
                method: "PUT",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({access: access})
            });
            if (!res.ok) setError(await failureDetail(res, "Failed to set access."));
            // Re-render either way, so a rejected change reverts the dropdown.
            loadGrants();
            loadPersons();
        }

        async function addGrant(e) {
            e.preventDefault();
            const userId = document.getElementById("grant-user").value;
            if (!userId) return false;
            await setGrant(userId, document.getElementById("grant-access").value);
            return false;
        }

        async function revokeGrant(userId) {
            clearMessages();
            const res = await fetch(`/api/persons/${selectedPerson.id}/grants/${userId}`, {
                method: "DELETE"
            });
            if (!res.ok) setError(await failureDetail(res, "Failed to revoke access."));
            loadGrants();
            loadPersons();
        }

        loadPersons();
    </script>
</body>
</html>"""

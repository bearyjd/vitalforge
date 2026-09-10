"""The HTML for the three auth pages.

A module of its own for the same reason shared/persons_admin_page.py is one:
so shared/auth.py stays about sessions, identity and authorization rather than
markup. These are 507 of that file's lines and none of them are logic.

XSS: like persons_admin_page.py these are raw HTMLResponse bodies with
client-side rendering, so there is no Jinja autoescape backstop.
"""

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

ACCOUNT_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#1a1a2e">
    <title>VitalForge Account</title>
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
        .box {
            background: #16213e;
            border-radius: 12px;
            padding: 2rem;
            width: min(620px, calc(100vw - 2rem));
        }
        h1 { font-size: 1.3rem; color: #c0c0e0; margin-bottom: 1.5rem; text-align: center; }
        h2 { font-size: 1rem; color: #c0c0e0; margin: 1.8rem 0 0.8rem; }
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
        .success { color: #66bb6a; font-size: 0.85rem; margin-bottom: 0.8rem; text-align: center; }
        table { width: 100%; border-collapse: collapse; margin-top: 0.8rem; }
        th, td { text-align: left; padding: 0.45rem; border-bottom: 1px solid #2a2a4a; font-size: 0.82rem; }
        td button { width: auto; padding: 0.4rem 0.7rem; background: #ef5350; }
        .token-reveal { display: none; margin: 0.8rem 0; padding: 0.8rem; background: #1a1a2e; border-radius: 6px; }
        .token-reveal code { display: block; overflow-wrap: anywhere; margin: 0.5rem 0; color: #80cbc4; }
        .token-reveal button { width: auto; }
        .hint { color: #aaa; font-size: 0.8rem; margin-bottom: 0.8rem; }
        a { color: #5c6bc0; text-decoration: none; display: block; text-align: center; margin-top: 1rem; font-size: 0.85rem; }
    </style>
</head>
<body>
    <div class="box">
        <h1>Your Account</h1>
        <div class="error" id="error"></div>
        <div class="success" id="success"></div>
        <form onsubmit="return changePassword(event)">
            <input type="password" id="current" placeholder="Current password" autocomplete="current-password" required>
            <input type="password" id="new" placeholder="New password" autocomplete="new-password" required>
            <button type="submit">Change Password</button>
        </form>
        <h2>API Tokens</h2>
        <p class="hint">Create a named token for Tasker, Bascule, or another unattended client.</p>
        <div class="token-reveal" id="token-reveal">
            <strong>Copy this token now. It will not be shown again.</strong>
            <code id="raw-token"></code>
            <button type="button" onclick="copyToken()">Copy token</button>
        </div>
        <form onsubmit="return createToken(event)">
            <input type="text" id="token-label" placeholder="Label (for example, Bascule)" required>
            <input type="password" id="token-password" placeholder="Current password" autocomplete="current-password" required>
            <button type="submit">Create Token</button>
        </form>
        <table>
            <thead><tr><th>Label</th><th>Created</th><th>Last used</th><th></th></tr></thead>
            <tbody id="tokens-body"></tbody>
        </table>
        <a href="/">Back</a>
    </div>
    <script>
        async function changePassword(e) {
            e.preventDefault();
            document.getElementById("error").textContent = "";
            document.getElementById("success").textContent = "";
            const res = await fetch("/auth/account/password", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    current_password: document.getElementById("current").value,
                    new_password: document.getElementById("new").value
                })
            });
            if (res.ok) {
                document.getElementById("success").textContent = "Password changed.";
                document.getElementById("current").value = "";
                document.getElementById("new").value = "";
            } else {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to change password.";
            }
            return false;
        }

        async function loadTokens() {
            const res = await fetch("/auth/tokens");
            if (!res.ok) return;
            const tokens = await res.json();
            const body = document.getElementById("tokens-body");
            body.textContent = "";
            for (const token of tokens) {
                const row = document.createElement("tr");
                for (const value of [token.label, token.created_at, token.last_used_at || "never"]) {
                    const cell = document.createElement("td");
                    cell.textContent = value;
                    row.appendChild(cell);
                }
                const action = document.createElement("td");
                const button = document.createElement("button");
                button.type = "button";
                button.textContent = "Revoke";
                button.onclick = () => revokeToken(token.id);
                action.appendChild(button);
                row.appendChild(action);
                body.appendChild(row);
            }
        }

        async function createToken(e) {
            e.preventDefault();
            document.getElementById("error").textContent = "";
            const res = await fetch("/auth/tokens", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    label: document.getElementById("token-label").value,
                    current_password: document.getElementById("token-password").value
                })
            });
            const responseBody = await res.json();
            if (res.ok) {
                document.getElementById("raw-token").textContent = responseBody.token;
                document.getElementById("token-reveal").style.display = "block";
                document.getElementById("token-label").value = "";
                document.getElementById("token-password").value = "";
                loadTokens();
            } else {
                document.getElementById("error").textContent = responseBody.detail || "Failed to create token.";
            }
            return false;
        }

        async function copyToken() {
            await navigator.clipboard.writeText(document.getElementById("raw-token").textContent);
        }

        async function revokeToken(id) {
            const password = prompt("Enter your current password to revoke this token:");
            if (!password) return;
            const res = await fetch(`/auth/tokens/${id}/revoke`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({current_password: password})
            });
            if (res.ok) {
                loadTokens();
            } else {
                const responseBody = await res.json();
                document.getElementById("error").textContent = responseBody.detail || "Failed to revoke token.";
            }
        }

        loadTokens();
    </script>
</body>
</html>"""

ADMIN_USERS_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#1a1a2e">
    <title>VitalForge Users</title>
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
            max-width: 640px;
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
        a { color: #5c6bc0; text-decoration: none; }
    </style>
</head>
<body>
    <div class="box">
        <h1>Manage Users</h1>
        <div class="error" id="error"></div>
        <div class="success" id="success"></div>
        <table id="users-table">
            <thead><tr><th>Username</th><th>Role</th><th>Created</th><th></th></tr></thead>
            <tbody id="users-body"></tbody>
        </table>
        <h2>Add User</h2>
        <form onsubmit="return createUser(event)">
            <input type="text" id="new-username" placeholder="Username" required>
            <input type="password" id="new-password" placeholder="Password" required>
            <select id="new-role">
                <option value="user">user</option>
                <option value="admin">admin</option>
            </select>
            <button type="submit">Create</button>
        </form>
        <h2>All API Tokens</h2>
        <table>
            <thead><tr><th>Owner</th><th>Label</th><th>Created</th><th>Last used</th><th></th></tr></thead>
            <tbody id="admin-tokens-body"></tbody>
        </table>
        <p style="margin-top:1rem"><a href="/auth/admin/persons">People</a> &middot; <a href="/">Back</a></p>
    </div>
    <script>
        // Every cell built from server data uses textContent/option.value, never
        // innerHTML -- a username is untrusted input as far as this page is
        // concerned, and innerHTML would execute markup in it.
        async function loadUsers() {
            const res = await fetch("/auth/admin/users/list");
            const users = await res.json();
            const body = document.getElementById("users-body");
            body.textContent = "";
            for (const u of users) {
                const row = document.createElement("tr");

                const usernameCell = document.createElement("td");
                usernameCell.textContent = u.username;
                row.appendChild(usernameCell);

                const roleCell = document.createElement("td");
                const roleSelect = document.createElement("select");
                for (const r of ["user", "admin"]) {
                    const opt = document.createElement("option");
                    opt.value = r;
                    opt.textContent = r;
                    if (r === u.role) opt.selected = true;
                    roleSelect.appendChild(opt);
                }
                roleSelect.onchange = () => updateRole(u.id, roleSelect.value);
                roleCell.appendChild(roleSelect);
                row.appendChild(roleCell);

                const createdCell = document.createElement("td");
                createdCell.textContent = u.created_at;
                row.appendChild(createdCell);

                const actionsCell = document.createElement("td");
                const resetBtn = document.createElement("button");
                resetBtn.textContent = "Reset Password";
                resetBtn.onclick = () => resetPassword(u.id);
                actionsCell.appendChild(resetBtn);

                const delBtn = document.createElement("button");
                delBtn.className = "danger";
                delBtn.textContent = "Delete";
                delBtn.onclick = () => deleteUser(u.id);
                actionsCell.appendChild(delBtn);

                row.appendChild(actionsCell);
                body.appendChild(row);
            }
        }

        async function createUser(e) {
            e.preventDefault();
            document.getElementById("error").textContent = "";
            document.getElementById("success").textContent = "";
            const res = await fetch("/auth/admin/users", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    username: document.getElementById("new-username").value,
                    password: document.getElementById("new-password").value,
                    role: document.getElementById("new-role").value
                })
            });
            if (res.ok) {
                document.getElementById("success").textContent = "User created.";
                document.getElementById("new-username").value = "";
                document.getElementById("new-password").value = "";
                loadUsers();
            } else {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to create user.";
            }
            return false;
        }

        async function updateRole(id, role) {
            document.getElementById("error").textContent = "";
            const res = await fetch(`/auth/admin/users/${id}`, {
                method: "PATCH",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({role: role})
            });
            if (!res.ok) {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to update role.";
            }
            loadUsers();  // re-render either way, so a rejected change reverts the dropdown
        }

        async function resetPassword(id) {
            document.getElementById("error").textContent = "";
            document.getElementById("success").textContent = "";
            const newPassword = prompt("New password for this user:");
            if (!newPassword) return;
            const res = await fetch(`/auth/admin/users/${id}`, {
                method: "PATCH",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({password: newPassword})
            });
            if (res.ok) {
                document.getElementById("success").textContent = "Password reset.";
            } else {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to reset password.";
            }
        }

        async function deleteUser(id) {
            document.getElementById("error").textContent = "";
            const res = await fetch(`/auth/admin/users/${id}`, { method: "DELETE" });
            if (res.ok) {
                loadUsers();
                loadAllTokens();
            } else {
                const body = await res.json();
                document.getElementById("error").textContent = body.detail || "Failed to delete user.";
            }
        }

        async function loadAllTokens() {
            const res = await fetch("/auth/admin/tokens");
            if (!res.ok) return;
            const tokens = await res.json();
            const body = document.getElementById("admin-tokens-body");
            body.textContent = "";
            for (const token of tokens) {
                const row = document.createElement("tr");
                for (const value of [token.owner, token.label, token.created_at, token.last_used_at || "never"]) {
                    const cell = document.createElement("td");
                    cell.textContent = value;
                    row.appendChild(cell);
                }
                const action = document.createElement("td");
                const button = document.createElement("button");
                button.type = "button";
                button.className = "danger";
                button.textContent = "Revoke";
                button.onclick = () => revokeManagedToken(token.id);
                action.appendChild(button);
                row.appendChild(action);
                body.appendChild(row);
            }
        }

        async function revokeManagedToken(id) {
            const password = prompt("Enter your current password to revoke this token:");
            if (!password) return;
            const res = await fetch(`/auth/tokens/${id}/revoke`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({current_password: password})
            });
            if (res.ok) {
                loadAllTokens();
            } else {
                const responseBody = await res.json();
                document.getElementById("error").textContent = responseBody.detail || "Failed to revoke token.";
            }
        }

        loadUsers();
        loadAllTokens();
    </script>
</body>
</html>"""

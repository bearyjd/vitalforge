/* Goal create / edit / delete. Writes carry a fixed header that the API requires (CSRF guard); values render via textContent. */
(function () {
    "use strict";
    const el = id => document.getElementById(id);
    const metrics = {
        sleep_duration: "Sleep duration", sleep_score: "Sleep score", resting_hr: "Resting heart rate",
        hrv: "Overnight HRV", body_battery: "Body Battery high", body_battery_low: "Body Battery low",
        stress: "Stress", steps: "Steps", active_calories: "Active calories"
    };
    const WRITE = { "Content-Type": "application/json", "X-VitalForge-Write": "goals" };
    let editingId = null;
    const status = message => { el("goalStatus").textContent = message; };

    async function call(path, options) {
        const response = await fetch(path, { credentials: "same-origin", ...options });
        if (!response.ok) {
            let detail = `request failed (${response.status})`;
            try { const body = await response.json(); if (typeof body.detail === "string") detail = body.detail; } catch (e) { /* keep default */ }
            throw new Error(detail);
        }
        return response.json();
    }
    function resetForm() {
        editingId = null;
        el("goalForm").reset();
        el("goalSave").textContent = "Add goal";
        el("goalCancel").hidden = true;
    }
    function startEdit(goal) {
        editingId = goal.id;
        el("goalMetric").value = goal.metric;
        el("goalTarget").value = goal.target_value;
        el("goalDate").value = goal.target_date || "";
        el("goalSave").textContent = "Save goal";
        el("goalCancel").hidden = false;
    }
    function row(goal) {
        const li = document.createElement("li");
        const label = metrics[goal.metric] || goal.metric;
        li.textContent = `${label}: target ${goal.target_value}${goal.target_date ? ` by ${goal.target_date}` : ""} `;
        const edit = document.createElement("button");
        edit.type = "button"; edit.className = "btn"; edit.textContent = "Edit";
        edit.addEventListener("click", () => startEdit(goal));
        const del = document.createElement("button");
        del.type = "button"; del.className = "btn"; del.textContent = "Delete";
        del.addEventListener("click", () => remove(goal));
        li.append(edit, " ", del);
        return li;
    }
    async function load() {
        try {
            const goals = await call(`${window.VF_API_BASE}/goals`);
            el("goalList").replaceChildren(...goals.map(row));
            if (!goals.length) status("No goals yet.");
        } catch (error) { status(`Goals unavailable: ${error.message}`); }
    }
    async function remove(goal) {
        if (!window.confirm(`Delete the ${metrics[goal.metric] || goal.metric} goal?`)) return;
        try {
            await call(`/api/goals/${goal.id}`, { method: "DELETE", headers: WRITE });
            if (editingId === goal.id) resetForm();
            status("Goal deleted.");
            await load();
        } catch (error) { status(`Delete failed: ${error.message}`); }
    }
    async function save(event) {
        event.preventDefault();
        const body = { metric: el("goalMetric").value, target_value: Number(el("goalTarget").value) };
        if (el("goalDate").value) body.target_date = el("goalDate").value;
        try {
            await call(editingId === null ? `${window.VF_API_BASE}/goals` : `${window.VF_API_BASE}/goals/${editingId}`,
                { method: editingId === null ? "POST" : "PATCH", headers: WRITE, body: JSON.stringify(body) });
            status(editingId === null ? "Goal added." : "Goal updated.");
            resetForm();
            await load();
        } catch (error) { status(`Save failed: ${error.message}`); }
    }
    Object.entries(metrics).forEach(([key, name]) => el("goalMetric").add(new Option(name, key)));
    el("goalForm").addEventListener("submit", save);
    el("goalCancel").addEventListener("click", resetForm);
    el("goalsPanel").addEventListener("toggle", function once(event) {
        if (!event.target.open) return;
        event.target.removeEventListener("toggle", once);
        load();
    });
})();

/* Shared profile context. Drafts and export consent live only in this page's memory. */
(function () {
    "use strict";
    const el = id => document.getElementById(id);
    const text = (id, value) => { el(id).textContent = value; };
    const today = () => new Date().toISOString().slice(0, 10);
    const metrics = {
        sleep_duration: "Sleep duration", sleep_score: "Sleep score", resting_hr: "Resting heart rate",
        hrv: "Overnight HRV", body_battery: "Body Battery high", body_battery_low: "Body Battery low",
        stress: "Stress", steps: "Steps", active_calories: "Active calories"
    };
    let version = 0, dateVersion = 0, days = 30, unit = "kg", canManage = false;
    let entries = [], plan = null, dayReady = false, dayDirty = false, planDirty = false, initialized = false;
    let activeDate = null, logged = false, revoked = false, contextLoaded = false, planRevision = 0;
    const number = value => typeof value === "number" && Number.isFinite(value);
    const api = path => `${window.VF_API_BASE}/context${path}`;
    const chartEligible = chart => /^chart(Sleep|Rhr|Hrv|BodyBattery|Stress|Weight|BodyFat|Steps|TrainingLoad)/.test(chart.canvas.id);
    const entryAt = (chart, index) => {
        if (!chartEligible(chart) || !number(chart.data.datasets[0]?.data[index])) return null;
        return entries.find(entry => entry.date === chart.data.labels[index]);
    };
    const summary = entry => [entry.tags.length ? `Tags: ${entry.tags.join(", ")}` : "Logged without tags", entry.note].filter(Boolean).join(" · ");
    function redraw() {
        if (typeof Chart !== "undefined") Object.values(Chart.instances).forEach(chart => {
            if (!chartEligible(chart)) return;
            chart.setActiveElements([]);
            if (chart.tooltip) {
                chart.tooltip.setActiveElements([], { x: 0, y: 0 });
                // Chart.js keeps afterBody text cached after a tooltip hides.
                chart.tooltip.afterBody = [];
            }
            chart.draw();
        });
    }
    if (typeof Chart !== "undefined") Chart.register({
        id: "vitalforgeContext",
        afterInit(chart) {
            if (!chartEligible(chart)) return;
            const tooltip = chart.options.plugins.tooltip;
            const previous = tooltip.callbacks.afterBody;
            tooltip.callbacks.afterBody = items => {
                const extra = previous ? previous.call(tooltip, items) : [];
                const entry = items.length ? entryAt(chart, items[0].dataIndex) : null;
                return [ ...(Array.isArray(extra) ? extra : [extra]).filter(Boolean), ...(entry ? [`Context: ${summary(entry).slice(0, 180)}`] : []) ];
            };
        },
        afterDatasetsDraw(chart) {
            if (!chartEligible(chart) || !chart.chartArea || !chart.scales.x) return;
            chart.$contextMarkerDates = [];
            const ctx = chart.ctx;
            ctx.save(); ctx.fillStyle = "#ffcf70";
            chart.data.labels.forEach((date, index) => {
                if (!entryAt(chart, index)) return;
                const x = chart.scales.x.getPixelForValue(index);
                if (x < chart.chartArea.left || x > chart.chartArea.right) return;
                ctx.beginPath(); ctx.arc(x, chart.chartArea.top + 5, 3, 0, 2 * Math.PI); ctx.fill();
                chart.$contextMarkerDates.push(date);
            });
            ctx.restore();
        }
    });
    function clear(message = "Loading context…") {
        ++version;
        entries = []; contextLoaded = false;
        text("contextStatus", message);
        text("contextSuggestions", "");
        el("contextEntries").replaceChildren();
        text("contextAssociation", "Loading association evidence…");
        redraw();
        exportPreview();
    }
    function dayControls() {
        const editable = canManage && dayReady;
        el("contextDate").disabled = !canManage || dayDirty;
        el("contextNote").disabled = !editable;
        el("contextTags").disabled = !editable;
        el("contextSave").disabled = !editable;
        el("contextDiscard").disabled = !canManage || !dayDirty;
        el("contextDelete").disabled = !editable || !logged;
    }
    function revoke() {
        revoked = true; canManage = false; ++dateVersion;
        // Not a latch: a failed write here (e.g. manage downgraded to view) does not end view access.
        window.VFWeeklyReview?.clear("Access to this profile may have changed. Generate the review again.");
        dayDirty = planDirty = dayReady = false;
        activeDate = null; plan = null; entries = [];
        el("contextNote").value = el("contextTags").value = "";
        el("contextPlanForm").reset();
        text("contextDayStatus", ""); text("contextPlanStatus", "");
        el("includeContext").checked = false;
        el("contextDayForm").hidden = el("contextPlanForm").hidden = true;
        text("contextPlanSummary", "");
        clear("Context access changed. Reload to check your access.");
        text("contextAssociation", "Context access changed. Reload to check your access.");
        window.updateContextExport?.();
    }
    async function request(path, method = "GET", body) {
        const options = { method };
        if (method !== "GET") {
            options.headers = { "X-VitalForge-Write": "context" };
            if (body !== undefined) {
                options.headers["Content-Type"] = "application/json";
                options.body = JSON.stringify(body);
            }
        }
        const response = await fetch(api(path), options);
        if (response.status === 401 || response.status === 403 || response.status === 404) revoke();
        if (!response.ok) throw new Error("Context request unavailable");
        return response.status === 204 ? null : response.json();
    }
    async function loadDate(date) {
        if (!canManage) return;
        const stamp = ++dateVersion;
        activeDate = date; dayReady = dayDirty = false; logged = false;
        el("contextDate").value = date;
        el("contextNote").value = el("contextTags").value = "";
        dayControls(); text("contextDayStatus", "Loading this date before editing…");
        try {
            const entry = await request(`/days/${encodeURIComponent(date)}`);
            if (stamp !== dateVersion || !canManage) return;
            el("contextNote").value = entry.note || "";
            el("contextTags").value = (entry.tags || []).join(", ");
            logged = entry.logged; dayReady = true;
            text("contextDayStatus", logged ? "Saved day loaded." : "No log for this date. Nothing is saved until you choose Save day.");
        } catch {
            if (stamp !== dateVersion) return;
            text("contextDayStatus", "This date could not be loaded. Choose a date again or reload before editing.");
        }
        dayControls();
    }
    function renderEntries() {
        el("contextEntries").replaceChildren();
        if (!entries.length) {
            const li = document.createElement("li"); li.textContent = "No context logged in this range."; el("contextEntries").append(li);
        }
        for (const entry of entries) {
            const li = document.createElement("li");
            li.textContent = `${entry.date} · ${summary(entry)}`;
            if (canManage) {
                const button = document.createElement("button");
                button.type = "button"; button.className = "btn btn-sm btn-ghost"; button.textContent = `Edit ${entry.date}`;
                button.addEventListener("click", () => {
                    if (dayDirty) { text("contextDayStatus", "Save or discard your draft before choosing another date."); return; }
                    loadDate(entry.date);
                });
                li.append(" ", button);
            }
            el("contextEntries").append(li);
        }
        redraw(); exportPreview();
    }
    function renderPlan() {
        text("contextPlanSummary", plan ? `Saved plan: ${plan.title} · tag: ${plan.tag} · ${metrics[plan.metric] || plan.metric} · planned ${plan.start_date} through ${plan.end_date}.` : "No routine plan saved.");
        el("contextPlanDelete").hidden = !plan || !canManage;
        el("contextReplaceLabel").hidden = !plan;
        el("contextReplace").required = Boolean(plan);
        if (planDirty) return;
        el("contextPlanTitle").value = plan?.title || "";
        el("contextPlanTag").value = plan?.tag || "";
        el("contextPlanMetric").value = plan?.metric || "sleep_duration";
        el("contextPlanStart").value = plan?.start_date || today();
        el("contextPlanEnd").value = plan?.end_date || today();
        el("contextReplace").checked = false;
    }
    function display(value, metric) {
        if (!number(value)) return "unavailable";
        let factor = 1, label = metric?.unit || "";
        if (label === "g") { factor = unit === "lbs" ? 1 / 453.592 : 1 / 1000; label = unit; }
        if (label === "seconds") { factor = 1 / 3600; label = "hours"; }
        return `${(value * factor).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${label}`.trim();
    }
    function renderAssociation(report) {
        if (report.status === "no_experiment") { text("contextAssociation", "Save a chosen routine plan to view association evidence."); return; }
        if (report.status === "outside_period") { text("contextAssociation", "The planned dates do not overlap this selected range. No association is calculated."); return; }
        const lines = [`${report.period.start_date} through ${report.period.end_date} · ${report.period.days} calendar days · ${report.metric.label}.`];
        for (const [key, label] of [["tagged", "Logged with routine tag"], ["untagged", "Explicitly logged without routine tag"]]) {
            const group = report.groups[key];
            lines.push(`${label}: ${group.paired_days} date-paired observations; ${group.logged_days} logged days; mean ${display(group.mean, report.metric)}.`,
                `Paired dates: ${group.dates.join(", ") || "none"}. Missing metric dates: ${group.missing_metric_dates.join(", ") || "none"}.`);
        }
        lines.push(report.difference ? `Tagged minus explicitly logged without tag: ${display(report.difference.absolute, report.metric)}; ${number(report.difference.percent) ? `${report.difference.percent.toFixed(1)}%` : "percent difference unavailable"}.` : "Insufficient evidence: at least 3 paired dates in each group are needed for a difference.");
        const c = report.coverage;
        lines.push(`Coverage: ${c.logged_days}/${c.calendar_days} days explicitly logged (${c.logged_percent}%); ${c.unlogged_days} unlogged days excluded; ${c.metric_missing_days} logged days missing a metric.`);
        if (report.cooccurring_tags.length) lines.push(`Other tags (with / without routine): ${report.cooccurring_tags.map(t => `${t.tag}: ${t.tagged_days} / ${t.untagged_days}`).join("; ")}. These can confound the comparison.`);
        lines.push(`Evidence flags: ${report.flags.map(flag => flag.replaceAll("_", " ")).join(", ") || "none"}.`, ...(report.notes || []));
        text("contextAssociation", lines.join("\n"));
    }
    function exportPreview() {
        if (!el("includeContext")) return;
        if (!el("includeContext").checked) { text("contextExportPreview", "Context notes, tags and routine plan are excluded."); return; }
        if (!contextLoaded) { text("contextExportPreview", "Context is selected for export, but its preview is unavailable while context loads or after a request fails. The downloaded file uses the current saved context for the selected range."); return; }
        const start = new Date(`${today()}T00:00:00Z`); start.setUTCDate(start.getUTCDate() - days + 1);
        const relevant = plan && plan.start_date <= today() && plan.end_date >= start.toISOString().slice(0, 10);
        text("contextExportPreview", `Included context preview (${entries.length} logged days): ${entries.map(entry => `${entry.date}: ${summary(entry)}`).join("; ") || "No logged context available in this range."}${relevant ? ` Relevant saved plan: ${plan.title}, tag ${plan.tag}, metric ${metrics[plan.metric] || plan.metric}, planned ${plan.start_date} to ${plan.end_date}.` : " No relevant saved routine plan."}`);
    }
    function initialize() {
        if (initialized) return; initialized = true;
        for (const [value, label] of Object.entries(metrics)) {
            const option = document.createElement("option"); option.value = value; option.textContent = label; el("contextPlanMetric").append(option);
        }
        el("contextDate").value = today();
        el("contextDate").addEventListener("change", () => { if (el("contextDate").value) loadDate(el("contextDate").value); });
        for (const id of ["contextNote", "contextTags"]) el(id).addEventListener("input", () => { dayDirty = true; dayControls(); text("contextDayStatus", "Unsaved draft. Save or discard before choosing another date."); });
        el("contextDiscard").addEventListener("click", () => loadDate(activeDate));
        el("contextDayForm").addEventListener("submit", async event => {
            event.preventDefault(); if (!canManage || !dayReady) return;
            const date = activeDate, stamp = dateVersion;
            const tags = el("contextTags").value.split(",").map(tag => tag.trim()).filter(Boolean);
            if (tags.length > 12 || tags.some(tag => tag.length > 40)) { text("contextDayStatus", "Use at most 12 tags, with at most 40 characters each."); return; }
            const note = el("contextNote").value;
            dayReady = false; dayControls();
            try {
                await request(`/days/${date}`, "PUT", { note, tags });
                if (stamp !== dateVersion || !canManage) return;
                await loadDate(date); await refresh(days, unit);
            } catch { if (stamp === dateVersion) { dayReady = true; dayControls(); text("contextDayStatus", "Could not save this day. Your draft is still here; try again."); } }
        });
        el("contextDelete").addEventListener("click", async () => {
            if (!canManage || !dayReady || !logged) return;
            const date = activeDate, stamp = dateVersion; dayReady = false; dayControls();
            try {
                await request(`/days/${date}`, "DELETE");
                if (stamp !== dateVersion || !canManage) return;
                await loadDate(date); await refresh(days, unit);
            } catch { if (stamp === dateVersion) { dayReady = true; dayControls(); text("contextDayStatus", "Could not delete this day. Try again."); } }
        });
        el("contextPlanForm").addEventListener("input", () => { planDirty = true; ++planRevision; });
        el("contextPlanDiscard").addEventListener("click", () => { planDirty = false; renderPlan(); text("contextPlanStatus", "Draft discarded."); });
        el("contextPlanForm").addEventListener("submit", async event => {
            event.preventDefault(); if (!canManage || (plan && !el("contextReplace").checked)) return;
            const body = { title: el("contextPlanTitle").value, tag: el("contextPlanTag").value, metric: el("contextPlanMetric").value, start_date: el("contextPlanStart").value, end_date: el("contextPlanEnd").value };
            const span = (Date.parse(body.end_date) - Date.parse(body.start_date)) / 86400000 + 1;
            if (span < 1 || span > 365) { text("contextPlanStatus", "Choose planned dates spanning 1 to 365 days."); return; }
            const revision = planRevision;
            el("contextPlanSave").disabled = true;
            try {
                await request("/experiment", "PUT", body);
                if (!canManage) return;
                if (revision === planRevision) planDirty = false;
                text("contextPlanStatus", planDirty ? "Routine plan saved. Newer edits are still an unsaved draft." : "Routine plan saved."); await refresh(days, unit);
            } catch { text("contextPlanStatus", "Could not save the routine plan. Check the fields and try again."); }
            finally { el("contextPlanSave").disabled = false; }
        });
        el("contextPlanDelete").addEventListener("click", async () => {
            if (!canManage) return;
            const revision = planRevision;
            try {
                await request("/experiment", "DELETE");
                if (!canManage) return;
                if (revision === planRevision) planDirty = false;
                text("contextPlanStatus", planDirty ? "Saved plan deleted. Newer edits are still an unsaved draft; day logs are retained." : "Saved plan deleted. Day logs are retained."); await refresh(days, unit);
            } catch { text("contextPlanStatus", "Could not delete the routine plan. Try again."); }
        });
        el("includeContext").addEventListener("change", exportPreview);
    }
    async function refresh(selectedDays, selectedUnit) {
        initialize(); days = selectedDays; unit = selectedUnit; clear();
        const stamp = version;
        try {
            const report = await request(`?days=${days}`);
            if (stamp !== version || revoked) return;
            canManage = report.can_manage;
            if (!canManage && (dayDirty || planDirty || dayReady)) {
                ++dateVersion; dayDirty = planDirty = dayReady = false; activeDate = null;
                el("contextNote").value = el("contextTags").value = ""; el("contextPlanForm").reset();
                text("contextDayStatus", ""); text("contextPlanStatus", "");
            }
            entries = report.entries; plan = report.experiment; contextLoaded = true;
            el("contextDayForm").hidden = el("contextPlanForm").hidden = !canManage;
            text("contextStatus", `${report.period.start_date} through ${report.period.end_date} · ${entries.length} logged days.${canManage ? "" : " View access: context is read only."}`);
            text("contextSuggestions", report.tag_suggestions?.length ? `Previously used tags: ${report.tag_suggestions.join(", ")}. You can use your own labels.` : "Use your own tag labels; tags are optional.");
            renderEntries(); renderPlan(); dayControls();
            if (canManage && !activeDate) loadDate(today());
        } catch {
            if (stamp !== version) return;
            entries = []; contextLoaded = false; renderEntries();
            text("contextStatus", "Context is unavailable. Drafts stay on this page; try changing the range or reloading.");
        }
        if (stamp !== version || revoked) return;
        try {
            const report = await request(`/associations?days=${days}`);
            if (stamp === version && !revoked) renderAssociation(report);
        } catch { if (stamp === version) text("contextAssociation", "Association evidence is unavailable. No old-range evidence is displayed."); }
    }
    window.VFContext = { refresh, clear, exportPreview };
})();

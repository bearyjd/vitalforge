/* Selected-range export preview and observation quality. */
(function () {
    "use strict";
    let requestVersion = 0;
    let exportDays = 30;
    const byId = id => document.getElementById(id);
    const labels = {
        available: "Available", empty: "No observations", unavailable: "Unavailable",
        complete: "Complete", partial: "Partial", current: "Recent", stale: "Stale",
        unknown: "Unknown", success: "Successful", error: "Error", failed: "Failed",
        backoff: "Waiting to retry", never_attempted: "Never attempted", idle: "Idle",
        link_required: "Garmin link required", auth_failed: "Garmin sign-in failed",
        rate_limited: "Garmin request limit reached", network: "Network error",
    };
    const label = value => labels[value] || "Unknown";

    function cell(row, value, secondary) {
        const td = document.createElement("td");
        td.textContent = value;
        if (secondary) {
            const note = document.createElement("small");
            note.textContent = secondary;
            td.appendChild(note);
        }
        row.appendChild(td);
    }

    window.updateContextExport = function () {
        const include = byId("includeContext").checked;
        byId("exportPackage").href = `${window.VF_API_BASE}/export/package?days=${exportDays}${include ? "&include_context=true" : ""}`;
        window.VFContext?.exportPreview();
    };
    byId("includeContext").checked = false;
    byId("includeContext").addEventListener("change", window.updateContextExport);
    window.addEventListener("pageshow", () => {
        byId("includeContext").checked = false;
        window.updateContextExport();
    });
    window.refreshDataQuality = async function (days) {
        const version = ++requestVersion;
        exportDays = days;
        window.updateContextExport();
        byId("packageRange").textContent = `All available metrics for this profile from the last ${days} days.`;
        byId("qualityStatus").textContent = "Loading data quality…";
        byId("qualitySync").textContent = "";
        byId("qualityRows").replaceChildren();
        byId("qualityTable").hidden = true;
        try {
            const response = await fetch(`${window.VF_API_BASE}/data-quality?days=${days}`);
            if (!response.ok) throw new Error("Quality unavailable");
            const report = await response.json();
            if (version !== requestVersion) return;
            if (!report.period || !report.metrics || !Object.keys(report.metrics).length) {
                throw new Error("Incomplete quality report");
            }
            const period = report.period;
            byId("packageRange").textContent = `All available metrics for this profile: ${period.start_date} through ${period.end_date}, inclusive (${period.days} days, UTC window). Observation dates follow the source’s recorded calendar date; its timezone is unknown.`;
            byId("qualityStatus").textContent = `${period.start_date} through ${period.end_date} · ${period.days} days · UTC window. Today may be incomplete.`;
            const sync = report.sync || {};
            byId("qualitySync").textContent = `Last sync attempt: ${sync.last_attempt_at || "Never recorded"}. Result: ${label(sync.last_result)}. Sync state: ${label(sync.status)}.${sync.backoff_until ? ` Retry after: ${sync.backoff_until}.` : ""} Per-metric sync results are unknown.`;
            const rows = document.createDocumentFragment();
            for (const metric of Object.values(report.metrics)) {
                const row = document.createElement("tr");
                cell(row, metric.label || "Unknown metric", `${metric.unit || "Unit unknown"} · ${metric.expected_cadence === "daily" ? "Daily" : "Snapshot"}`);
                cell(row, metric.latest_observation_date || "None recorded");
                const coverage = typeof metric.coverage_percent === "number" ? `${metric.coverage_percent}%` : "Unknown";
                cell(row, `${metric.observed_days ?? "Unknown"} / ${period.days} days`, coverage);
                cell(row, metric.missing_days ?? "Unknown");
                cell(row, `${label(metric.availability)} / ${label(metric.completeness)}`);
                cell(row, metric.expected_cadence === "daily" ? label(metric.freshness) : "Not assessed (snapshot)");
                rows.appendChild(row);
            }
            byId("qualityRows").replaceChildren(rows);
            byId("qualityTable").hidden = false;
        } catch {
            if (version !== requestVersion) return;
            byId("qualityStatus").textContent = "Data quality is unavailable. Try changing the range or refreshing the page.";
            byId("qualitySync").textContent = "";
            byId("qualityRows").replaceChildren();
            byId("qualityTable").hidden = true;
        }
    };
    window.refreshDataQuality(30);
})();

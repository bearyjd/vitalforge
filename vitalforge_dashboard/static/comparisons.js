/* Personal, descriptive comparisons. All dates and evidence come from one report. */
(function () {
    "use strict";
    const number = value => typeof value === "number" && Number.isFinite(value);
    const observedMean = values => {
        const result = values.length ? values.reduce((mean, value) => mean + value / values.length, 0) : null;
        return number(result) ? result : null;
    };
    const text = (id, value) => { document.getElementById(id).textContent = value; };
    const format = value => number(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—";
    const words = value => String(value || "unknown").replaceAll("_", " ");
    function conversion(metric, unit) {
        if (metric.unit === "g") return { factor: unit === "lbs" ? 1 / 453.592 : 1 / 1000, unit };
        if (metric.unit === "seconds") return { factor: 1 / 3600, unit: "hours" };
        return { factor: 1, unit: metric.unit || "" };
    }
    function display(value, metric, unit) {
        const converted = conversion(metric, unit);
        return number(value) ? `${format(value * converted.factor)} ${converted.unit}`.trim() : "—";
    }
    function period(value) { return `${value.start_date} to ${value.end_date} (${value.days} calendar days)`; }
    function clear(message = "Loading personal comparisons…") {
        text("comparisonStatus", message);
        text("comparisonWindows", "");
        document.getElementById("comparisonTable").hidden = true;
        document.getElementById("comparisonRows").replaceChildren();
        text("comparisonNotes", "");
        text("findingsPeriod", "");
        document.getElementById("comparisonFindings").replaceChildren();
        document.getElementById("readinessTile").style.display = "";
        text("readinessScoreValue", "—");
        document.getElementById("readinessScoreValue").className = "readiness-score-value";
        text("readinessStatus", message);
        document.getElementById("readinessComponents").replaceChildren();
        document.getElementById("readinessComponentDetails").replaceChildren();
        text("readinessExplanation", "HRV 40% · resting heart rate 30% · sleep score 30%. Available components are renormalized when inputs are missing. HRV and resting heart rate use observation windows, not calendar days.");
    }
    function renderReadiness(data) {
        text("readinessScoreValue", data?.freshness === "current" && number(data?.score) ? data.score : "—");
        text("readinessStatus", `${words(data?.status || "insufficient_data")} · ${words(data?.freshness)}${data?.freshness === "stale" ? ". Stale observations; current readiness is unavailable." : ""}`);
        const root = document.getElementById("readinessComponents");
        const details = document.getElementById("readinessComponentDetails");
        root.replaceChildren();
        details.replaceChildren();
        const explanation = document.createElement("p");
        explanation.textContent = `${data?.explanation || "Insufficient observations to explain this score."} ${(data?.notes || []).join(" ")}`;
        details.append(explanation);
        for (const [key, label] of Object.entries({ hrv: "HRV", rhr: "Resting HR", sleep_score: "Sleep score" })) {
            const span = document.createElement("span");
            const info = data?.component_details?.[key];
            span.textContent = `${label}: ${format(data?.components?.[key])}${info ? ` · ${info.available ? "available" : "missing or insufficient"} · ${words(info.freshness)} · n=${info.observation_count} · weight ${format(info.effective_weight * 100)}%` : " · missing or insufficient input"}`;
            root.append(span);
            if (info) {
                const detail = document.createElement("p");
                detail.textContent = `${label} · latest ${info.latest_observation_date || "unavailable"}. ${info.explanation} Recent observations: ${(info.recent_dates || []).join(", ") || "none"}. Baseline observations: ${(info.baseline_dates || []).join(", ") || "none"}.`;
                details.append(detail);
            }
        }
    }
    function render(report, unit) {
        text("comparisonStatus", `Descriptive differences in recorded observations; changes do not establish a cause. Generated ${report.generated_at}.`);
        text("comparisonWindows", `Current: ${period(report.current_period)}. Previous: ${period(report.previous_period)}.`);
        const root = document.getElementById("comparisonRows");
        root.replaceChildren();
        for (const [key, metric] of Object.entries(report.metrics)) {
            const snapshot = metric.expected_cadence !== "daily";
            const row = document.createElement("tr");
            row.dataset.metric = key;
            const status = snapshot ? "Repeated estimates, not independent measurements; no difference or band" : words(metric.status);
            const baseline = metric.baseline;
            const cells = [metric.label, display(metric.current.mean, metric, unit), display(metric.previous.mean, metric, unit),
                snapshot ? "—" : display(metric.difference, metric, unit),
                snapshot || !number(metric.percent_change) ? "—" : `${format(metric.percent_change)}%`,
                `Current n=${metric.current.n}; previous n=${metric.previous.n}; minimum ${metric.minimum_samples}`,
                `${status} · ${words(metric.freshness)} · latest ${metric.latest_observation_date || "unavailable"}`,
                !snapshot && baseline ? `Previous-period mean ± 1 sample SD: ${display(baseline.lower, metric, unit)} to ${display(baseline.upper, metric, unit)}; n=${baseline.n}; mean ${display(baseline.mean, metric, unit)}; SD ${display(baseline.sample_sd, metric, unit)}` : "Unavailable"];
            cells.forEach(value => { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); });
            root.append(row);
        }
        document.getElementById("comparisonTable").hidden = false;
        text("comparisonNotes", (report.notes || []).join(" "));
        text("findingsPeriod", `Existing daily anomaly findings · ${period(report.findings_period)}. This fixed 30-day analysis is independent of the selected comparison range.`);
        const findings = document.getElementById("comparisonFindings");
        findings.replaceChildren();
        if (!report.findings.length) {
            const item = document.createElement("li");
            item.textContent = "No supported anomaly finding in this window; sparse history can prevent an assessment.";
            findings.append(item);
        }
        report.findings.forEach(finding => {
            const item = document.createElement("li");
            item.textContent = finding.message;
            const evidence = document.createElement("p");
            const stats = finding.evidence;
            const metric = { unit: finding.unit };
            evidence.textContent = stats ? `Recent observations (${stats.recent_n}): ${stats.recent_dates.join(", ")}; mean ${display(stats.recent_avg, metric, unit)}. Preceding baseline observations (${stats.baseline_n}): ${stats.baseline_dates.join(", ")}; mean ${display(stats.baseline_mean, metric, unit)}, sample SD ${display(stats.baseline_sd, metric, unit)}. Method: latest 3 observations against the preceding up to 21 observations; ${words(stats.freshness)}. ${stats.z_score_kind === "constant_baseline_rule_flag" ? `Constant baseline rule flag ${format(stats.assigned_rule_score)}; a mathematical z-score is undefined.` : `Descriptive z-score ${format(stats.z_score)}.`}` : "Evidence unavailable.";
            item.append(evidence);
            findings.append(item);
        });
        renderReadiness(report.readiness);
    }
    function observedSeries(metric) {
        return { data: metric.data.map((point, index) => {
            const observed = metric.data.slice(Math.max(0, index - 6), index + 1).map(item => item.value).filter(number);
            return { ...point, moving_avg_7d: number(point.value) ? observedMean(observed) : null };
        }) };
    }
    function sanitizeSeries(series, dates) {
        const observed = new Map();
        for (const point of series.data || []) {
            if (typeof point.date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(point.date) || !number(point.value)) continue;
            const parsed = new Date(`${point.date}T00:00:00Z`);
            if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== point.date) continue;
            if (point.date >= dates.start_date && point.date <= dates.end_date) observed.set(point.date, point.value);
        }
        const data = [];
        for (let day = new Date(`${dates.start_date}T00:00:00Z`); day.toISOString().slice(0, 10) <= dates.end_date; day.setUTCDate(day.getUTCDate() + 1)) {
            const date = day.toISOString().slice(0, 10);
            data.push({ date, value: observed.get(date) ?? null });
        }
        return observedSeries({ data });
    }
    function chartData(metric, dates, unit, color, secondary) {
        const factor = conversion(metric, unit).factor;
        const byDate = new Map(metric.data.map(point => [point.date, point.value]));
        const labels = [];
        for (let day = new Date(`${dates.start_date}T00:00:00Z`); day.toISOString().slice(0, 10) <= dates.end_date; day.setUTCDate(day.getUTCDate() + 1)) {
            labels.push(day.toISOString().slice(0, 10));
        }
        const values = labels.map(date => number(byDate.get(date)) ? byDate.get(date) * factor : null);
        const averages = values.map((value, index) => {
            if (value === null) return null;
            const observed = values.slice(Math.max(0, index - 6), index + 1).filter(number);
            return observedMean(observed);
        });
        const datasets = [
            { label: "Recorded value", data: values, borderColor: color, pointRadius: 2, borderWidth: 1, spanGaps: false },
            { label: "7 calendar day observed mean", data: averages, borderColor: secondary, pointRadius: 0, borderWidth: 2, spanGaps: false }
        ];
        if (metric.baseline) {
            datasets.push({ label: "Previous-period mean − 1 sample SD", data: labels.map(() => metric.baseline.lower * factor), borderColor: "rgba(128,128,160,0.5)", pointRadius: 0, borderWidth: 1, fill: false });
            datasets.push({ label: "Previous-period mean + 1 sample SD", data: labels.map(() => metric.baseline.upper * factor), borderColor: "rgba(128,128,160,0.5)", backgroundColor: "rgba(128,128,160,0.14)", pointRadius: 0, borderWidth: 1, fill: "-1" });
        }
        return { labels, datasets };
    }
    window.VFComparisons = {
        clear, render, chartData, observedSeries, sanitizeSeries,
        async fetch(base, days) {
            try {
                const response = await fetch(`${base}/comparisons?days=${days}`);
                return response.ok ? await response.json() : null;
            } catch { return null; }
        }
    };
})();

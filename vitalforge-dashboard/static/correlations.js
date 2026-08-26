/**
 * Ad-hoc cross-metric correlation view: metric picker + hand-rolled CSS-grid
 * heatmap, backed by GET /api/correlations, plus a Chart.js scatter
 * drill-down on cell click.
 *
 * Self-contained on purpose: index.html's own inline <script> already owns
 * a `charts`/`metricCache`/`currentDays` global surface for the metric
 * charts above this section, and this file deliberately does not read or
 * write any of it -- it keeps its own state and its own single Chart.js
 * instance, so it behaves correctly (including on a right-out-of-the-box
 * empty database, e.g. Playwright's smoke test) regardless of whether the
 * rest of the page has loaded any data yet.
 *
 * `weight_log` is intentionally absent from this list: it's timestamp-keyed
 * (one row per log entry), not date-keyed like every table `/api/correlations`
 * can reach, and the server-side METRIC_TABLES dict this list mirrors never
 * contains it either.
 */
(function () {
    "use strict";

    const METRICS = [
        { key: "sleep_duration", label: "Sleep Duration", short: "Sleep Dur", unit: "sec" },
        { key: "sleep_score", label: "Sleep Score", short: "Sleep Score", unit: "" },
        { key: "resting_hr", label: "Resting HR", short: "Resting HR", unit: "bpm" },
        { key: "hrv", label: "HRV", short: "HRV", unit: "ms" },
        { key: "body_battery", label: "Body Battery (high)", short: "BB High", unit: "" },
        { key: "body_battery_low", label: "Body Battery (low)", short: "BB Low", unit: "" },
        { key: "stress", label: "Stress", short: "Stress", unit: "" },
        { key: "vo2max", label: "VO2 Max", short: "VO2 Max", unit: "" },
        { key: "weight", label: "Weight", short: "Weight", unit: "g" },
        { key: "body_fat", label: "Body Fat", short: "Body Fat", unit: "%" },
        { key: "body_water", label: "Body Water", short: "Body Water", unit: "%" },
        { key: "bone_mass", label: "Bone Mass", short: "Bone Mass", unit: "g" },
        { key: "muscle_mass", label: "Muscle Mass", short: "Muscle Mass", unit: "g" },
        { key: "training_load", label: "Training Load", short: "Train Load", unit: "" },
        { key: "steps", label: "Steps", short: "Steps", unit: "" },
        { key: "active_calories", label: "Active Calories", short: "Act Cal", unit: "" },
    ];
    const METRIC_BY_KEY = Object.fromEntries(METRICS.map((m) => [m.key, m]));
    const DEFAULT_SELECTED = ["sleep_duration", "resting_hr", "hrv", "stress", "steps"];

    const state = {
        selected: new Set(DEFAULT_SELECTED),
        days: 30,
        lag: 0,
        minPairs: 5,
        matrix: null, // last /api/correlations response
    };

    let scatterChart = null;

    // ── Color scale ──────────────────────────────────────────────────────
    // Diverging: negative -> teal, 0 -> neutral grid color, positive -> violet.
    // Matches the page's existing accent/secondary palette without implying
    // a value judgement (correlation sign isn't inherently good or bad).
    function corrColor(r) {
        if (r === null || r === undefined) return "#1a1a2e";
        const t = Math.max(0, Math.min(1, (r + 1) / 2)); // 0..1
        const neg = [38, 166, 154]; // teal accent
        const neu = [42, 42, 74]; // grid neutral
        const pos = [124, 77, 255]; // violet secondary
        let c;
        if (t < 0.5) {
            const k = t / 0.5;
            c = neg.map((v, i) => Math.round(v + (neu[i] - v) * k));
        } else {
            const k = (t - 0.5) / 0.5;
            c = neu.map((v, i) => Math.round(v + (pos[i] - v) * k));
        }
        return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
    }

    // ── Data fetching ────────────────────────────────────────────────────
    async function fetchCorrelations(metricKeys, days, lag, minPairs) {
        const params = new URLSearchParams({
            metrics: metricKeys.join(","),
            days: String(days),
            lag: String(lag),
            min_pairs: String(minPairs),
        });
        const res = await fetch(`/api/correlations?${params.toString()}`);
        if (!res.ok) throw new Error(`correlations request failed: ${res.status}`);
        return res.json();
    }

    async function fetchMetricSeries(name, days) {
        try {
            const res = await fetch(`/api/metrics/${name}?days=${days}`);
            if (!res.ok) return [];
            const json = await res.json();
            return json.data || [];
        } catch {
            return [];
        }
    }

    // ── Date-shift alignment (mirrors correlations.py's align_series) ─────
    function shiftDate(dateStr, days) {
        const d = new Date(`${dateStr}T00:00:00Z`);
        d.setUTCDate(d.getUTCDate() + days);
        return d.toISOString().slice(0, 10);
    }

    function alignForScatter(rowData, colData, lagDays) {
        const colMap = new Map(colData.map((d) => [d.date, d.value]));
        const rowMap = new Map();
        for (const d of rowData) {
            const key = lagDays ? shiftDate(d.date, lagDays) : d.date;
            rowMap.set(key, d.value);
        }
        const commonDates = [...rowMap.keys()].filter((d) => colMap.has(d)).sort();
        return commonDates.map((d) => ({ x: rowMap.get(d), y: colMap.get(d) }));
    }

    // ── DOM builders ─────────────────────────────────────────────────────
    function buildControls(container) {
        const wrap = document.createElement("div");
        wrap.className = "corr-controls";

        const picker = document.createElement("div");
        picker.className = "corr-metric-picker";
        METRICS.forEach((m) => {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = "corr-chip" + (state.selected.has(m.key) ? " active" : "");
            chip.textContent = m.short;
            chip.title = m.label;
            chip.addEventListener("click", () => {
                if (state.selected.has(m.key)) {
                    if (state.selected.size <= 1) return; // keep at least one metric
                    state.selected.delete(m.key);
                } else {
                    state.selected.add(m.key);
                }
                chip.classList.toggle("active");
                refresh();
            });
            picker.appendChild(chip);
        });
        wrap.appendChild(picker);

        wrap.appendChild(buildNumberField("Days", state.days, 1, 365, (v) => {
            state.days = v;
            refresh();
        }));
        wrap.appendChild(buildNumberField("Lag (days)", state.lag, -365, 365, (v) => {
            state.lag = v;
            refresh();
        }));
        wrap.appendChild(buildNumberField("Min pairs", state.minPairs, 2, 365, (v) => {
            state.minPairs = v;
            refresh();
        }));

        container.appendChild(wrap);
    }

    function buildNumberField(labelText, value, min, max, onChange) {
        const field = document.createElement("label");
        field.className = "corr-field";
        const span = document.createElement("span");
        span.textContent = labelText;
        const input = document.createElement("input");
        input.type = "number";
        input.min = String(min);
        input.max = String(max);
        input.value = String(value);
        input.addEventListener("change", () => {
            const v = parseInt(input.value, 10);
            if (Number.isFinite(v) && v >= min && v <= max) onChange(v);
        });
        field.appendChild(span);
        field.appendChild(input);
        return field;
    }

    // A null `r` can mean two different things -- too few aligned pairs, or
    // enough pairs but one metric was constant over them (zero variance) --
    // and the server disambiguates via `cellData.reason` (see
    // correlations.py's `compute_cell`).
    function nullCellReasonText(cellData, matrix) {
        if (cellData.r !== null) {
            return `r = ${cellData.r.toFixed(3)}, n = ${cellData.n}`;
        }
        if (cellData.reason === "zero_variance") {
            return `n = ${cellData.n} (zero variance -- one or both metrics were constant over this window)`;
        }
        return `n = ${cellData.n} (below min_pairs = ${matrix.min_pairs})`;
    }

    function renderHeatmap(heatmapWrap, matrix) {
        heatmapWrap.innerHTML = "";

        const names = matrix.metrics;
        if (!names.length) {
            const empty = document.createElement("div");
            empty.className = "corr-empty";
            empty.textContent = "Select at least one metric.";
            heatmapWrap.appendChild(empty);
            return;
        }

        const grid = document.createElement("div");
        grid.className = "corr-grid";
        grid.style.gridTemplateColumns = `auto repeat(${names.length}, 2.7rem)`;

        // Corner + column headers
        grid.appendChild(document.createElement("div"));
        names.forEach((key) => {
            const colLabel = document.createElement("div");
            colLabel.className = "corr-label-col";
            colLabel.textContent = METRIC_BY_KEY[key] ? METRIC_BY_KEY[key].short : key;
            grid.appendChild(colLabel);
        });

        // Rows
        names.forEach((rowKey, i) => {
            const rowLabel = document.createElement("div");
            rowLabel.className = "corr-label-row";
            rowLabel.textContent = METRIC_BY_KEY[rowKey] ? METRIC_BY_KEY[rowKey].short : rowKey;
            grid.appendChild(rowLabel);

            names.forEach((colKey, j) => {
                const cellData = matrix.cells[i][j];
                const cell = document.createElement("div");
                cell.className = "corr-cell" + (cellData.r === null ? " corr-null" : "");
                cell.style.background = corrColor(cellData.r);
                cell.textContent = cellData.r === null ? "–" : cellData.r.toFixed(2);
                cell.title = `${rowKey} × ${colKey}\n${nullCellReasonText(cellData, matrix)}`;
                cell.addEventListener("click", () => showDrilldown(rowKey, colKey, cellData));
                grid.appendChild(cell);
            });
        });

        heatmapWrap.appendChild(grid);
    }

    async function showDrilldown(rowKey, colKey, cellData) {
        const panel = document.getElementById("corrDrilldown");
        const label = document.getElementById("corrDrilldownLabel");
        panel.style.display = "";

        const rowMeta = METRIC_BY_KEY[rowKey] || { label: rowKey, unit: "" };
        const colMeta = METRIC_BY_KEY[colKey] || { label: colKey, unit: "" };
        const lagText = state.lag ? ` (row shifted +${state.lag}d)` : "";
        const rText = cellData.r === null ? "n/a" : cellData.r.toFixed(3);
        label.textContent = `${rowMeta.label} vs ${colMeta.label}${lagText} — r = ${rText}, n = ${cellData.n}`;

        const [rowData, colData] = await Promise.all([
            fetchMetricSeries(rowKey, state.days),
            fetchMetricSeries(colKey, state.days),
        ]);
        const points = alignForScatter(rowData, colData, state.lag);

        const ctx = document.getElementById("chartCorrScatter");
        if (!ctx || typeof Chart === "undefined") return;
        if (scatterChart) scatterChart.destroy();

        scatterChart = new Chart(ctx, {
            type: "scatter",
            data: {
                datasets: [
                    {
                        label: `${rowMeta.label} vs ${colMeta.label}`,
                        data: points,
                        backgroundColor: "rgba(124, 77, 255, 0.55)",
                        borderColor: "#7c4dff",
                        pointRadius: 3,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                aspectRatio: 2,
                plugins: {
                    legend: { display: false },
                    tooltip: { backgroundColor: "#16213e", borderColor: "#2a2a4a", borderWidth: 1 },
                },
                scales: {
                    x: {
                        title: { display: true, text: `${rowMeta.label}${rowMeta.unit ? ` (${rowMeta.unit})` : ""}` },
                        grid: { color: "#2a2a4a" },
                    },
                    y: {
                        title: { display: true, text: `${colMeta.label}${colMeta.unit ? ` (${colMeta.unit})` : ""}` },
                        grid: { color: "#2a2a4a" },
                    },
                },
            },
        });
    }

    // ── Main refresh ─────────────────────────────────────────────────────
    async function refresh() {
        const heatmapWrap = document.getElementById("corrHeatmapWrap");
        if (!heatmapWrap) return;

        const keys = [...state.selected];
        let matrix;
        try {
            matrix = await fetchCorrelations(keys, state.days, state.lag, state.minPairs);
        } catch {
            heatmapWrap.innerHTML = '<div class="corr-empty">Could not load correlations.</div>';
            return;
        }
        state.matrix = matrix;
        renderHeatmap(heatmapWrap, matrix);
    }

    function buildHeatmapPlaceholder(container) {
        const wrap = document.createElement("div");
        wrap.className = "corr-heatmap-wrap";
        wrap.id = "corrHeatmapWrap";
        container.appendChild(wrap);
    }

    function buildDrilldownPanel(container) {
        const panel = document.createElement("div");
        panel.className = "corr-drilldown";
        panel.id = "corrDrilldown";
        panel.style.display = "none";

        const label = document.createElement("div");
        label.className = "chart-label";
        label.id = "corrDrilldownLabel";
        panel.appendChild(label);

        const canvas = document.createElement("canvas");
        canvas.id = "chartCorrScatter";
        panel.appendChild(canvas);

        container.appendChild(panel);
    }

    function init() {
        const section = document.getElementById("correlationsSection");
        if (!section) return;

        const heading = document.createElement("h2");
        heading.textContent = "Correlations";
        section.appendChild(heading);

        buildControls(section);
        buildHeatmapPlaceholder(section);
        buildDrilldownPanel(section);
        refresh();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();

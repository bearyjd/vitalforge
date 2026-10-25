/* On-demand weekly review. Fetches only on a click; every value is rendered with textContent. */
(function () {
    "use strict";
    const el = id => document.getElementById(id);
    const number = value => typeof value === "number" && Number.isFinite(value);
    const words = value => String(value || "unknown").replaceAll("_", " ");
    const format = value => number(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—";
    let generation = 0, revoked = false;

    function display(value, unit) {
        if (!number(value)) return "—";
        if (unit === "seconds") return `${format(value / 3600)} hours`;
        if (unit === "g") return `${format(value / 1000)} kg`;
        return `${format(value)} ${unit || ""}`.trim();
    }
    function range(period) { return `${period.start_date} to ${period.end_date}`; }
    function status(message) { el("weeklyReviewStatus").textContent = message; }
    function clear(message = "") {
        generation += 1;
        el("weeklyReviewOutput").replaceChildren();
        status(message);
        el("weeklyReviewGenerate").disabled = revoked;
    }
    function revoke(message = "Weekly review unavailable: access to this profile has changed.") {
        revoked = true;
        clear(message);
    }
    function section(title, items, emptyText) {
        const wrap = document.createElement("section");
        wrap.dataset.section = title.toLowerCase().replaceAll(" ", "-");
        const heading = document.createElement("h3");
        heading.textContent = title;
        const list = document.createElement("ul");
        for (const text of items.length ? items : [emptyText]) {
            const item = document.createElement("li");
            item.textContent = text;
            list.append(item);
        }
        wrap.append(heading, list);
        return wrap;
    }
    const changeText = c => `${c.label}: ${display(c.current.mean, c.unit)} (${range(c.current)}, n=${c.current.n}, dates ${c.current.dates.join(", ")}) vs ${display(c.previous.mean, c.unit)} (${range(c.previous)}, n=${c.previous.n}, dates ${c.previous.dates.join(", ")}); difference ${display(c.difference, c.unit)}${number(c.percent_change) ? `, ${format(c.percent_change)}%` : ""}.`;
    const baselineText = f => {
        const dates = f.evidence?.baseline_dates || [];
        return dates.length ? ` Compared with ${dates.length} observations from ${dates[0]} to ${dates[dates.length - 1]}.` : "";
    };
    const findingText = f => `${f.label}: ${f.message} Period ${range(f.period)}; triggering observations ${f.observation_dates.join(", ")}.${baselineText(f)}`;
    const stableText = c => `${c.label}: unchanged, mean ${display(c.current.mean, c.unit)} in both weeks (current n=${c.current.n}, previous n=${c.previous.n}; ${range(c.current)} vs ${range(c.previous)}).`;
    const coverageText = (c, days) => `${c.label}: ${words(c.completeness)}, ${c.observed_days} of ${days} days observed, ${words(c.freshness)}, latest ${c.latest_observation_date || "unavailable"}.`;
    const goalText = g => `${g.label}: latest ${display(g.latest_value, g.unit)}${g.latest_observation_date ? ` on ${g.latest_observation_date}${g.stale ? " (stale)" : ""}` : ""}, target ${display(g.target_value, g.unit)}${g.target_date ? ` by ${g.target_date}` : ""}; week mean ${display(g.week_mean, g.unit)} (n=${g.week_n}) vs previous ${display(g.previous_mean, g.unit)} (n=${g.previous_n}); ${g.on_track === true ? "on track" : g.on_track === false ? "not on track" : "no projection"}.`;

    function render(report) {
        const root = el("weeklyReviewOutput");
        root.replaceChildren();
        status(report.empty ? report.message : `Descriptive weekly review for ${range(report.period)}, generated ${report.generated_at}. Differences do not establish a cause.`);
        const sync = `Last sync: ${words(report.sync.status)} (${words(report.sync.last_result)})${report.sync.last_attempt_at ? ` at ${report.sync.last_attempt_at}` : ""}.`;
        root.append(
            section("Changes", [...report.changes.map(changeText), ...report.stable.map(stableText)], "No metric has enough observations in both weeks to compare."),
            section("Findings", report.findings.map(findingText), "No supported finding this week."),
            section("Coverage", [sync, ...report.coverage.map(c => coverageText(c, report.period.days))], "Coverage is complete."),
            section("Goals", report.goals.map(goalText), "No goals to show."),
            section("Follow-up", [report.follow_up.text], "")
        );
        const limits = document.createElement("p");
        limits.textContent = report.limitations.join(" ");
        root.append(limits);
    }
    async function generate(base) {
        const stamp = ++generation;
        el("weeklyReviewOutput").replaceChildren();
        status("Generating weekly review…");
        el("weeklyReviewGenerate").disabled = true;
        try {
            const response = await fetch(`${base}/weekly-review`);
            if (stamp !== generation) return;
            if (response.status === 401 || response.status === 403 || response.status === 404) {
                revoke();
                return;
            }
            if (!response.ok) throw new Error(String(response.status));
            const report = await response.json();
            if (stamp !== generation) return;
            render(report);
        } catch {
            if (stamp !== generation) return;
            status("Weekly review unavailable. Try again.");
        }
        el("weeklyReviewGenerate").disabled = revoked;
    }
    const button = el("weeklyReviewGenerate");
    if (button) button.addEventListener("click", () => { if (!revoked) generate(window.VF_API_BASE); });
    window.VFWeeklyReview = { clear, generate, revoke };
})();

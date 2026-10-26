/* Printable report control: builds the report URL from the chosen period and opt-ins. */
(function () {
    "use strict";
    const byId = id => document.getElementById(id);
    // VF_API_BASE is "/p/<slug>/api"; the report page lives beside it at "/p/<slug>/report".
    const reportBase = () => `${window.VF_API_BASE.replace(/\/api$/, "")}/report`;

    function update() {
        const params = new URLSearchParams({
            days: byId("reportDays").value,
            include_identity: byId("reportIdentity").checked ? "true" : "false",
            include_context: byId("reportContext").checked ? "true" : "false",
        });
        byId("reportLink").href = `${reportBase()}?${params.toString()}`;
    }

    function reset() {
        byId("reportDays").value = "30";
        byId("reportIdentity").checked = false;
        byId("reportContext").checked = false;
        update();
    }

    for (const id of ["reportDays", "reportIdentity", "reportContext"]) {
        byId(id).addEventListener("change", update);
    }
    window.addEventListener("pageshow", reset);
    reset();
})();

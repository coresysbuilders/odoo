/** @odoo-module **/
/**
 * Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
 * Licensed under the Odoo Proprietary License v1.0 (OPL-1). Unauthorized copying,
 * redistribution, or resale, in whole or in part, via any medium, is prohibited.
 */
//
// Leads Analytics Dashboard client action. Read-only: the controls only change
// what is shown or open an existing view.
//
// Chart.js is loaded from Odoo's own bundle, not a CDN, and uses the v3/v4
// config shape (plugins.legend, keyed scales). Date buckets come from the
// backend; don't re-bucket them here. Sync Log and System Health are global
// and ignore the selected period.
//
import {
    Component,
    useState,
    useRef,
    onWillStart,
    onMounted,
    onWillUnmount,
} from "@odoo/owl";
import { registry } from "@web/core/registry";
import { loadJS } from "@web/core/assets";
import { useService } from "@web/core/utils/hooks";

// CoreSys brand colours; the donut uses shades of the accent.
const BRAND_ACCENT = "#0052FE";
const BRAND_INK = "#06152C";
const BRAND_SECONDARY = "#E4EEFC";
const DONUT_TINTS = ["#0052FE", "#3B7BFE", "#6CA0FE", "#9CC0FE", "#C7DBFE", "#E4EEFC"];

export class MetaLeadsDashboard extends Component {
    static template = "meta_lead_ads.MetaLeadsDashboard";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.lineRef = useRef("lineCanvas");
        this.donutRef = useRef("donutCanvas");

        // Opens on "This month". dateFrom/dateTo are only used for a custom range.
        this.state = useState({
            periodMode: "month",
            dateFrom: null,
            dateTo: null,
            drilldown: "campaign", // campaign | ads | adsets
            data: null,
            loading: true,
            error: false,
        });

        // Chart instances, destroyed before each re-render and on unmount.
        this._charts = [];
        // Request counter: a slow older response must not overwrite a newer
        // period selection.
        this._reqSeq = 0;

        onWillStart(async () => {
            // Chart is usually already global in the backend; loadJS is a
            // no-op then.
            await loadJS("/web/static/lib/Chart/Chart.js");
            await this.load();
        });
        // The canvas refs only exist after mount.
        onMounted(() => this.renderCharts());
        // Chart.js keeps canvas listeners alive unless destroyed.
        onWillUnmount(() => this._destroyCharts());
    }

    // ------------------------------------------------------------------ //
    // Data load
    // ------------------------------------------------------------------ //
    async load() {
        const seq = ++this._reqSeq;
        this.state.loading = true;
        this.state.error = false;
        try {
            // The third argument is the period mode, not a group-by; the
            // backend picks the bucket size from it.
            const data = await this.orm.call("meta.account", "get_dashboard_metrics", [
                this.state.dateFrom,
                this._inclusiveDateTo(),
                this.state.periodMode,
            ]);
            // A newer request has been sent since; drop this response.
            if (seq !== this._reqSeq) {
                return;
            }
            this.state.data = data;
        } catch (e) {
            // Show a generic message; backend or Graph error text is never
            // displayed.
            if (seq === this._reqSeq) {
                this.state.error = true;
            }
        } finally {
            if (seq === this._reqSeq) {
                this.state.loading = false;
                this.renderCharts();
            }
        }
    }

    get Chart() {
        // Set by the loadJS call in onWillStart.
        return globalThis.Chart;
    }

    // ------------------------------------------------------------------ //
    // Charts
    // ------------------------------------------------------------------ //
    _destroyCharts() {
        this._charts.forEach((c) => {
            try {
                c.destroy();
            } catch (e) {
                // Already destroyed; nothing to do.
            }
        });
        this._charts = [];
    }

    renderCharts() {
        this._destroyCharts();
        const Chart = this.Chart;
        const data = this.state.data;
        if (!Chart || !data || this.state.loading || this.state.error) {
            return;
        }

        // Leads over time
        const series = data.series || [];
        if (this.lineRef.el && series.length) {
            const labels = series.map((p) => p.bucket);
            const counts = series.map((p) => p.count);
            this._charts.push(
                new Chart(this.lineRef.el, {
                    type: "line",
                    data: {
                        labels,
                        datasets: [
                            {
                                label: "New leads",
                                data: counts,
                                borderColor: BRAND_ACCENT,
                                backgroundColor: BRAND_SECONDARY,
                                tension: 0.3,
                                fill: true,
                                pointBackgroundColor: BRAND_ACCENT,
                            },
                        ],
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: { legend: { display: false } },
                        scales: {
                            x: { ticks: { color: BRAND_INK } },
                            y: { beginAtZero: true, ticks: { color: BRAND_INK, precision: 0 } },
                        },
                    },
                })
            );
        }

        // Top campaigns donut. It shows every campaign's share of volume; the
        // 10-lead minimum only applies to the ranking table.
        const rows = this._donutRows();
        if (this.donutRef.el && rows.length) {
            this._charts.push(
                new Chart(this.donutRef.el, {
                    type: "doughnut",
                    data: {
                        labels: rows.map((r) => r.name),
                        datasets: [
                            {
                                data: rows.map((r) => r.count),
                                backgroundColor: rows.map(
                                    (r, i) => DONUT_TINTS[i % DONUT_TINTS.length]
                                ),
                            },
                        ],
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: { position: "bottom", labels: { color: BRAND_INK } },
                        },
                    },
                })
            );
        }
    }

    // ------------------------------------------------------------------ //
    // Drill-down
    //
    // In campaign mode the donut and the table use different lists: the donut
    // shows all campaigns, the table uses conversion_ranking, which leaves out
    // campaigns under 10 leads. For ads and ad sets both use the same rows.
    // ------------------------------------------------------------------ //
    _drilldownChildRows() {
        // Returns null in campaign mode so each caller picks its own list.
        const data = this.state.data;
        if (!data) {
            return [];
        }
        if (this.state.drilldown === "ads") {
            return (data.drilldown && data.drilldown.ads) || [];
        }
        if (this.state.drilldown === "adsets") {
            return (data.drilldown && data.drilldown.adsets) || [];
        }
        return null;
    }

    // Donut: all campaigns, no minimum volume.
    _donutRows() {
        const data = this.state.data;
        if (!data) {
            return [];
        }
        const child = this._drilldownChildRows();
        return child !== null ? child : data.campaigns || [];
    }

    // Table: campaigns under 10 leads are left out because their conversion
    // rate is noise (see the footnote on the card).
    _rankingRows() {
        const data = this.state.data;
        if (!data) {
            return [];
        }
        const child = this._drilldownChildRows();
        return child !== null ? child : data.conversion_ranking || [];
    }

    get drilldownRows() {
        return this._rankingRows();
    }

    setDrilldown(level) {
        if (this.state.loading || this.state.drilldown === level) {
            return;
        }
        this.state.drilldown = level;
        // Data is already loaded; just redraw.
        this.renderCharts();
    }

    // ------------------------------------------------------------------ //
    // Period selector (does not affect Sync Log or System Health)
    // ------------------------------------------------------------------ //
    get isCustom() {
        return this.state.periodMode === "custom";
    }

    onPeriodChange(ev) {
        if (this.state.loading) {
            return;
        }
        const mode = ev.target.value;
        this.state.periodMode = mode;
        if (mode !== "custom") {
            // Custom ranges wait until both dates are set.
            this.state.dateFrom = null;
            this.state.dateTo = null;
            this.load();
        }
    }

    onDateFromChange(ev) {
        this.state.dateFrom = ev.target.value || null;
        this._maybeLoadCustom();
    }

    onDateToChange(ev) {
        this.state.dateTo = ev.target.value || null;
        this._maybeLoadCustom();
    }

    _maybeLoadCustom() {
        if (this.state.loading) {
            return;
        }
        if (this.state.periodMode === "custom" && this.state.dateFrom && this.state.dateTo) {
            this.load();
        }
    }

    _inclusiveDateTo() {
        // Users read "To" as inclusive but the backend range is [from, to).
        // Add a day so the last day counts and From == To is a one-day range.
        if (this.state.periodMode !== "custom" || !this.state.dateTo) {
            return this.state.dateTo;
        }
        const d = new Date(this.state.dateTo + "T00:00:00Z");
        if (isNaN(d.getTime())) {
            return this.state.dateTo;
        }
        d.setUTCDate(d.getUTCDate() + 1);
        return d.toISOString().slice(0, 10);
    }

    // ------------------------------------------------------------------ //
    // Refresh and navigation
    // ------------------------------------------------------------------ //
    refresh() {
        if (this.state.loading) {
            return;
        }
        this.load();
    }

    openFullSyncLog() {
        this.action.doAction("meta_lead_ads.meta_sync_log_action");
    }

    // ------------------------------------------------------------------ //
    // Template helpers
    // ------------------------------------------------------------------ //
    formatPercent(fraction) {
        // Rates and deltas arrive as fractions (0.25 = 25%).
        if (fraction === null || fraction === undefined) {
            return "";
        }
        return `${Math.round(fraction * 100)}%`;
    }

    deltaClass(value) {
        if (value === null || value === undefined) {
            return "";
        }
        return value >= 0 ? "o_meta_delta_up" : "o_meta_delta_down";
    }

    deltaIcon(value) {
        if (value === null || value === undefined) {
            return "";
        }
        return value >= 0 ? "fa-arrow-up" : "fa-arrow-down";
    }

    statusPillClass(status) {
        // Colour only; the pill always shows a text label as well.
        const map = {
            success: "o_meta_pill_ok",
            skipped_idempotent: "o_meta_pill_ok",
            skipped_duplicate: "o_meta_pill_ok",
            ok: "o_meta_pill_ok",
            valid: "o_meta_pill_ok",
            running: "o_meta_pill_ok",
            pending: "o_meta_pill_warn",
            waiting: "o_meta_pill_warn",
            expiring: "o_meta_pill_warn",
            unknown: "o_meta_pill_warn",
            failed: "o_meta_pill_bad",
            invalid: "o_meta_pill_bad",
            not_running: "o_meta_pill_bad",
            stalled: "o_meta_pill_bad",
            disabled: "o_meta_pill_bad",
        };
        return map[status] || "o_meta_pill_warn";
    }

    statusLabel(status) {
        const map = {
            success: "Synced",
            skipped_idempotent: "Skipped",
            skipped_duplicate: "Skipped",
            pending: "Pending",
            failed: "Failed",
        };
        return map[status] || status;
    }

    relativeTime(value) {
        // Input is an Odoo UTC datetime string; returned as-is if it won't parse.
        if (!value) {
            return "—";
        }
        const then = new Date(value.replace(" ", "T") + "Z");
        if (isNaN(then.getTime())) {
            return value;
        }
        // Clock skew can put the time slightly in the future; show "just now".
        const secs = Math.max(0, Math.round((Date.now() - then.getTime()) / 1000));
        if (secs < 60) {
            return "just now";
        }
        const mins = Math.round(secs / 60);
        if (mins < 60) {
            return `${mins} minute${mins === 1 ? "" : "s"} ago`;
        }
        const hours = Math.round(mins / 60);
        if (hours < 24) {
            return `${hours} hour${hours === 1 ? "" : "s"} ago`;
        }
        const days = Math.round(hours / 24);
        return `${days} day${days === 1 ? "" : "s"} ago`;
    }

    shortRef(leadgenId) {
        return leadgenId ? String(leadgenId) : "—";
    }

    // No sync activity at all and nothing in the window: the module has never
    // received a lead. Otherwise an empty window just means a quiet period.
    get isFirstRun() {
        const d = this.state.data;
        return (
            !!d &&
            (!d.sync_recent || d.sync_recent.length === 0) &&
            (!d.series || d.series.length === 0) &&
            d.kpis &&
            d.kpis.new === 0
        );
    }

    get isEmptyWindow() {
        const d = this.state.data;
        return !!d && (!d.series || d.series.length === 0);
    }
}

registry.category("actions").add("meta_lead_ads.dashboard", MetaLeadsDashboard);

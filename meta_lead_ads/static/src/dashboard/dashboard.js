/**
 * Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
 * Licensed under the Odoo Proprietary License v1.0 (OPL-1). Unauthorized copying,
 * redistribution, or resale, in whole or in part, via any medium, is prohibited.
 */
//
// Read-only Leads Analytics Dashboard — OWL client action (Phase 11, D-01/D-13).
//
// Load-bearing rules:
//   - STRICTLY READ-ONLY: every affordance either
//     re-scopes the analytics cards (period selector, drill-down) or links out
//     to an existing view (View Full Sync Log). No create/write/unlink.
//   - Chart.js comes from the Odoo BUNDLE (/web/static/lib/Chart/Chart.js) — never
//     a content-delivery network / npm (threat T-11-SC supply-chain guard). We
//     prefer the already-global `globalThis.Chart`
//     and keep loadJS() of the bundled path as a guard.
//   - Chart config is v3/v4 ONLY: legend under options.plugins.legend + keyed
//     options.scales.x/y (the v2 top-level legend / per-axis arrays are forbidden).
//   - A request-sequence-id guard (`_reqSeq`) prevents a slow EARLIER response from
//     overwriting a NEWER period selection (async race). Controls disable
//     while loading.
//   - x-axis labels come from the backend `series[].bucket` + `bucket_granularity`;
//     we NEVER re-bucket dates in JS. Deltas are RELATIVE fractions; render
//     as percent and HIDE the arrow when the value is null (D-11). sync_recent +
//     System Health are GLOBAL — they do NOT re-scope with the period.
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

// Coresys brand palette (branding/coresys_branding_colors.txt) — accent #0052FE,
// ink #06152C, secondary #E4EEFC; semantic status colors for the donut tints.
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

        // Default scope = "This month" (D-09). dateFrom/dateTo only used by Custom.
        this.state = useState({
            periodMode: "month",
            dateFrom: null,
            dateTo: null,
            drilldown: "campaign", // campaign | ads | adsets (D-08)
            data: null,
            loading: true,
            error: false,
        });

        // Live Chart instances — destroyed before every re-render and on unmount.
        this._charts = [];
        // Monotonic request id: each load() captures its seq; a response is only
        // committed if its seq is still the latest (slow-earlier-wins guard).
        this._reqSeq = 0;

        onWillStart(async () => {
            // BUNDLED path — never a CDN. Guard even though Chart is usually a
            // backend global: loadJS is a no-op if already loaded.
            await loadJS("/web/static/lib/Chart/Chart.js");
            await this.load();
        });
        // Render only AFTER mount so the canvas refs exist (OWL async).
        onMounted(() => this.renderCharts());
        // Destroy every instance to avoid the classic canvas/listener leak (Pitfall 5).
        onWillUnmount(() => this._destroyCharts());
    }

    // ------------------------------------------------------------------ //
    // Data load — the single read-only backend call.
    // ------------------------------------------------------------------ //
    async load() {
        const seq = ++this._reqSeq;
        this.state.loading = true;
        this.state.error = false;
        try {
            // Third positional arg is period_mode (month/quarter/year/custom) —
            // NOT a raw groupby. The backend DERIVES bucket_granularity.
            const data = await this.orm.call("meta.account", "get_dashboard_metrics", [
                this.state.dateFrom,
                this._inclusiveDateTo(),
                this.state.periodMode,
            ]);
            // Ignore a stale (superseded) response (async race).
            if (seq !== this._reqSeq) {
                return;
            }
            this.state.data = data;
        } catch (e) {
            // Token-free error state — never echo a backend/Graph string (T-11-ID-err).
            if (seq === this._reqSeq) {
                this.state.error = true;
            }
        } finally {
            if (seq === this._reqSeq) {
                this.state.loading = false;
                // Re-render charts once data + canvas are both ready.
                this.renderCharts();
            }
        }
    }

    get Chart() {
        // Prefer the already-bundled global; loadJS in onWillStart guarantees it.
        return globalThis.Chart;
    }

    // ------------------------------------------------------------------ //
    // Chart lifecycle — v3/v4 config, destroy-before-recreate.
    // ------------------------------------------------------------------ //
    _destroyCharts() {
        this._charts.forEach((c) => {
            try {
                c.destroy();
            } catch (e) {
                // ignore — instance may already be gone
            }
        });
        this._charts = [];
    }

    renderCharts() {
        // FIRST destroy any prior instances (no leak on re-render).
        this._destroyCharts();
        const Chart = this.Chart;
        const data = this.state.data;
        if (!Chart || !data || this.state.loading || this.state.error) {
            return;
        }

        // -- leads-over-time line chart (accent series) --
        const series = data.series || [];
        if (this.lineRef.el && series.length) {
            // x labels come straight from the backend bucket (NO JS re-bucketing).
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
                        // v3/v4: legend lives under plugins (top-level legend removed).
                        plugins: { legend: { display: false } },
                        // v3/v4: keyed scales (per-axis arrays removed).
                        scales: {
                            x: { ticks: { color: BRAND_INK } },
                            y: { beginAtZero: true, ticks: { color: BRAND_INK, precision: 0 } },
                        },
                    },
                })
            );
        }

        // -- Top Campaigns donut (drill-down aware) --
        // Donut shows the FULL volume share — ALL campaigns incl. sub-threshold
        // ones (DASH-03). The min-volume guard applies to the ranking TABLE only.
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
                        // v3/v4: plugins.legend keyed config.
                        plugins: {
                            legend: { position: "bottom", labels: { color: BRAND_INK } },
                        },
                    },
                })
            );
        }
    }

    // ------------------------------------------------------------------ //
    // Drill-down selection (D-08) — bound to campaigns / drilldown.ads / .adsets.
    //
    // Two views over the same drill-down state diverge ONLY in campaign mode:
    //   * the donut shows the full volume share (ALL campaigns), while
    //   * the ranking table applies the N=10 min-volume guard (DASH-03) and so
    //     consumes the backend's pre-filtered `conversion_ranking` list.
    // Ad / ad-set drill-downs are identical for both (the backend already caps
    // them to the top-N by volume), so they share `_drilldownChildRows()`.
    // ------------------------------------------------------------------ //
    _drilldownChildRows() {
        // Ads / ad-set rows shared by donut + table; null in campaign mode so
        // each caller can pick its campaign-mode source.
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

    // Donut: full campaign volume list (no min-volume guard) — DASH-03.
    _donutRows() {
        const data = this.state.data;
        if (!data) {
            return [];
        }
        const child = this._drilldownChildRows();
        return child !== null ? child : data.campaigns || [];
    }

    // Ranking table: campaign mode uses the min-volume-guarded
    // `conversion_ranking` so sub-10-lead campaigns never surface a misleading
    // per-row conversion rate (DASH-03 — matches the card footnote).
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
        // Re-scope is a pure presentation switch over already-loaded data.
        this.renderCharts();
    }

    // ------------------------------------------------------------------ //
    // Period selector (D-10) — re-scopes the ANALYTICS cards only.
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
            // Named periods recompute immediately; custom waits for From+To.
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
        // The <input type="date"> "To" reads as inclusive to a user, but the
        // backend window is half-open [from, to). For a custom range, advance the
        // To date by one day so the whole selected To-day is counted and a
        // single-day From=To selection is a valid 1-day window (WR-01). Named
        // periods are unaffected (dateTo is null). The backend stays half-open.
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
    // Refresh — read-side recompute for the current scope (NOT a write).
    // ------------------------------------------------------------------ //
    refresh() {
        if (this.state.loading) {
            return;
        }
        this.load();
    }

    // ------------------------------------------------------------------ //
    // Link-out to the existing Sync Logs list (D-16) — read-only navigation.
    // ------------------------------------------------------------------ //
    openFullSyncLog() {
        this.action.doAction("meta_lead_ads.meta_sync_log_action");
    }

    // ------------------------------------------------------------------ //
    // Presentation helpers (template-only; no business logic).
    // ------------------------------------------------------------------ //
    formatPercent(fraction) {
        // Deltas/rates are fractions; render as a rounded percent.
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
        // Status is never color-only — the template also carries text/glyph.
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
        // Lightweight relative time for the GLOBAL sync_recent rows. Backend sends
        // an Odoo datetime string (UTC). Falls back to the raw string on parse fail.
        if (!value) {
            return "—";
        }
        const then = new Date(value.replace(" ", "T") + "Z");
        if (isNaN(then.getTime())) {
            return value;
        }
        // Future timestamps (client/server clock skew) are intentionally clamped
        // to 0 -> "just now" rather than emitting negative/"in N hours" text (WR-05).
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

    // First-run = the backend has never seen any Meta lead (empty global heartbeat
    // AND zero new in window). Otherwise an empty window is just "no leads here".
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

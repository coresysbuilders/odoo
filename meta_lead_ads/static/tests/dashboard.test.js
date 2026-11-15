import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { animationFrame, queryAllTexts } from "@odoo/hoot-dom";
import { defineMetaLeadAdsModels } from "@meta_lead_ads/../tests/meta_lead_ads_test_helpers";
import {
    defineModels,
    mountWithCleanup,
    models,
    onRpc,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";
import { assets } from "@web/core/assets";

import { MetaLeadsDashboard } from "@meta_lead_ads/dashboard/dashboard";

class MetaAccount extends models.Model {
    _name = "meta.account";
}

defineMetaLeadAdsModels();
defineModels([MetaAccount]);

// Stand-in for Chart.js. These tests only look at the markup around the
// canvases, so the chart itself never has to draw anything.
class StubChart {
    constructor(canvas, config) {
        this.canvas = canvas;
        this.config = config;
    }
    update() {}
    destroy() {}
}

beforeEach(() => {
    // The dashboard fetches Chart.js on mount. On a slow box that request
    // has outlasted the test timeout, so answer it at once and use the stub.
    patchWithCleanup(window, { Chart: StubChart });
    patchWithCleanup(assets, {
        loadJS(url) {
            if (String(url).endsWith("/Chart/Chart.js")) {
                return Promise.resolve();
            }
            return super.loadJS(...arguments);
        },
    });
});

function sig(status, label, caption = "") {
    return { status, label, caption };
}

function payload(campaigns, ranking, health = {}, conversion = {}) {
    return {
        kpis: { new: 5, synced: 5, failed: 0 },
        deltas: { new: null, synced: null, failed: null },
        conversion: {
            converted_rate: 0,
            won_rate: 0,
            created_as_opportunity: 0,
            ...conversion,
        },
        series: campaigns.length ? [{ bucket: "2026-10-01", count: 5 }] : [],
        campaigns,
        conversion_ranking: ranking,
        drilldown: { ads: [], adsets: [] },
        sync_recent: [],
        window: { from: "2026-10-01", to: "2026-11-01" },
        health: {
            webhook: sig("running", "Running", "last lead received 5 minutes ago"),
            drain: sig("ok", "Running", "last ran 1 minute ago"),
            backfill: sig("ok", "Running", "last ran 3 minutes ago"),
            token: sig("valid", "Valid", "checked 2 hours ago"),
            ...health,
        },
    };
}

// The pill of the n-th System Health row (0 = Webhook).
function healthPill(index) {
    return document.querySelectorAll(".o_meta_health_signal .o_meta_pill")[index];
}

async function mountWith(data) {
    onRpc("meta.account", "get_dashboard_metrics", () => data);
    await mountWithCleanup(MetaLeadsDashboard);
    await animationFrame();
}

function row(name, count, rates = {}) {
    return { name, count, converted_rate: 0, won_rate: 0, ...rates };
}

describe.current.tags("desktop");

test("donut card renders when every campaign has fewer than 10 leads", async () => {
    const campaigns = [row("Spring sale", 4), row("Webinar", 3), row("Retarget", 2)];
    onRpc("meta.account", "get_dashboard_metrics", () => payload(campaigns, []));

    await mountWithCleanup(MetaLeadsDashboard);
    await animationFrame();

    const wrap = document.querySelector(".o_meta_donut_wrap");
    expect(wrap).not.toBe(null);
    expect(wrap.classList.contains("d-none")).toBe(false);
    expect(".o_meta_card_donut .o_meta_dash_empty").toHaveCount(0);
    // the ranking table still leaves the small campaigns out
    expect(".o_meta_rank_table tbody tr").toHaveCount(0);
    expect(".o_meta_rank_footnote").toHaveCount(1);
});

test("ranking table only lists the campaigns the backend ranks", async () => {
    const campaigns = [row("Big", 12), row("Small", 3)];
    onRpc("meta.account", "get_dashboard_metrics", () =>
        payload(campaigns, [row("Big", 12)])
    );

    await mountWithCleanup(MetaLeadsDashboard);
    await animationFrame();

    expect(".o_meta_donut_wrap").not.toHaveClass("d-none");
    expect(".o_meta_rank_table tbody tr").toHaveCount(1);
    expect(".o_meta_rank_table tbody tr td:first").toHaveText("Big");
    expect(".o_meta_rank_footnote").toHaveCount(1);
});

test("donut card shows the empty state when there are no campaigns", async () => {
    onRpc("meta.account", "get_dashboard_metrics", () => payload([], []));

    await mountWithCleanup(MetaLeadsDashboard);
    await animationFrame();

    expect(".o_meta_card_donut .o_meta_dash_empty").toHaveCount(1);
    expect(".o_meta_card_donut .o_meta_dash_empty").toHaveText(/No Meta leads in this period/);
    expect(".o_meta_donut_wrap").toHaveClass("d-none");
});

test("health card lists webhook, queue drain, backfill and token in that order", async () => {
    await mountWith(payload([row("Spring sale", 4)], []));

    expect(queryAllTexts(".o_meta_health_signal .o_meta_health_name")).toEqual([
        "Webhook",
        "Queue drain",
        "Backfill",
        "Token",
    ]);
    expect(queryAllTexts(".o_meta_health_signal .o_meta_pill")).toEqual([
        "Running",
        "Running",
        "Running",
        "Valid",
    ]);
});

test("a quiet webhook gets a grey pill and the footnote explains it", async () => {
    await mountWith(
        payload([row("Spring sale", 4)], [], {
            webhook: sig("quiet", "Quiet", "last lead received 2 days ago"),
        })
    );

    const pill = healthPill(0);
    expect(pill).toHaveText("Quiet");
    expect(pill).toHaveClass("o_meta_pill_neutral");
    expect(pill).not.toHaveClass("o_meta_pill_warn");
    expect(pill).not.toHaveClass("o_meta_pill_bad");
    const footnote = ".o_meta_card_health .o_meta_rank_footnote";
    expect(footnote).toHaveCount(1);
    expect(footnote).toHaveText(
        /Quiet means Odoo hasn't processed a webhook lead in the last 24 hours\./
    );
    expect(footnote).toHaveText(/rejected calls from Meta/);
    expect(footnote).not.toHaveText(/turns red only/);
});

test("a webhook with no leads yet is grey too", async () => {
    await mountWith(
        payload([row("Spring sale", 4)], [], {
            webhook: sig("no_activity", "No webhook leads yet", "no success recorded yet"),
        })
    );

    expect(healthPill(0)).toHaveClass("o_meta_pill_neutral");
    expect(healthPill(0)).not.toHaveClass("o_meta_pill_warn");
    const footnote = ".o_meta_card_health .o_meta_rank_footnote";
    expect(footnote).toHaveCount(1);
    expect(footnote).toHaveText(/No successful webhook lead is recorded yet\./);
    expect(footnote).not.toHaveText(/has been processed/);
    expect(footnote).not.toHaveText(/turns red only/);
});

test("a running webhook is green and shows no footnote", async () => {
    await mountWith(payload([row("Spring sale", 4)], []));

    expect(healthPill(0)).toHaveClass("o_meta_pill_ok");
    expect(".o_meta_card_health .o_meta_rank_footnote").toHaveCount(0);
});

test("failing webhook is red and a drain backlog is amber", async () => {
    await mountWith(
        payload([row("Spring sale", 4)], [], {
            webhook: sig("failing", "Receiving, but failing", "3 failed since the last success"),
            drain: sig("backlog", "Falling behind", "4 events waiting over 10 minutes"),
            backfill: sig("stalled", "Not running", "last ran 2 hours ago"),
        })
    );

    expect(healthPill(0)).toHaveClass("o_meta_pill_bad");
    expect(healthPill(1)).toHaveClass("o_meta_pill_warn");
    expect(healthPill(2)).toHaveClass("o_meta_pill_bad");
    expect(healthPill(3)).toHaveClass("o_meta_pill_ok");
    expect(".o_meta_card_health .o_meta_rank_footnote").toHaveCount(0);
});

test("health labels and captions are shown as plain text", async () => {
    await mountWith(
        payload([row("Spring sale", 4)], [], {
            drain: sig("backlog", "<b>Falling behind</b>", "<i>4 events</i>"),
        })
    );

    expect(healthPill(1)).toHaveText("<b>Falling behind</b>");
    expect(".o_meta_card_health b").toHaveCount(0);
    expect(".o_meta_card_health i").toHaveCount(0);
});

test("each KPI says which date it counts by", async () => {
    await mountWith(payload([row("Spring sale", 4)], []));

    expect(queryAllTexts(".o_meta_kpi .o_meta_kpi_basis")).toEqual([
        "By Meta submission date",
        "By date processed in Odoo",
        "Unresolved, by date processed in Odoo",
    ]);
});

test("two ranking rows with the same name both render", async () => {
    const rows = [row("Same", 14), row("Same", 11)];
    await mountWith(payload(rows, rows));

    expect(".o_meta_rank_table tbody tr").toHaveCount(2);
    expect(queryAllTexts(".o_meta_rank_table tbody tr td:first-child")).toEqual([
        "Same",
        "Same",
    ]);
});

test("the ranking footnote says rows are grouped by Meta ID", async () => {
    await mountWith(payload([row("Big", 12)], [row("Big", 12)]));

    expect(".o_meta_card_ranking .o_meta_rank_footnote").toHaveText(/grouped by Meta ID/);
});

test("the conversion card says it counts leads converted from a lead", async () => {
    await mountWith(payload([row("Spring sale", 4)], []));

    expect(".o_meta_card_conversion").toHaveText(/Converted from lead/);
    expect(".o_meta_card_conversion").not.toHaveText(/Converted to opportunity/);
});

test("the conversion card shows how many leads were created as opportunities", async () => {
    await mountWith(
        payload([row("Spring sale", 4)], [], {}, { created_as_opportunity: 42 })
    );

    expect(queryAllTexts(".o_meta_card_conversion .o_meta_kpi_basis")).toEqual([
        "Created as opportunity · 42",
    ]);
});

test("an empty converted rate shows a dash and says why", async () => {
    await mountWith(
        payload([row("Spring sale", 4)], [], {}, { converted_rate: null, won_rate: 0.5 })
    );

    expect(queryAllTexts(".o_meta_card_conversion .o_meta_conv_value")).toEqual([
        "· —",
        "· 50%",
    ]);
    expect(".o_meta_card_conversion").toHaveText(/No leads to convert in this period/);
    expect(".o_meta_card_conversion").not.toHaveText(/No opportunities in this period/);
});

test("an empty won rate shows a dash and says why", async () => {
    await mountWith(
        payload([row("Spring sale", 4)], [], {}, { converted_rate: 0.25, won_rate: null })
    );

    expect(queryAllTexts(".o_meta_card_conversion .o_meta_conv_value")).toEqual([
        "· 25%",
        "· —",
    ]);
    expect(".o_meta_card_conversion").toHaveText(/No opportunities in this period/);
    expect(".o_meta_card_conversion").not.toHaveText(/No leads to convert in this period/);
});

test("a zero converted rate shows 0% and no caption", async () => {
    await mountWith(
        payload([row("Spring sale", 4)], [], {}, { converted_rate: 0, won_rate: 0 })
    );

    expect(queryAllTexts(".o_meta_card_conversion .o_meta_conv_value")).toEqual([
        "· 0%",
        "· 0%",
    ]);
    expect(".o_meta_card_conversion").not.toHaveText(/No leads to convert in this period/);
    expect(".o_meta_card_conversion").not.toHaveText(/No opportunities in this period/);
});

test("the ranking marks an empty rate with a dash", async () => {
    const rows = [row("Direct", 12, { converted_rate: null, won_rate: 0.25 })];
    await mountWith(payload(rows, rows));

    expect(queryAllTexts(".o_meta_rank_table thead th")).toEqual([
        "Name",
        "Leads",
        "Converted",
        "Won rate",
    ]);
    expect(queryAllTexts(".o_meta_rank_table tbody tr td")).toEqual([
        "Direct",
        "12",
        "—",
        "25%",
    ]);
    expect(".o_meta_card_ranking .o_meta_rank_footnote").toHaveText(
        /A dash means there was nothing to measure/
    );
});

test("the period empty state talks about submission dates", async () => {
    await mountWith(payload([], []));

    expect(".o_meta_card_chart .o_meta_dash_empty").toHaveText(/were submitted between/);
    expect(".o_meta_card_donut .o_meta_dash_empty").toHaveText(/were submitted between/);
});

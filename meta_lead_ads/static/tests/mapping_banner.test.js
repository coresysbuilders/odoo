import { describe, expect, test } from "@odoo/hoot";
import { advanceTime, animationFrame } from "@odoo/hoot-dom";
import { defineMetaLeadAdsModels } from "@meta_lead_ads/../tests/meta_lead_ads_test_helpers";
import {
    contains,
    defineModels,
    fields,
    makeServerError,
    models,
    mountView,
    onRpc,
    patchWithCleanup,
    toggleActionMenu,
    toggleMenuItem,
} from "@web/../tests/web_test_helpers";
import { MetaMappingListRenderer } from "@meta_lead_ads/mapping_list/mapping_list";

class MetaFieldMapping extends models.Model {
    _name = "meta.field.mapping";

    meta_key = fields.Char();
    active = fields.Boolean({ default: true });
    disabled_reason = fields.Char();

    _records = [
        { id: 1, meta_key: "favourite_colour", active: true },
        { id: 2, meta_key: "owner", active: false, disabled_reason: "Turned off" },
        { id: 3, meta_key: "branch", active: false, disabled_reason: "Turned off" },
    ];
}

defineMetaLeadAdsModels();
defineModels([MetaFieldMapping]);

const ARCH = `
    <list js_class="meta_mapping_list" editable="bottom">
        <field name="meta_key"/>
    </list>`;

const BANNER_TEXT =
    "Some mappings were turned off because their fields can no longer be " +
    "filled from a form answer. The answers are still saved on each lead. " +
    "Use the Disabled filter to see them and why.";

const BANNER = ".alert";

async function mountMappingList(params = {}) {
    await mountView({ type: "list", resModel: "meta.field.mapping", arch: ARCH, ...params });
    await animationFrame();
}

// Longer than the renderer's debounce, so a pending count request has run.
async function settleCount() {
    await advanceTime(200);
    await animationFrame();
}

function countCalls(results) {
    const calls = [];
    onRpc("meta.field.mapping", "search_count", ({ args, kwargs }) => {
        calls.push({ domain: args[0], context: kwargs.context });
        return results.length > 1 ? results.shift() : results[0];
    });
    return calls;
}

describe.current.tags("desktop");

test("mapping list shows the banner when a mapping was turned off", async () => {
    onRpc("meta.field.mapping", "search_count", () => 1);

    await mountMappingList();

    expect(".alert.alert-warning[role=alert]").toHaveCount(1);
    expect(".alert.alert-warning[role=alert]").toHaveText(BANNER_TEXT);
    expect(".alert.alert-warning[role=alert]").toHaveAttribute(
        "title",
        /every form you can see/
    );
    // above the table, not inside it
    expect(".o_list_renderer .alert").toHaveCount(0);
});

test("mapping list has no banner when nothing was turned off", async () => {
    onRpc("meta.field.mapping", "search_count", () => 0);

    await mountMappingList();

    expect(BANNER).toHaveCount(0);
    expect(".o_data_row").toHaveCount(1);
});

test("mapping list still loads when the banner count fails", async () => {
    onRpc("meta.field.mapping", "search_count", () => {
        throw makeServerError({ message: "count failed" });
    });

    await mountMappingList();

    expect(".o_list_renderer").toHaveCount(1);
    expect(".o_data_row").toHaveCount(1);
    expect(".o_data_row .o_data_cell:first").toHaveText("favourite_colour");
    expect(BANNER).toHaveCount(0);
    expect(".o_error_dialog").toHaveCount(0);
    expect(".modal").toHaveCount(0);
});

test("banner counts disabled rows with archived ones included", async () => {
    const calls = [];
    onRpc("meta.field.mapping", "search_count", ({ args, kwargs }) => {
        calls.push({ domain: args[0], context: kwargs.context });
        return 0;
    });

    await mountMappingList();

    expect(calls).toHaveLength(1);
    expect(calls[0].domain).toEqual([
        ["active", "=", false],
        ["disabled_reason", "!=", false],
    ]);
    expect(calls[0].context.active_test).toBe(false);
});

test("mapping list stays editable with the banner on", async () => {
    onRpc("meta.field.mapping", "search_count", () => 1);

    await mountMappingList();
    await contains(".o_data_row .o_data_cell").click();

    expect(".o_data_row.o_selected_row").toHaveCount(1);
    expect(".o_selected_row .o_field_widget[name=meta_key] input").toHaveCount(1);
    expect(".alert.alert-warning[role=alert]").toHaveCount(1);
});

test("banner count reloads after the list changes", async () => {
    const calls = countCalls([2, 1, 0]);

    await mountMappingList({ actionMenus: {}, domain: [["active", "=", false]] });
    expect(".o_data_row").toHaveCount(2);
    expect(BANNER).toHaveCount(1);

    // Turn one row back on: the list reloads and the count is read again.
    await contains(".o_data_row:eq(0) .o_list_record_selector input").click();
    await toggleActionMenu();
    await toggleMenuItem("Unarchive");
    await settleCount();
    expect(".o_data_row").toHaveCount(1);
    expect(calls).toHaveLength(2);
    expect(BANNER).toHaveCount(1);

    // The last one: the count drops to 0 and the banner goes away.
    await contains(".o_data_row:eq(0) .o_list_record_selector input").click();
    await toggleActionMenu();
    await toggleMenuItem("Unarchive");
    await settleCount();
    expect(calls).toHaveLength(3);
    expect(BANNER).toHaveCount(0);
});

test("banner count reloads after a row is saved", async () => {
    const calls = countCalls([1, 0]);

    await mountMappingList();
    expect(BANNER).toHaveCount(1);

    await contains(".o_data_row .o_data_cell").click();
    await contains(".o_selected_row .o_field_widget[name=meta_key] input").edit("colour");
    await contains(".o_list_button_save").click();
    await settleCount();

    expect(calls).toHaveLength(2);
    expect(BANNER).toHaveCount(0);
});

test("banner counts only the current form", async () => {
    const calls = countCalls([1]);

    await mountMappingList({ context: { default_form_id: 7 } });

    expect(calls).toHaveLength(1);
    expect(calls[0].domain).toEqual([
        ["active", "=", false],
        ["disabled_reason", "!=", false],
        ["form_id", "=", 7],
    ]);
    expect(calls[0].context.active_test).toBe(false);
    expect(".alert.alert-warning[role=alert]").toHaveAttribute("title", /on this form/);
});

test("banner counts every form without a form in the context", async () => {
    const calls = countCalls([1]);

    await mountMappingList();

    expect(calls).toHaveLength(1);
    expect(calls[0].domain.some(([name]) => name === "form_id")).toBe(false);
    expect(".alert.alert-warning[role=alert]").toHaveAttribute(
        "title",
        /every form you can see/
    );
});

test("a burst of list updates makes one count request", async () => {
    let renderer;
    patchWithCleanup(MetaMappingListRenderer.prototype, {
        setup() {
            super.setup(...arguments);
            renderer = this;
        },
    });
    const calls = countCalls([1]);

    await mountMappingList();
    expect(calls).toHaveLength(1);

    const bus = renderer.props.list.model.bus;
    bus.trigger("update");
    bus.trigger("update");
    bus.trigger("update");
    expect(calls).toHaveLength(1);

    await settleCount();
    expect(calls).toHaveLength(2);
});

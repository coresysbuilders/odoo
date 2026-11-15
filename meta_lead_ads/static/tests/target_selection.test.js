import { describe, expect, test } from "@odoo/hoot";
import { animationFrame, queryAllTexts } from "@odoo/hoot-dom";
import { defineMetaLeadAdsModels } from "@meta_lead_ads/../tests/meta_lead_ads_test_helpers";
import {
    contains,
    defineModels,
    fields,
    models,
    mountView,
} from "@web/../tests/web_test_helpers";

class MetaFieldMapping extends models.Model {
    _name = "meta.field.mapping";

    meta_key = fields.Char();
    crm_field_name = fields.Selection({
        selection: [
            ["city", "City (city)"],
            ["street", "Street (street)"],
            ["user_id", "Salesperson (user_id)"],
            ["team_id", "Sales Team (team_id)"],
        ],
    });
    unoffered_targets = fields.Json();
    active = fields.Boolean({ default: true });

    _records = [
        { id: 1, meta_key: "city_q", crm_field_name: "city",
          unoffered_targets: ["user_id", "team_id"] },
        { id: 2, meta_key: "owner_q", crm_field_name: "user_id",
          unoffered_targets: ["user_id", "team_id"] },
    ];
}

defineMetaLeadAdsModels();
defineModels([MetaFieldMapping]);

const ARCH = `
    <list editable="bottom">
        <field name="meta_key"/>
        <field name="crm_field_name" widget="meta_target_selection"/>
    </list>`;

async function openRow(index) {
    await mountView({ type: "list", resModel: "meta.field.mapping", arch: ARCH });
    await contains(`.o_data_row:eq(${index}) .o_data_cell:eq(1)`).click();
    await animationFrame();
}

// Odoo 18 renders the picker as a native select whose first option is the
// empty choice.
function offeredLabels() {
    return queryAllTexts(
        ".o_selected_row .o_field_widget[name=crm_field_name] select option"
    ).filter(Boolean);
}

describe.current.tags("desktop");

test("picker hides fields that can't be picked any more", async () => {
    await openRow(0);

    expect(offeredLabels()).toEqual([
        "City (city)",
        "Street (street)",
    ]);
});

test("picker keeps the row's own field and still hides the others", async () => {
    await openRow(1);

    expect(offeredLabels()).toEqual([
        "City (city)",
        "Street (street)",
        "Salesperson (user_id)",
    ]);
});

test("read-only cells keep the label of a field no longer offered", async () => {
    await mountView({ type: "list", resModel: "meta.field.mapping", arch: ARCH });

    expect(".o_data_row:eq(1) .o_data_cell:eq(1)").toHaveText("Salesperson (user_id)");
});

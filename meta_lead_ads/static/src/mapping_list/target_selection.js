/**
 * Meta Lead Ads Integration for Odoo - (c) 2026 CoreSys Builders. All rights reserved.
 * Licensed under the Odoo Proprietary License v1.0 (OPL-1). Unauthorized copying,
 * redistribution, or resale, in whole or in part, via any medium, is prohibited.
 */
//
// CRM Lead Field picker. The selection also lists fields that existing rows
// still point at, so their cells keep a label. Those fields can't be picked
// any more, so the dropdown only shows one when it is the row's own value.
//
import { registry } from "@web/core/registry";
import { SelectionField, selectionField } from "@web/views/fields/selection/selection_field";

export class MetaTargetSelectionField extends SelectionField {
    get options() {
        const unoffered = this.props.record.data.unoffered_targets || [];
        const current = this.props.record.data[this.props.name];
        return super.options.filter(
            ([value]) => value === current || !unoffered.includes(value)
        );
    }
}

registry.category("fields").add("meta_target_selection", {
    ...selectionField,
    component: MetaTargetSelectionField,
    fieldDependencies: [{ name: "unoffered_targets", type: "json" }],
});

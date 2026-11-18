/**
 * Meta Lead Ads Integration for Odoo - (c) 2026 CoreSys Builders. All rights reserved.
 * Licensed under the Odoo Proprietary License v1.0 (OPL-1). Unauthorized copying,
 * redistribution, or resale, in whole or in part, via any medium, is prohibited.
 */
//
// Field Mappings list with a banner above the rows when some mappings were
// turned off. Odoo 18 and later have no banner_route on views any more, so the
// banner lives in a small list renderer. Everything else is the stock list,
// editing included.
//
// The count is read again after the list changes (a save, an unarchive, a
// delete, a new search), so fixing the last row hides the banner without
// reopening the menu. Opened from a form, it counts only that form's rows.
//
import { onWillDestroy, onWillStart, status, proxy } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useBus, useService } from "@web/core/utils/hooks";
import { useDebounced } from "@web/core/utils/timing";
import { listView } from "@web/views/list/list_view";
import { ListRenderer } from "@web/views/list/list_renderer";

// Model hooks that run after a reload or a save. Unarchive, delete and
// saves don't fire the model's "update" event, so these are followed too.
const RELOAD_HOOKS = ["onRootLoaded", "onRecordSaved"];

export class MetaMappingListRenderer extends ListRenderer {
    static template = "meta_lead_ads.MappingListRenderer";

    setup() {
        super.setup();
        this.orm = useService("orm");
        this.bannerState = proxy({ disabledCount: 0 });
        this.countRequest = 0;
        // One save can reload the list and fire several events; they are
        // folded into one count request.
        this.reloadDisabledCount = useDebounced(() => this.loadDisabledCount(), 150);
        onWillStart(() => this.loadDisabledCount());
        useBus(this.props.list.model.bus, "update", () => this.reloadDisabledCount());
        this.followModelHooks(this.props.list.model);
    }

    followModelHooks(model) {
        const hooks = model.hooks;
        const wrapped = {};
        for (const name of RELOAD_HOOKS) {
            const original = hooks[name];
            const hook = async (...args) => {
                const result = await original(...args);
                if (status(this) !== "destroyed") {
                    this.reloadDisabledCount();
                }
                return result;
            };
            wrapped[name] = { original, hook };
            hooks[name] = hook;
        }
        onWillDestroy(() => {
            for (const [name, { original, hook }] of Object.entries(wrapped)) {
                // Leave a hook alone if something wrapped it after us.
                if (hooks[name] === hook) {
                    hooks[name] = original;
                }
            }
        });
    }

    get formId() {
        return this.props.list.context.default_form_id || false;
    }

    get bannerTitle() {
        return this.formId
            ? _t("Counts disabled mappings on this form, not only the rows listed here.")
            : _t(
                  "Counts disabled mappings on every form you can see, not only the rows listed here."
              );
    }

    async loadDisabledCount() {
        // Only the latest request may set the count, so a slow answer to an
        // older one can't bring the banner back.
        const request = ++this.countRequest;
        const domain = [
            ["active", "=", false],
            ["disabled_reason", "!=", false],
        ];
        if (this.formId) {
            domain.push(["form_id", "=", this.formId]);
        }
        let count = 0;
        // The banner is only a notice and this list is where mappings get
        // fixed, so a failed count shows no banner instead of no list.
        try {
            count = await this.orm.searchCount("meta.field.mapping", domain, {
                context: { active_test: false },
            });
        } catch {
            console.debug("meta_lead_ads: disabled mapping count not loaded");
        }
        if (request === this.countRequest && status(this) !== "destroyed") {
            this.bannerState.disabledCount = count;
        }
    }
}

registry.category("views").add("meta_mapping_list", {
    ...listView,
    Renderer: MetaMappingListRenderer,
});

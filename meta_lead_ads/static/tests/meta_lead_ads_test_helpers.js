import { before, delay } from "@odoo/hoot";
import { defineMailModels } from "@mail/../tests/mail_test_helpers";
import { fetchModelDefinitions } from "@web/../tests/_framework/module_set.hoot";

// The definitions request normally answers in well under a second. After
// this long it is taken as stuck and sent again.
const DEFINITIONS_WAIT_MS = 5000;
const DEFINITIONS_TRIES = 3;

/**
 * Defines the mail models these tests run with and loads their server
 * definitions before the first test of the file.
 *
 * Left to the mock server, the definitions are fetched inside the first test.
 * In the headless browser that first request now and then never completes,
 * even though the server has sent the whole response, and the test times out
 * with no assertion run. The same request sent again goes through, so it is
 * retried here, outside any test.
 */
export function defineMetaLeadAdsModels() {
    defineMailModels();
    before(async () => {
        for (let attempt = 1; attempt <= DEFINITIONS_TRIES; attempt++) {
            const loaded = await Promise.race([
                fetchModelDefinitions([]).then(() => true),
                delay(DEFINITIONS_WAIT_MS).then(() => false),
            ]);
            if (loaded) {
                return;
            }
        }
        throw new Error(`Server model definitions did not load in ${DEFINITIONS_TRIES} tries`);
    });
}

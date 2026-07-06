<meta name="author" content="CoreSys Builders">

# Smart Approvals — Screenshot Shot-List

Marketplace and doc-site screenshots for the **Smart Approvals** listing (the core
engine plus its purchase bridge). Follow the shots in order — later shots depend on
the state produced by earlier ones.

## Before you start

- Create a fresh database **with Demo data enabled** and install both modules:
  `coresys_approvals` and `coresys_approvals_purchase`. Both demo files load only on
  a demo-enabled database, so a plain production install stays clean.
- The demo data seeds Ranborne Equipment Co. — a buyer (Martin Weber), a department
  manager (Claire Devine), a finance director (Harold Nkemelu), a Capital Expenditure
  Approval category (any purchase order above 10,000), a supplier (Meridian Industrial
  Supply), two catalogue items, and one purchase order that trips the threshold.
- To capture a shot as a demo user, set that user a password under
  Settings → Users, or use the developer **Log in as** action to impersonate them.
- **PNG capture is a manual step performed by you** on the running instance — this
  file only tells you what to open, in which state, and as whom. Save each image with
  the shot number so it slots straight into the doc-site *Screenshots* section.

## Shots

| # | Screen to open | State it must show | Log in as |
|---|----------------|--------------------|-----------|
| 1 | Approvals app → main menu | The app landing with the *To Approve*, *Requests*, *My Delegations* and *Configuration* menus | Harold Nkemelu (manager) |
| 2 | Approvals → Configuration → Approval Categories (list) | Both seeded categories: *New Vendor Onboarding* and *Capital Expenditure Approval* | Administrator |
| 3 | Open the *Capital Expenditure Approval* category form | Target document *Purchase Order*, the amount-total condition, and the two ordered levels (Department Manager, Finance Director) | Administrator |
| 4 | Purchase → Orders → open the seeded Meridian Industrial Supply order | A draft PO over 10,000 with the *Submit for Approval* stat button visible and no approval yet | Martin Weber (buyer) |
| 5 | On that same PO, click the standard **Confirm** button | The gate trips: the PO stays in draft, a "requires approval" notification appears, and an approval request is now pending | Martin Weber (buyer) |
| 6 | Reopen the PO after step 5 | The **Approval** stat button now shows the *To Approve* state (the order is held pending sign-off) | Martin Weber (buyer) |
| 7 | Approvals → To Approve (inbox) | The pending Capital Expenditure request awaiting the first level | Claire Devine (department manager) |
| 8 | Open that request from the inbox | The request form with the level sequence, the active first level highlighted, and the Approve / Refuse buttons | Claire Devine (department manager) |
| 9 | Approvals → To Approve (inbox) after Claire approves | The same request now routed to the second level | Harold Nkemelu (finance director) |
| 10 | Open the request → Decision History tab | The immutable audit trail of the submit and each decision, with who decided and when | Harold Nkemelu (finance director) |
| 11 | Approvals → My Delegations → new delegation | The delegation form (hand approval authority to a colleague for a date window) | Claire Devine (department manager) |
| 12 | Reopen the PO after the final approval | The Approval stat button in the *Approved* state and the order now confirmable / confirmed | Martin Weber (buyer) |

## Notes on the gated-request shots

Shots 5–10 depend on a live pending request. The demo data does **not** pre-create the
approval request; instead you produce it by clicking **Confirm** on the seeded purchase
order (shot 5). That is the real gate path — the order is auto-submitted for approval and
held, which is exactly what the "gated PO" and "request awaiting approval" screenshots
should show. (The *Submit for Approval* stat button on the PO is an equivalent entry point
if you prefer to trigger it explicitly.)

---

*CoreSys Builders — https://coresysbuilders.com — Smart Approvals. Licensed OPL-1.*

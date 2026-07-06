<!-- CoreSys Builders — https://coresysbuilders.com | License OPL-1 -->
<meta name="author" content="CoreSys Builders">

# Smart Approvals

Route any Odoo document through a configurable, multi-level approval chain — with a
full audit trail, delegation, and a personal "To Approve" inbox for every approver.

Smart Approvals is a bundled product from CoreSys Builders. It ships as two modules
that are sold and installed together:

- **`coresys_approvals`** — the approval engine and the `coresys.approval.mixin` you
  attach to any model.
- **`coresys_approvals_purchase`** — the Purchase Order bridge, which gates PO
  confirmation through the engine and serves as the live demonstration of how the
  mixin plugs into a real Odoo workflow.

Built for Odoo **18.0** and **19.0** Community. No external dependencies — the engine
depends only on `base` and `mail`.

## What it does

- **Configurable approval categories and levels.** Define an approval category once,
  give it as many sequential levels as the process needs, and point each level at the
  group or the specific users who sign off on it. No code, no custom development per
  workflow.
- **A guarded state machine.** Requests move through draft, submitted, approved, and
  refused states along a single, controlled path. Every write that would skip a step
  or forge a decision is rejected at the ORM boundary, so the process cannot be routed
  around from an import, a script, or a crafted context.
- **An immutable audit trail.** Each decision is recorded as a create-only response
  line — who acted, at which level, the outcome, and the reason. Records cannot be
  edited or deleted afterward, even by an administrator, which is exactly what an
  auditor expects to see.
- **Delegation.** An approver going on leave can delegate their authority for a period.
  Delegated approvals are attributed to both the delegate who acted and the original
  approver, so the trail stays honest.
- **A "To Approve" inbox.** Every approver gets a filtered list of what is waiting on
  them right now, backed by native Odoo to-do activities so reminders show up where
  users already look.
- **Chatter on every request.** The standard Odoo chatter gives each request a
  human-readable timeline alongside the structured audit log.

## Supported versions

| Series | Status |
|--------|--------|
| Odoo 18.0 Community | Supported |
| Odoo 19.0 Community | Supported |

The two series are maintained on parallel branches. Pick the branch that matches your
Odoo version.

## Installation

Smart Approvals installs by copying, like any standard Odoo module — there is nothing
to `pip install` and no build step.

1. Copy `coresys_approvals` (and, for the Purchase Order gate, `coresys_approvals_purchase`)
   into your Odoo addons path.
2. Restart the Odoo service and update the apps list.
3. Install **Multi-Level Approvals** from the Apps menu. Install **Approvals for
   Purchase** as well if you want the Purchase Order confirmation gate.

## Configuration

1. Open **Approvals → Configuration → Categories** and create a category for the
   process you want to control (for example, capital expenditure sign-off).
2. Add the levels the request must pass through, in order. For each level, choose the
   approver group or name the specific approvers.
3. If you installed the Purchase bridge, link the category to purchase orders so a PO
   must be approved before it can be confirmed.
4. Approvers will find everything waiting on them under **Approvals → To Approve**.

## Support

Questions, licensing, or help with a rollout: **https://coresysbuilders.com**.

---

<!-- CoreSys Builders — https://coresysbuilders.com | License OPL-1 -->
Built by [CoreSys Builders](https://coresysbuilders.com). Licensed under OPL-1.

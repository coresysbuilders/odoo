# Changelog

All notable changes to Smart Approvals are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/).

## [18.0.1.0.0] — Initial release

### Added

- Configurable approval categories with sequential, multi-level approval chains that
  apply to any target model through `coresys.approval.mixin`.
- Per-level approvers set by group or by named users.
- A guarded request lifecycle (draft, submitted, approved, refused) with every
  out-of-band write rejected at the ORM boundary.
- An immutable, create-only decision audit log capturing the actor, level, outcome,
  and reason for each approval or refusal.
- Delegation of approval authority for a period, with decisions attributed to both the
  delegate and the original approver.
- A personal "To Approve" inbox backed by native Odoo to-do activities.
- Chatter on every request for a human-readable timeline.
- Purchase Order confirmation gate via the bundled `coresys_approvals_purchase` bridge.

---

<!-- CoreSys Builders — https://coresysbuilders.com | License OPL-1 -->
<meta name="author" content="CoreSys Builders">
Built by [CoreSys Builders](https://coresysbuilders.com). Licensed under OPL-1.

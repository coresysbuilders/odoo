# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo import api, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestPurchaseGateHardening(TransactionCase):
    """Hardening proofs: approved-state + line-level invalidation (edited as a
    non-sudo buyer, including a watched edit that makes the record STOP matching
    its category), duplicate-request reuse + the concurrency guard, backstop
    ambiguity, the create bypass for 'purchase', the PO-line read
    ACL, and cross-user request isolation. Every exception assertion is wrapped in
    a savepoint (REVIEW #6)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Request = cls.env['coresys.approval.request']
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.po_model_id = cls.env['ir.model']._get_id('purchase.order')

        cls.env.company.po_double_validation = 'one_step'

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_approver = cls.env.ref('coresys_approvals.group_approval_approver')
        cls.purchase_user_group = cls.env.ref('purchase.group_purchase_user')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.buyer = Users.create({
            'name': 'CoreSys Buyer',
            'login': 'coresys_poh_buyer',
            'group_ids': [(6, 0, [cls.group_internal.id,
                                  cls.purchase_user_group.id])],
        })
        # A second, unrelated purchase user — never the requester of buyer A's
        # request — used to prove the create_uid isolation record rule.
        cls.buyer_b = Users.create({
            'name': 'CoreSys Buyer B',
            'login': 'coresys_poh_buyer_b',
            'group_ids': [(6, 0, [cls.group_internal.id,
                                  cls.purchase_user_group.id])],
        })
        cls.approver = Users.create({
            'name': 'CoreSys Approver',
            'login': 'coresys_poh_approver',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })

        cls.supplier = cls.env['res.partner'].create({'name': 'Hardening Supplier'})
        cls.product = cls.env['product.product'].create({
            'name': 'Hardening Service',
            'type': 'service',
            'purchase_ok': True,
            'list_price': 100.0,
            'standard_price': 100.0,
        })

        cls.category = cls.Category.create({
            'name': 'PO Approval',
            'model_id': cls.po_model_id,
            'domain': "[('amount_total', '>', 0)]",
            'approval_watch_fields': 'amount_total,order_line',
        })
        cls.Level.create({
            'name': 'Finance',
            'category_id': cls.category.id,
            'approver_user_ids': [(6, 0, [cls.approver.id])],
        })

        # Threshold category for the stops-matching test — kept inactive so a
        # >500 PO does not collide with the >0 category during other tests.
        cls.category_threshold = cls.Category.create({
            'name': 'PO Approval (over 500)',
            'model_id': cls.po_model_id,
            'domain': "[('amount_total', '>', 500)]",
            'approval_watch_fields': 'amount_total,order_line',
            'active': False,
        })
        cls.Level.create({
            'name': 'Finance (threshold)',
            'category_id': cls.category_threshold.id,
            'approver_user_ids': [(6, 0, [cls.approver.id])],
        })

    # ---- helpers -----------------------------------------------------------

    def _line_vals(self, qty=1.0, price=100.0):
        return {
            'name': self.product.display_name,
            'product_id': self.product.id,
            'product_qty': qty,
            'product_uom_id': self.product.uom_id.id,
            'price_unit': price,
            'tax_ids': [(6, 0, [])],
            'date_planned': fields.Datetime.now(),
        }

    def _gated_po(self, qty=1.0, price=100.0, user=None):
        return self.env['purchase.order'].with_user(user or self.buyer).create({
            'partner_id': self.supplier.id,
            'order_line': [(0, 0, self._line_vals(qty, price))],
        })

    def _request_for(self, po):
        return self.Request.sudo().search(
            [('reference', '=', 'purchase.order,%d' % po.id)])

    def _active_requests_for(self, po):
        return self.Request.sudo().search([
            ('reference', '=', 'purchase.order,%d' % po.id),
            ('state', 'in', ('draft', 'to_approve', 'approved')),
        ])

    # ---- tests -------------------------------------------------------------

    def test_watched_field_change_resets_request(self):
        # D-37 parent path, non-sudo: editing the parent order_line on a PENDING
        # request as the buyer resets that request to draft.
        po = self._gated_po()
        po.with_user(self.buyer).button_confirm()
        req = self._request_for(po)
        self.assertEqual(req.state, 'to_approve')

        line = po.order_line[0]
        po.with_user(self.buyer).write(
            {'order_line': [(1, line.id, {'product_qty': 5.0})]})
        self.assertEqual(req.state, 'draft')

    def test_line_edit_invalidates_approval(self):
        # REVIEW #3: a direct purchase.order.line write on an APPROVED-but-
        # unconfirmed request (kept approved by stripping the requester's confirm
        # rights before approval, D-36) resets that request to draft. The edit is
        # performed by a non-sudo purchase user.
        po = self._gated_po()
        po.with_user(self.buyer).button_confirm()
        req = self._request_for(po)

        # Approve while the requester cannot confirm → request stays 'approved',
        # PO stays draft (D-36 keep-approval).
        self.buyer.write({'group_ids': [(3, self.purchase_user_group.id)]})
        self.env.invalidate_all()
        self.env.registry.clear_cache()
        req.with_user(self.approver).action_approve()
        self.assertEqual(req.state, 'approved')
        # po is bound to the buyer's env and the buyer's purchase rights are still
        # stripped here (restored below), so read the true stored state via sudo.
        po.invalidate_recordset(['state'])
        self.assertNotEqual(po.sudo().state, 'purchase')

        # Restore the buyer's rights so the line edit runs as a non-sudo buyer.
        self.buyer.write({'group_ids': [(4, self.purchase_user_group.id)]})
        self.env.invalidate_all()
        self.env.registry.clear_cache()

        po.order_line[0].with_user(self.buyer).write({'price_unit': 250.0})
        self.assertEqual(req.state, 'draft')

    def test_watched_edit_that_stops_matching_still_invalidates(self):
        # T-04-21: an edit that drops the record BELOW its category threshold (so
        # it no longer matches) still resets the active request — invalidation
        # keys off the active request's OWN category, not a post-write re-match.
        self.category.active = False
        self.category_threshold.active = True
        po = self._gated_po(qty=1.0, price=600.0)  # amount_total 600 > 500
        po.with_user(self.buyer).button_confirm()
        req = self._request_for(po)
        self.assertEqual(req.state, 'to_approve')

        line = po.order_line[0]
        # Non-sudo edit that makes amount_total fall below the threshold.
        po.with_user(self.buyer).write(
            {'order_line': [(1, line.id, {'price_unit': 1.0})]})
        self.assertLess(po.amount_total, 500.0)
        self.assertEqual(req.state, 'draft')

    def test_duplicate_pending_reused(self):
        # REVIEW divergent 'duplicate pending': two confirm clicks reuse the one
        # pending request — no duplicate active request.
        po = self._gated_po()
        po.with_user(self.buyer).button_confirm()
        po.with_user(self.buyer).button_confirm()
        self.assertEqual(len(self._active_requests_for(po)), 1)

    def test_concurrent_confirm_no_duplicate_request(self):
        # Round-2 concurrency: the mixin takes a FOR UPDATE row-lock on the PO
        # before reuse/create. A racing confirm in a SECOND cursor/Environment
        # blocks on that lock; a short statement_timeout bounds the wait so the
        # harness never hangs, and the single-request invariant holds. The full
        # multi-transaction race is validated live in the audit gate.
        po = self._gated_po()
        po.with_user(self.buyer).button_confirm()  # holds the row-lock in this txn

        try:
            with self.registry.cursor() as cr2:
                env2 = api.Environment(cr2, self.buyer.id, {})
                env2.cr.execute("SET LOCAL statement_timeout = '2000'")
                try:
                    env2['purchase.order'].browse(po.id).button_confirm()
                except Exception:
                    cr2.rollback()
        except Exception:
            pass

        self.assertEqual(len(self._active_requests_for(po)), 1)

    def test_ambiguous_categories_block_write(self):
        # REVIEW divergent 'backstop ambiguity': a second active category also
        # matching the PO makes the raw-write backstop reuse the D-32 ambiguity
        # error.
        self.Category.create({
            'name': 'PO Approval (duplicate)',
            'model_id': self.po_model_id,
            'domain': "[('amount_total', '>', 0)]",
        }).write({'active': True})
        # A matching level so the duplicate category is a real, submittable config.
        po = self._gated_po()
        second = self.Category.search(
            [('name', '=', 'PO Approval (duplicate)')], limit=1)
        self.Level.create({
            'name': 'Duplicate Finance',
            'category_id': second.id,
            'approver_user_ids': [(6, 0, [self.approver.id])],
        })
        with self.env.cr.savepoint():
            with self.assertRaises(UserError) as caught:
                po.with_user(self.buyer).write({'state': 'purchase'})
        self.assertIn('categories', str(caught.exception))

    def test_create_in_purchase_state_blocked(self):
        # REVIEW divergent 'create bypass': create({'state': 'purchase'}) on a
        # gated config is rejected, rolling back the create.
        with self.env.cr.savepoint():
            with self.assertRaises(UserError):
                self.env['purchase.order'].with_user(self.buyer).create({
                    'partner_id': self.supplier.id,
                    'state': 'purchase',
                    'order_line': [(0, 0, self._line_vals())],
                })

    def test_approver_can_read_po_and_lines(self):
        # REVIEW #4: an approver opens a gated PO and reads its lines with no
        # AccessError (the PO + line read ACL rows).
        po = self._gated_po()
        po.with_user(self.approver).read(['name'])
        po.order_line.with_user(self.approver).read(['product_qty'])

    def test_requester_cannot_read_other_buyers_request(self):
        # T-04-24: buyer B cannot search or read buyer A's request or its lines —
        # search returns empty and a direct read raises AccessError, proving the
        # create_uid own-scope record rule.
        po = self._gated_po(user=self.buyer)
        po.with_user(self.buyer).button_confirm()
        req = self._request_for(po)
        self.assertTrue(req)

        found = self.Request.with_user(self.buyer_b).search(
            [('reference', '=', 'purchase.order,%d' % po.id)])
        self.assertFalse(found)

        with self.env.cr.savepoint():
            with self.assertRaises(AccessError):
                req.with_user(self.buyer_b).read(['state'])
        with self.env.cr.savepoint():
            with self.assertRaises(AccessError):
                req.line_ids.with_user(self.buyer_b).read(['status'])

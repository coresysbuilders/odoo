# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestPurchaseGate(TransactionCase):
    """Core enforcement proofs for the purchase approval bridge, run against a
    live Odoo runtime. Every exception assertion is wrapped in a savepoint so it
    reflects real RPC rollback (REVIEW #6); watched edits and confirms are driven
    by a non-approver purchase user via with_user() so no path falsely passes as
    superuser (round-2 medium)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Request = cls.env['coresys.approval.request']
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.po_model_id = cls.env['ir.model']._get_id('purchase.order')

        # Deterministic single-step confirm so final approval auto-runs straight
        # to 'purchase' rather than native 'to approve' double-validation (REVIEW #7).
        cls.env.company.po_double_validation = 'one_step'

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_approver = cls.env.ref('coresys_approvals.group_approval_approver')
        cls.purchase_user_group = cls.env.ref('purchase.group_purchase_user')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        # The buyer is a plain purchase user, deliberately NOT an approval group
        # member — the bridge requester ACL + own-scope record rules are what let
        # this persona create/submit/read its OWN request (round-2 HIGH).
        cls.buyer = Users.create({
            'name': 'CoreSys Buyer',
            'login': 'coresys_po_buyer',
            'group_ids': [(6, 0, [cls.group_internal.id,
                                  cls.purchase_user_group.id])],
        })
        cls.approver = Users.create({
            'name': 'CoreSys Approver',
            'login': 'coresys_po_approver',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })

        cls.supplier = cls.env['res.partner'].create({'name': 'Gate Supplier'})
        cls.product = cls.env['product.product'].create({
            'name': 'Gate Service',
            'type': 'service',
            'purchase_ok': True,
            'list_price': 100.0,
            'standard_price': 100.0,
        })

        # Any PO with a positive total matches. Exactly one active category so a
        # gated PO has a single unambiguous match (D-32).
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

        # Fail-closed fixture: same match, but its only level resolves to nobody.
        # Kept inactive so it never collides with cls.category; the fail-closed
        # test flips the active flags.
        empty_group = cls.env['res.groups'].create({'name': 'Gate Empty Group'})
        cls.category_no_approver = cls.Category.create({
            'name': 'PO Approval (no approver)',
            'model_id': cls.po_model_id,
            'domain': "[('amount_total', '>', 0)]",
            'active': False,
        })
        cls.Level.create({
            'name': 'Nobody',
            'category_id': cls.category_no_approver.id,
            'approver_group_id': empty_group.id,
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

    def _gated_po(self, qty=1.0, price=100.0):
        # Built AS the buyer with a valid supplier + product line so amount_total
        # > 0 (matches the category) and confirm's side effects succeed on the
        # sanctioned path (Pitfall 5). Returned in draft.
        return self.env['purchase.order'].with_user(self.buyer).create({
            'partner_id': self.supplier.id,
            'order_line': [(0, 0, self._line_vals(qty, price))],
        })

    def _request_for(self, po):
        return self.Request.sudo().search(
            [('reference', '=', 'purchase.order,%d' % po.id)])

    # ---- tests -------------------------------------------------------------

    def test_non_approver_cannot_confirm_via_write(self):
        # SC-4 credibility anchor: a raw RPC state write is rejected at write().
        po = self._gated_po()
        with self.env.cr.savepoint():
            with self.assertRaises(UserError):
                po.with_user(self.buyer).write({'state': 'purchase'})
        po.invalidate_recordset(['state'])
        self.assertNotEqual(po.state, 'purchase')

    def test_forged_context_cannot_confirm(self):
        # REVIEW #1: a client-supplied coresys_approval_bypass_gate=True is a plain
        # boolean, NOT the in-memory token, so it must NOT authorize confirmation.
        po = self._gated_po()
        forged = po.with_user(self.buyer).with_context(
            coresys_approval_bypass_gate=True)
        with self.env.cr.savepoint():
            with self.assertRaises(UserError):
                forged.write({'state': 'purchase'})
        po.invalidate_recordset(['state'])
        self.assertNotEqual(po.state, 'purchase')

        # The forged flag is equally inert on the friendly button path: it still
        # blocks + submits rather than confirming.
        res = forged.button_confirm()
        po.invalidate_recordset(['state'])
        self.assertEqual(po.state, 'draft')
        self.assertTrue(self._request_for(po))
        self.assertIsInstance(res, dict)

    def test_non_approver_buyer_can_autosubmit(self):
        # Round-2 HIGH requester ACL: the plain purchase buyer can auto-submit via
        # the button with NO AccessError, can read THEIR OWN request, and still
        # cannot approve it.
        po = self._gated_po()
        po.with_user(self.buyer).button_confirm()
        req = self._request_for(po)
        self.assertTrue(req)
        self.assertEqual(req.state, 'to_approve')

        # Own-request read via the bridge ACL + create_uid record rule.
        po.with_user(self.buyer).read(['name'])
        req.with_user(self.buyer).read(['state'])

        with self.env.cr.savepoint():
            with self.assertRaises(AccessError):
                req.with_user(self.buyer).action_approve()

    def test_auto_created_request_persists_after_ui_submit(self):
        # D-30 / REVIEW #2: clicking Confirm does NOT raise, returns a notification
        # action, and the auto-created request PERSISTS with the PO still draft.
        po = self._gated_po()
        res = po.with_user(self.buyer).button_confirm()
        req = self._request_for(po)
        self.assertTrue(req)
        self.assertEqual(req.state, 'to_approve')
        po.invalidate_recordset(['state'])
        self.assertEqual(po.state, 'draft')
        self.assertIsInstance(res, dict)

    def test_gate_fail_closed_no_eligible_approver(self):
        # D-31: a zero-eligible-approver auto-submit raises ValidationError inside
        # a savepoint and leaves NO orphan request; the PO stays draft.
        self.category.active = False
        self.category_no_approver.active = True
        po = self._gated_po()
        with self.env.cr.savepoint():
            with self.assertRaises(ValidationError):
                po.with_user(self.buyer).button_confirm()
        po.invalidate_recordset(['state'])
        self.assertEqual(po.state, 'draft')
        self.assertFalse(self._request_for(po))

    def test_auto_confirm_on_final_approval(self):
        # D-33 / REVIEW #7: final approval auto-confirms the PO to 'purchase' as
        # the requester under single-step validation.
        po = self._gated_po()
        po.with_user(self.buyer).button_confirm()
        req = self._request_for(po)
        req.with_user(self.approver).action_approve()
        self.assertEqual(req.state, 'approved')
        po.invalidate_recordset(['state'])
        self.assertEqual(po.state, 'purchase')

    def test_auto_run_access_error_keeps_approval(self):
        # D-36 / round-2 cache: strip the buyer's purchase rights and clear the
        # ORM/group cache BEFORE approving so the auto-run truly lacks confirm
        # rights (not a stale-rights false pass). The approval is kept, the PO is
        # not confirmed, and the failure is recorded in the request chatter.
        po = self._gated_po()
        po.with_user(self.buyer).button_confirm()
        req = self._request_for(po)

        self.buyer.write({'group_ids': [(3, self.purchase_user_group.id)]})
        self.env.invalidate_all()
        self.env.registry.clear_cache()

        req.with_user(self.approver).action_approve()
        self.assertEqual(req.state, 'approved')
        # po is bound to the buyer's env, and the buyer just lost purchase rights
        # (line above), so read the true stored state via sudo — the assertion is
        # about the PO's real state, not the buyer's read access to it.
        po.invalidate_recordset(['state'])
        self.assertNotEqual(po.sudo().state, 'purchase')

        bodies = ' '.join(req.message_ids.mapped('body'))
        self.assertIn('automatic action failed', bodies)

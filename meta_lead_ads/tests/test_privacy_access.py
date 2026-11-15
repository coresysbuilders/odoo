# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Meta answers are readable exactly by the people who can read the lead.

Every way into meta.lead.answer is tried as the real user: search, count,
grouping, export and read by id. A Meta group on its own gives nothing.
"""
from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

_OWN_DOCS = 'base.group_user,sales_team.group_sale_salesman'

# crm's own crm.lead rules, as the answer rules in meta_security.xml copy
# them: xml id -> (groups, domain).
_STOCK_LEAD_RULES = {
    'crm.crm_lead_company_rule': (
        set(), "[('company_id', 'in', company_ids + [False])]"),
    'crm.crm_rule_personal_lead': (
        {'sales_team.group_sale_salesman'},
        "['|',('user_id','=',user.id),('user_id','=',False)]"),
    'crm.crm_rule_all_lead': (
        {'sales_team.group_sale_salesman_all_leads'}, "[(1,'=',1)]"),
}


def _squash(domain):
    return ''.join(domain.split())


@tagged('post_install', '-at_install')
class TestAnswerAccess(MultiCompanyFixtureMixin, IngestFixtureMixin,
                       TransactionCase):

    def setUp(self):
        super().setUp()
        # Allowed to export, so the export test meets the record rules and
        # not Odoo's export permission.
        self.own_a = self._user(
            'ans_own_a', _OWN_DOCS + ',base.group_allow_export')
        self.meta_own_a = self._user(
            'ans_meta_own_a', _OWN_DOCS + ',meta_lead_ads.group_meta_user')
        self.other_a = self._user('ans_other_a', _OWN_DOCS)
        # admin_a from the fixture is the Meta Admin without a sales role.
        self.assertFalse(self.admin_a.has_group('sales_team.group_sale_salesman'))

        self.lead_own = self._lead('ANS own', 'LGANS_OWN', self.co_a, self.own_a)
        self.lead_free = self._lead('ANS free', 'LGANS_FREE', self.co_a, False)
        self.lead_other = self._lead(
            'ANS other', 'LGANS_OTHER', self.co_a, self.other_a)
        self.lead_b = self._lead('ANS b', 'LGANS_B', self.co_b, False)
        self.leads = (self.lead_own | self.lead_free | self.lead_other
                      | self.lead_b)
        self.answers = self.leads.sudo().answer_ids
        self.assertEqual(len(self.answers), 8)
        # Start cold, the way a request does.
        self.env.flush_all()
        self.env.invalidate_all()

    # -- helpers --------------------------------------------------------- #
    def _user(self, login, groups):
        return new_test_user(
            self.env, login, groups=groups, company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])

    def _lead(self, name, leadgen_id, company, user):
        lead = self.env['crm.lead'].sudo().create({
            'name': name,
            'type': 'opportunity',
            'company_id': company.id,
            'user_id': user.id if user else False,
            'meta_leadgen_id': leadgen_id,
        })
        self.assertEqual(lead.user_id, user or self.env['res.users'])
        for i in range(2):
            self.env['meta.lead.answer'].sudo().create({
                'lead_id': lead.id,
                'question_key': 'q%d' % i,
                'label': 'q%d' % i,
                'value': '%s %d' % (name, i),
            })
        return lead

    def _answers(self, *leads):
        return self.env['meta.lead.answer'].sudo().search(
            [('lead_id', 'in', [lead.id for lead in leads])])

    def _visible(self, user):
        """Rows of this test the user finds with an unrestricted search."""
        found = self.env['meta.lead.answer'].with_user(user).search([])
        return found.sudo() & self.answers

    # -- own-documents salesperson ---------------------------------------- #
    def test_own_documents_search_and_count(self):
        Answer = self.env['meta.lead.answer'].with_user(self.own_a)
        visible = self._answers(self.lead_own, self.lead_free)
        self.assertEqual(self._visible(self.own_a), visible)
        found = Answer.search([])
        self.assertEqual(Answer.search_count([]), len(found))
        self.assertEqual(
            Answer.search_count([('id', 'in', self.answers.ids)]), 4)

    def test_own_documents_read_group(self):
        Answer = self.env['meta.lead.answer'].with_user(self.own_a)
        groups = Answer.read_group([], ['lead_id'], ['lead_id'])
        counts = {g['lead_id'][0]: g['lead_id_count'] for g in groups}
        mine = {lead_id: n for lead_id, n in counts.items()
                if lead_id in self.leads.ids}
        self.assertEqual(
            mine, {self.lead_own.id: 2, self.lead_free.id: 2})

    def test_own_documents_read_by_id_refused(self):
        Answer = self.env['meta.lead.answer'].with_user(self.own_a)
        for lead in (self.lead_other, self.lead_b):
            hidden = self._answers(lead)
            with self.assertRaises(AccessError):
                Answer.browse(hidden.ids).read(['value'])
        # One visible row next to a hidden one still refuses the read.
        mixed = self._answers(self.lead_own)[:1] | self._answers(self.lead_other)[:1]
        with self.assertRaises(AccessError):
            Answer.browse(mixed.ids).read(['value'])

    def test_own_documents_export(self):
        Answer = self.env['meta.lead.answer'].with_user(self.own_a)
        with self.assertRaises(AccessError):
            Answer.browse(self._answers(self.lead_other).ids).export_data(
                ['value'])
        rows = Answer.search([]).export_data(['value'])['datas']
        values = {row[0] for row in rows}
        ours = set(self.answers.mapped('value'))
        self.assertEqual(values & ours, {
            'ANS own 0', 'ANS own 1', 'ANS free 0', 'ANS free 1'})

    def test_own_documents_can_not_change_answers(self):
        Answer = self.env['meta.lead.answer'].with_user(self.own_a)
        mine = Answer.browse(self._answers(self.lead_own).ids)
        self.assertEqual(len(mine.read(['value'])), 2)
        with self.assertRaises(AccessError):
            mine[:1].write({'value': 'x'})
        with self.assertRaises(AccessError):
            Answer.create({'lead_id': self.lead_own.id,
                           'question_key': 'q9', 'value': 'x'})
        with self.assertRaises(AccessError):
            mine[:1].unlink()
        self.assertEqual(
            sorted(self._answers(self.lead_own).mapped('value')),
            ['ANS own 0', 'ANS own 1'])

    def test_own_documents_reads_answers_through_the_lead(self):
        lead = self.lead_own.with_user(self.own_a)
        self.assertEqual(
            sorted(lead.answer_ids.mapped('value')),
            ['ANS own 0', 'ANS own 1'])

    def test_own_documents_form_shows_answers_without_promote(self):
        views = self.env['crm.lead'].with_user(self.own_a).get_views(
            [(False, 'form')])
        arch = etree.fromstring(views['views']['form']['arch'])
        self.assertTrue(arch.xpath("//page[@name='meta_answers']"))
        self.assertTrue(arch.xpath("//field[@name='answer_ids']"))
        self.assertFalse(
            arch.xpath("//button[@name='action_promote_to_field']"))

    # -- other roles ------------------------------------------------------ #
    def test_all_leads_salesperson_sees_own_company_only(self):
        self.assertEqual(
            self._visible(self.sales_a),
            self._answers(self.lead_own, self.lead_free, self.lead_other))
        Answer = self.env['meta.lead.answer'].with_user(self.sales_a)
        with self.assertRaises(AccessError):
            Answer.browse(self._answers(self.lead_b).ids).read(['value'])

    def test_meta_admin_without_sales_role_sees_nothing(self):
        Answer = self.env['meta.lead.answer'].with_user(self.admin_a)
        self.assertEqual(Answer.search_count([]), 0)
        with self.assertRaises(AccessError):
            Answer.browse(self._answers(self.lead_free).ids).read(['value'])

    def test_meta_user_follows_own_documents_rule(self):
        # Being a Meta User no longer opens other salespeople's answers.
        self.assertEqual(
            self._visible(self.meta_own_a), self._answers(self.lead_free))
        Answer = self.env['meta.lead.answer'].with_user(self.meta_own_a)
        with self.assertRaises(AccessError):
            Answer.browse(self._answers(self.lead_own).ids).read(['value'])

    # -- the crm rules the answer rules copy ------------------------------ #
    def test_lead_rules_are_the_mirrored_stock_set(self):
        msg = ("crm's own crm.lead rules changed; the meta.lead.answer rules "
               "in security/meta_security.xml copy them and must be updated "
               "together")
        rules = self.env['ir.rule'].sudo().search(
            [('model_id.model', '=', 'crm.lead')])
        xmlids = rules.get_external_id()
        crm_rules = {xmlids[rule.id]: rule for rule in rules
                     if xmlids[rule.id].startswith('crm.')}
        self.assertEqual(set(crm_rules), set(_STOCK_LEAD_RULES), msg)
        for xmlid, (groups, domain) in _STOCK_LEAD_RULES.items():
            rule = crm_rules[xmlid]
            self.assertEqual(
                _squash(rule.domain_force), _squash(domain),
                '%s: %s' % (xmlid, msg))
            rule_groups = set(rule.groups.get_external_id().values())
            self.assertEqual(rule_groups, groups, '%s: %s' % (xmlid, msg))

# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""What the lead erase removes outside the lead's own fields: the Meta
answer blocks in the description, the module's inbox alerts and the copies
of the old title kept on messages and activities."""
from markupsafe import Markup, escape

from odoo.tests.common import TransactionCase, tagged
from odoo.tools import html2plaintext

from odoo import Command
from odoo.addons.meta_lead_ads.models.const import (
    LEAD_NAME_PARAM, NOTIFY_ENABLED_PARAM, NOTIFY_TARGET_PARAM,
    NOTIFY_USER_PARAM)
from odoo.addons.meta_lead_ads.models.meta_lead_ingest import (
    QA_CLASS, QA_HEADING)

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin
from .test_privacy_erase import _ERASED_NAME, EraseFixtureMixin

_LEFT = ("An older Meta answer block in the description couldn't be "
         "located safely and was left as is.")
_KEPT = ("Lines in the Meta answer block of the description that aren't "
         "this lead's Meta answers were kept, such as notes typed into it.")

# Shapes the Odoo 19 description editor saved in a real session. Pressing
# Enter inside the Meta block copies its class onto the new line, Shift+Enter
# adds a <br> line to the block itself, and the first element gets the
# editor's version attributes (history list shortened here).
_EDITOR_ATTRS = (' data-oe-version="2.0" data-last-history-steps='
                 '",3472283704548866,2879748251206348"')
_TYPED_ABOVE = 'Called the client back on Monday.'
_TYPED_BELOW = 'Sent the price list after the call.'
_TYPED_SHIFT = 'Wants a call after 5pm.'
_LINK = '<a href="https://example.com/evil">click</a>'


@tagged('post_install', '-at_install')
class TestEraseBlocks(EraseFixtureMixin, MultiCompanyFixtureMixin,
                      IngestFixtureMixin, TransactionCase):

    def setUp(self):
        super().setUp()
        self._give_sales(self.admin_a)

    def _note_plain(self, lead):
        notes = self._notes(lead)
        self.assertEqual(len(notes), 1)
        return html2plaintext(notes.body)

    def _with_description(self, leadgen_id, n, html):
        lead = self._ingest(leadgen_id, self._contact(n))
        lead.sudo().write({'description': html})
        return lead

    def _alerted_lead(self, leadgen_id, n):
        # The new-lead alert goes to sales_a, through the real ingest path.
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(NOTIFY_ENABLED_PARAM, '1')
        ICP.set_param(NOTIFY_TARGET_PARAM, 'user')
        ICP.set_param(NOTIFY_USER_PARAM, str(self.sales_a.id))
        lead = self._ingest(leadgen_id, self._contact(n))
        alert = self._messages(lead).filtered(
            lambda m: '-openerp-meta-lead-alert@' in (m.message_id or ''))
        self.assertEqual(len(alert), 1)
        self.assertTrue(alert.notification_ids)
        return lead, alert

    def _messages(self, lead):
        return self.env['mail.message'].sudo().search([
            ('model', '=', 'crm.lead'), ('res_id', '=', lead.id)])

    def _hand_written(self, lead, body='Spoke to them, call again Monday'):
        return lead.with_user(self.sales_a).message_post(
            body=body, message_type='comment', subtype_xmlid='mail.mt_note')

    def _attachment(self, name, res_model, res_id):
        return self.env['ir.attachment'].sudo().create({
            'name': name, 'raw': b'x', 'res_model': res_model,
            'res_id': res_id})

    # -- description blocks ------------------------------------------------ #
    def test_marked_block_after_salesperson_text(self):
        seed = self.Lead.create({
            'name': 'Seed', 'type': 'lead', 'email_from': 'person41@example.com',
            'description': '<p>Called back</p>', 'company_id': self.co_a.id})
        lead = self._ingest('LGBLK_ONE', self._contact(41, [
            {'name': 'what_is_your_budget', 'values': ['$5k secret']}]))
        self.assertEqual(lead, seed)
        self.assertTrue(lead.meta_leadgen_id)
        self.assertIn('o_meta_lead_qa', str(lead.description))

        result = self._erase(lead)
        description = str(lead.description)
        self.assertIn('<p>Called back</p>', description)
        self.assertNotIn('o_meta_lead_qa', description)
        self.assertNotIn('secret', description)
        self.assertNotIn(QA_HEADING, description)
        self.assertNotIn(_LEFT, self._note_plain(lead))
        self.assertNotIn('See the note', result['params']['message'])

    def test_two_marked_blocks_both_go(self):
        seed = self.Lead.create({
            'name': 'Seed', 'type': 'lead', 'email_from': 'person42@example.com',
            'description': '<p>Called back</p>', 'company_id': self.co_a.id})
        lead = self._ingest('LGBLK_TWO', self._contact(42, [
            {'name': 'what_is_your_budget', 'values': ['first secret']}]))
        self.assertEqual(lead, seed)
        # A later submission matched to the same lead appends its own block.
        block = (Markup('<div class="%s">') % QA_CLASS
                 + Markup('<br>').join([
                     escape(QA_HEADING),
                     escape('what_is_your_budget: second secret')])
                 + Markup('</div>'))
        self.Ingest._apply_answers(seed.sudo(), [], block)
        self.assertEqual(str(seed.description).count('o_meta_lead_qa'), 2)

        self._erase(seed)
        description = str(seed.description)
        self.assertIn('<p>Called back</p>', description)
        self.assertNotIn('o_meta_lead_qa', description)
        self.assertNotIn('secret', description)

    def test_block_alone_leaves_the_description_empty(self):
        lead = self._ingest('LGBLK_ALONE', self._contact(43, [
            {'name': 'what_is_your_budget', 'values': ['secret']}]))
        self.assertIn('o_meta_lead_qa', str(lead.description))
        self._erase(lead)
        self.assertFalse(lead.description)

    def test_legacy_paragraph_block_removed(self):
        lead = self._with_description(
            'LGBLK_LEGA', 44,
            '<p>keep me</p><p>%s<br>q: secret</p>' % QA_HEADING)
        self.assertIn('q: secret', str(lead.description))

        self._erase(lead)
        description = str(lead.description)
        self.assertIn('<p>keep me</p>', description)
        self.assertNotIn('secret', description)
        self.assertNotIn(QA_HEADING, description)
        self.assertNotIn(_LEFT, self._note_plain(lead))

    def test_legacy_tail_block_cut(self):
        lead = self._with_description(
            'LGBLK_LEGB', 45, '<p>note</p>\n\n%s\nq: secret' % QA_HEADING)
        self.assertIn('</p>', str(lead.description))
        self.assertIn('q: secret', str(lead.description))

        self._erase(lead)
        description = str(lead.description)
        self.assertIn('<p>note</p>', description)
        self.assertNotIn('secret', description)
        self.assertNotIn(QA_HEADING, description)
        self.assertNotIn(_LEFT, self._note_plain(lead))

    def test_legacy_block_that_cannot_be_cut_is_left_and_noted(self):
        lead = self._with_description(
            'LGBLK_LEFT', 46,
            '<p>note</p>\n\n%s\nq: secret<p>later</p>' % QA_HEADING)
        self.assertIn('q: secret', str(lead.description))
        self.assertIn('<p>later</p>', str(lead.description))

        result = self._erase(lead)
        description = str(lead.description)
        self.assertIn('q: secret', description)
        self.assertIn('<p>note</p>', description)
        self.assertIn('<p>later</p>', description)
        self.assertIn(_LEFT, self._note_plain(lead))
        self.assertIn('See the note on 1 of them', result['params']['message'])

    # -- lines typed into a marked block ------------------------------------ #
    def _editor_lead(self, leadgen_id, n):
        """A lead whose description is only its Meta block, as ingested:
        a link answer kept as text and a budget answer."""
        lead = self._ingest(leadgen_id, self._contact(n, [
            {'name': 'what_is_your_website', 'values': [_LINK]},
            {'name': 'what_is_your_budget', 'values': ['$5k secret']}]))
        block = str(lead.description)
        self.assertTrue(block.startswith('<div class="%s">' % QA_CLASS))
        self.assertIn('&lt;a href=', block)
        return lead, block

    def _typed(self, text, attrs=''):
        return '<div class="%s"%s>%s</div>' % (QA_CLASS, attrs, text)

    def _assert_answers_gone(self, description):
        self.assertNotIn(QA_CLASS, description)
        self.assertNotIn(QA_HEADING, description)
        self.assertNotIn('secret', description)
        self.assertNotIn('example.com/evil', description)
        self.assertNotIn('what_is_your', description)

    def test_line_typed_above_the_block_is_kept(self):
        lead, block = self._editor_lead('LGBLK_ABOVE', 61)
        lead.sudo().write({'description': self._typed(
            _TYPED_ABOVE, _EDITOR_ATTRS) + block})
        self.assertEqual(str(lead.description).count(QA_CLASS), 2)

        result = self._erase(lead)
        description = str(lead.description)
        self.assertIn(_TYPED_ABOVE, description)
        self._assert_answers_gone(description)
        self.assertIn(_KEPT, self._note_plain(lead))
        self.assertIn('See the note on 1 of them', result['params']['message'])

    def test_line_typed_below_the_block_is_kept(self):
        lead, block = self._editor_lead('LGBLK_BELOW', 62)
        html = (block.replace('<div class="%s">' % QA_CLASS,
                              '<div class="%s"%s>' % (QA_CLASS, _EDITOR_ATTRS),
                              1)
                + self._typed(_TYPED_BELOW))
        lead.sudo().write({'description': html})
        self.assertIn('data-oe-version', str(lead.description))

        self._erase(lead)
        description = str(lead.description)
        self.assertIn(_TYPED_BELOW, description)
        self._assert_answers_gone(description)
        self.assertIn(_KEPT, self._note_plain(lead))

    def test_line_typed_inside_the_block_keeps_only_that_line(self):
        # Typed above with Enter, then below with Shift+Enter, which puts
        # the second line in the block itself after a <br>.
        lead, block = self._editor_lead('LGBLK_MIXED', 63)
        mixed = block[:-len('</div>')] + '<br>%s</div>' % _TYPED_SHIFT
        lead.sudo().write({'description': self._typed(
            _TYPED_ABOVE, _EDITOR_ATTRS) + mixed})

        self._erase(lead)
        description = str(lead.description)
        self.assertIn(_TYPED_ABOVE, description)
        self.assertIn(_TYPED_SHIFT, description)
        self._assert_answers_gone(description)
        # Each kept line stays in its own element, without the class.
        self.assertEqual(description, '<div%s>%s</div><div>%s</div>' % (
            _EDITOR_ATTRS, _TYPED_ABOVE, _TYPED_SHIFT))
        self.assertIn(_KEPT, self._note_plain(lead))

    def test_line_typed_between_two_blocks_is_kept(self):
        seed = self.Lead.create({
            'name': 'Seed', 'type': 'lead', 'email_from': 'person64@example.com',
            'description': '<p>Called back</p>', 'company_id': self.co_a.id})
        lead = self._ingest('LGBLK_BETWEEN', self._contact(64, [
            {'name': 'what_is_your_budget', 'values': ['first secret']}]))
        self.assertEqual(lead, seed)
        # A later submission matched to the same lead, with its own answer.
        block = (Markup('<div class="%s">') % QA_CLASS
                 + Markup('<br>').join([
                     escape(QA_HEADING),
                     escape('what_is_your_timeline: second secret')])
                 + Markup('</div>'))
        self.Ingest._apply_answers(seed.sudo(), [{
            'question_key': 'what_is_your_timeline',
            'label': 'what_is_your_timeline', 'value': 'second secret'}],
            block)
        seam = '</div><div class="%s">' % QA_CLASS
        html = str(seed.description)
        self.assertEqual(html.count(seam), 1)
        seed.sudo().write({'description': html.replace(
            seam, '</div>%s<div class="%s">' % (
                self._typed('Typed between the two.'), QA_CLASS))})
        self.assertEqual(str(seed.description).count(QA_CLASS), 3)

        self._erase(seed)
        description = str(seed.description)
        self.assertIn('<p>Called back</p>', description)
        self.assertIn('Typed between the two.', description)
        self._assert_answers_gone(description)
        self.assertIn(_KEPT, self._note_plain(seed))

    def test_typed_markup_stays_text_when_answer_lines_are_cut(self):
        # The answer holding a link is escaped in the block and must still be
        # recognised; the typed line's own escaped tags must stay escaped
        # when the rest of its element is rebuilt.
        lead, block = self._editor_lead('LGBLK_ESC', 65)
        mixed = (block[:-len('</div>')]
                 + '<br>Asked why &lt;script&gt; showed up.</div>')
        lead.sudo().write({'description': mixed})

        self._erase(lead)
        description = str(lead.description)
        self.assertIn('Asked why &lt;script&gt; showed up.', description)
        self.assertNotIn('<script', description)
        self._assert_answers_gone(description)

    def test_block_without_typed_lines_leaves_no_kept_note(self):
        lead, block = self._editor_lead('LGBLK_CLEAN', 66)
        # The editor's attributes on the block alone change nothing.
        lead.sudo().write({'description': block.replace(
            '<div class="%s">' % QA_CLASS,
            '<div class="%s"%s>' % (QA_CLASS, _EDITOR_ATTRS), 1)})

        result = self._erase(lead)
        self.assertFalse(lead.description)
        self.assertNotIn(_KEPT, self._note_plain(lead))
        self.assertNotIn('See the note', result['params']['message'])

    # -- inbox alerts and their attachments -------------------------------- #
    def test_module_alert_deleted_hand_written_note_kept(self):
        lead, alert = self._alerted_lead('LGBLK_ALERT', 51)
        notifications = alert.notification_ids
        note = self._hand_written(lead)

        self._erase(lead)
        self.assertFalse(alert.exists())
        self.assertFalse(notifications.exists())
        self.assertTrue(note.exists())
        self.assertIn('call again Monday', str(note.body))

    def test_older_alert_without_tag_deleted(self):
        lead = self._ingest('LGBLK_OLD', self._contact(52))
        # As posted before alerts were tagged.
        older = self.env['mail.message'].sudo().create({
            'model': 'crm.lead', 'res_id': lead.id,
            'message_type': 'user_notification',
            'author_id': self.env.ref('base.partner_root').id,
            'subject': 'New Meta lead',
            'body': 'A new lead arrived from Meta Lead Ads: Person 52',
            'partner_ids': [Command.set(self.sales_a.partner_id.ids)],
        })
        note = self._hand_written(lead)

        self._erase(lead)
        self.assertFalse(older.exists())
        self.assertTrue(note.exists())

    def test_attachment_only_an_alert_holds_goes(self):
        lead, alert = self._alerted_lead('LGBLK_ATT', 53)
        note = self._hand_written(lead)
        own = self._attachment('alert.txt', 'crm.lead', lead.id)
        # Linked to the alert itself, which Odoo's own message delete
        # would remove with the alert.
        shared = self._attachment('shared.txt', 'mail.message', alert.id)
        alert.sudo().write({'attachment_ids': [
            Command.link(own.id), Command.link(shared.id)]})
        note.sudo().write({'attachment_ids': [Command.link(shared.id)]})

        self._erase(lead)
        self.assertFalse(alert.exists())
        self.assertFalse(own.exists())
        self.assertTrue(shared.exists())
        self.assertEqual(note.attachment_ids, shared)
        # Filed under the lead now, so it doesn't point at a deleted message.
        self.assertEqual((shared.res_model, shared.res_id),
                         ('crm.lead', lead.id))

    # -- title copies ------------------------------------------------------- #
    def test_stored_title_copies_replaced(self):
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, '{contact_name}')
        lead = self._ingest('LGBLK_TITLE', self._contact(54))
        self.assertEqual(lead.name, 'Person 54')
        self.assertTrue(lead.meta_name_is_generated)
        activity = lead.activity_schedule(
            'mail.mail_activity_data_todo', summary='Call back',
            user_id=self.sales_a.id)
        note = self._hand_written(lead, body='Asked about Person 54 again')
        Message = self.env['mail.message']
        if self.env['mail.activity']._fields['res_name'].store:
            self.assertEqual(activity.res_name, 'Person 54')

        self._erase(lead)
        self.assertEqual(lead.name, _ERASED_NAME)
        if self.env['mail.activity']._fields['res_name'].store:
            self.assertEqual(activity.res_name, _ERASED_NAME)
        if Message._fields['record_name'].store:
            for message in self._messages(lead):
                self.assertEqual(message.record_name, _ERASED_NAME)
        # Message bodies are never edited.
        self.assertIn('Asked about Person 54 again', str(note.body))

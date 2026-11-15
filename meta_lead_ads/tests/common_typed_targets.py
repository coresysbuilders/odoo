# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Custom crm.lead fields of every type a mapping can convert to.

Stock crm.lead no longer offers a date, number, selection or boolean field
to map onto, so tests that check the conversions map onto these instead.
"""
from odoo.fields import Command


class TypedTargetsMixin:
    """Adds manual x_meta_typed_* fields to crm.lead for the whole class.

    Put it first in the bases so the fields exist before any setUp maps
    onto them.
    """

    # name: (ttype, label, extra values)
    _TYPED_FIELDS = {
        'x_meta_typed_date': ('date', 'Typed Date', {}),
        'x_meta_typed_when': ('datetime', 'Typed When', {}),
        'x_meta_typed_amount': ('float', 'Typed Amount', {}),
        'x_meta_typed_count': ('integer', 'Typed Count', {}),
        'x_meta_typed_money': ('monetary', 'Typed Money',
                               {'currency_field': 'company_currency'}),
        'x_meta_typed_level': ('selection', 'Typed Level', {'selection_ids': [
            Command.create({'value': 'low', 'name': 'Low', 'sequence': 1}),
            Command.create({'value': 'high', 'name': 'High', 'sequence': 2}),
        ]}),
        'x_meta_typed_flag': ('boolean', 'Typed Flag', {}),
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Created once per class: the class cleanup resets the registry,
        # while a field made inside one test would stay in the registry
        # after its column is rolled back.
        Fields = cls.env['ir.model.fields'].sudo()
        model_id = cls.env['ir.model']._get('crm.lead').id
        cls.TYPED = {}
        for name, (ttype, label, extra) in cls._TYPED_FIELDS.items():
            cls.TYPED[name] = Fields.create(dict(
                extra, name=name, model_id=model_id, model='crm.lead',
                ttype=ttype, state='manual', store=True,
                field_description=label))


class LinkTargetsMixin:
    """Adds manual crm.lead fields that link to a contact or a user.

    A form answer may never fill these, whatever they are called. Created
    once per class for the same reason as the typed fields above. Keep it
    off classes with a failing import: load() then resets the registry,
    which drops these fields for the rest of the class.
    """

    # name: (ttype, label, comodel)
    _LINK_FIELDS = {
        'x_referrer_id': ('many2one', 'Referrer', 'res.partner'),
        'x_watcher_ids': ('many2many', 'Watchers', 'res.users'),
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Fields = cls.env['ir.model.fields'].sudo()
        model_id = cls.env['ir.model']._get('crm.lead').id
        cls.LINKS = {}
        for name, (ttype, label, comodel) in cls._LINK_FIELDS.items():
            cls.LINKS[name] = Fields.create({
                'name': name, 'model_id': model_id, 'model': 'crm.lead',
                'ttype': ttype, 'relation': comodel, 'state': 'manual',
                'store': True, 'field_description': label})

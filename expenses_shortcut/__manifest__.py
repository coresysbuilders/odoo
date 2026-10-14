# Expenses Shortcut for Odoo — © 2026 CoreSys Builders
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)

{
    'name': 'Expenses Shortcut',
    'version': '18.0.1.0.0',
    'category': 'Human Resources/Expenses',
    'summary': 'Submit, approve, post and pay each expense directly, with no '
               'expense reports. Brings the Odoo 19 expense flow to Odoo 18: '
               'one-click submit, per-expense approval and refusal, posting and '
               'payment from the expense, and a weekly approver digest.',
    'author': 'CoreSys Builders',
    'maintainer': 'CoreSys Builders',
    'support': 'info@coresysbuilders.com',
    'license': 'LGPL-3',
    'depends': ['hr_expense'],
    'data': [
        'security/ir.model.access.csv',
        'data/mail_templates.xml',
        'data/mail_activity_type_data.xml',
        'data/hr_expense_cron.xml',
        'data/hr_expense_concealment.xml',
        'wizard/expenses_shortcut_refuse_reason_views.xml',
        'wizard/expenses_shortcut_post_wizard_views.xml',
        'views/hr_expense_actions.xml',
        'views/hr_expense_views.xml',
        'views/hr_expense_concealment_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'expenses_shortcut/static/src/views/expense_buttons.xml',
        ],
    },
    'images': [
        'static/description/cover.png',
        'static/description/screenshots/01-expense-list.jpg',
        'static/description/screenshots/02-submit.jpg',
        'static/description/screenshots/03-approve.jpg',
        'static/description/screenshots/04-refuse.jpg',
        'static/description/screenshots/05-post.jpg',
        'static/description/screenshots/06-paid.jpg',
        'static/description/screenshots/07-arabic-rtl.jpg',
    ],
    'pre_init_hook': 'pre_init_hook',
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
}

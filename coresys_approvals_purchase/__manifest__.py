# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
{
    'name': 'Approvals for Purchase',
    'summary': 'Require approval before a purchase order can be confirmed',
    'version': '19.0.1.0.0',
    'license': 'OPL-1',
    'author': 'CoreSys Builders',
    'company': 'CoreSys Builders',
    'website': 'https://coresysbuilders.com',
    'category': 'Inventory/Purchase',
    'depends': ['coresys_approvals', 'purchase'],
    'data': [
        'security/ir.model.access.csv',
        'security/purchase_approval_security.xml',
        'views/purchase_order_views.xml',
    ],
    'demo': ['demo/purchase_approval_demo.xml'],
    'images': ['static/description/banner.png'],
    'installable': True,
}

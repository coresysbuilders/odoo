# Part of Expenses Shortcut by CoreSys Builders. License LGPL-3.0 or later.
from . import models
from . import wizard
from . import hooks

# Odoo looks up the manifest's hooks on the addon package itself.
from .hooks import pre_init_hook
from .hooks import post_init_hook
from .hooks import uninstall_hook

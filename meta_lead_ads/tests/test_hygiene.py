# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Internal planning references stay out of the shipped module.

Tests and migrations ship with the addon, so a work-item number in a field
name, a login or a comment ends up in every customer database. The patterns
are put together from pieces at run time, so this file never matches its
own scan.
"""
import os
import re

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase, tagged

TEXT_EXTENSIONS = ('.py', '.xml', '.js', '.csv', '.css', '.scss', '.html',
                   '.rst', '.md', '.po', '.pot', '.txt')

TWO_DIGITS = '[0-9]' + '{2}'
# The range the work-item numbers used: 10 to 20.
WORK_ITEM = '(?:1' + '[0-9]|2' + '0)'
PLAN_FILE_SUFFIXES = ('PLAN', 'SUMMARY', 'CONTEXT', 'RESEARCH', 'REVIEW',
                      'VALIDATION', 'PATTERNS')
REQUIREMENT_PREFIXES = ('CONN', 'INGEST', 'LEAD', 'ATTR', 'DEDUP', 'SYNC',
                        'SEC', 'COMP', 'PRIV', 'REV', 'IN', 'WR', 'CR',
                        'ONB', 'OPS', 'RECOV', 'MAP', 'ROUTE', 'MULTIAPP',
                        'LIVE')


def _phase_pattern():
    # Short ids such as a "p" or "t" prefix glued to the number, or the
    # word for a planning stage followed by it. Case does not matter.
    short = '[pt]' + WORK_ITEM
    stage = 'pha' + 'se' + '[ _-]?' + WORK_ITEM
    return re.compile('(?:^|[^a-z0-9])(?:' + short + '|' + stage
                      + ')(?:[^0-9]|$)', re.IGNORECASE)


def _marker_pattern():
    # Decision, threat, planning file and requirement ids. Upper case only,
    # so lower-case strings of the same shape are left alone.
    dash = '-'
    decision = r'\b' + 'D' + dash + TWO_DIGITS + r'\b'
    threat = r'\b' + 'T' + dash + TWO_DIGITS + dash + TWO_DIGITS + r'\b'
    plan_file = (r'\b' + TWO_DIGITS + r'(?:\.[0-9])?' + dash + TWO_DIGITS
                 + dash + '(?:' + '|'.join(PLAN_FILE_SUFFIXES) + r')\b')
    requirement = (r'\b(?:' + '|'.join(REQUIREMENT_PREFIXES) + ')' + dash
                   + TWO_DIGITS + r'\b')
    return re.compile('|'.join((decision, threat, plan_file, requirement)))


PATTERNS = (_phase_pattern(), _marker_pattern())


def _is_hit(line):
    return any(pattern.search(line) for pattern in PATTERNS)


@tagged('post_install', '-at_install')
class TestHygiene(TransactionCase):

    def test_no_planning_ids_in_shipped_files(self):
        root = get_module_path('meta_lead_ads')
        self.assertTrue(root and os.path.isdir(root))
        hits = []
        scanned = 0
        for folder, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d != '__pycache__')
            for name in sorted(files):
                if not name.lower().endswith(TEXT_EXTENSIONS):
                    continue
                path = os.path.join(folder, name)
                scanned += 1
                with open(path, encoding='utf-8', errors='replace') as fh:
                    for line_no, line in enumerate(fh, 1):
                        if _is_hit(line):
                            rel = os.path.relpath(path, root).replace(
                                os.sep, '/')
                            hits.append('%s:%d' % (rel, line_no))
        # Guards against a walk that silently finds nothing.
        self.assertGreater(scanned, 50)
        self.assertFalse(
            hits, "planning ids found in shipped files (first 20):\n%s"
            % '\n'.join(hits[:20]))

    def test_scanner_catches_a_planning_id(self):
        caught = (
            "login='" + 'p' + '1' + '7' + "_user'",
            '# per ' + 'D' + '-' + '05',
            '# see ' + 'T' + '-18-' + '06',
            'from ' + '18' + '-07-' + 'PLAN' + '.md',
            'covers ' + 'PRIV' + '-' + '04',
            'fixes ' + 'IN' + '-' + '05',
            'covers ' + 'O' + 'PS' + '-' + '03',
            'see ' + 'RECOV' + '-' + '04',
            'per ' + 'MAP' + '-' + '01',
            'per ' + 'ROUTE' + '-' + '02',
            'not ' + 'MULTI' + 'APP' + '-' + '01',
            'deferred to ' + 'LIVE' + '-' + '01',
            '# added in ' + 'pha' + 'se 2' + '0',
            "login='" + 'p' + '2' + '0' + "_admin'",
        )
        for line in caught:
            with self.subTest(line=line):
                self.assertTrue(_is_hit(line))
        missed = (
            '2026-12-25',
            '2026-12-25T10:00:00',
            '0123456789abcdef' * 4,
            'LGD-01',
            'd-01',
            'x_meta_typed_date',
            'padding:0 16px 6px',
            'top 20 campaigns',
            'p21_user',
        )
        for line in missed:
            with self.subTest(line=line):
                self.assertFalse(_is_hit(line))

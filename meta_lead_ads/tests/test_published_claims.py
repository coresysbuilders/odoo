# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""The store listing and the guides must not promise more than the module does.

The Apps store page, the setup guide that opens from Settings and the
manifest summary are read by buyers before and after install. Words that
promise an absolute outcome are kept out of them. A few uses are fine because
they are not promises: Meta's "Never" token expiration option, the callback
URL that is always the base URL plus a fixed path, and "Not always" as an
answer. The word lists are put together from pieces at run time so this file
doesn't trip other scans that look for them. The repository README and the
setup doc are scanned too when they sit next to the module; they are not
part of the module zip, so they are skipped when absent.
"""
import ast
import html
import os
import re

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase, tagged

HTML_FILES = ('static/description/index.html', 'static/doc/setup_guide.html')
MANIFEST = '__manifest__.py'
# Repository docs, relative to the folder that holds the module.
DOC_FILES = ('README.md', 'docs/connecting-to-meta.md')
DASHBOARD_XML = 'static/src/dashboard/dashboard.xml'

_SP = r'\s+'


def _banned_pattern():
    words = (
        'nev' + 'er',
        'noth' + 'ing' + _SP + '(?:is' + _SP + ')?' + 'lo' + 'st',
        'eve' + 'ry' + _SP + 'fail' + 'ure',
        'exa' + 'ctly' + r'[-\s]+' + 'on' + 'ce',
        'guar' + 'antee',
        'no' + _SP + 'mat' + 'ter',
        'loss' + 'less',
        '100' + '%',
        'one' + r'[-\s]+' + 'cli' + 'ck',
        r'\b' + 'alw' + 'ays' + r'\b',
    )
    return re.compile('|'.join(words), re.IGNORECASE)


def _allowed_patterns():
    return (
        # Meta's token expiration option, quoted as its UI label.
        re.compile('(?i:expiration)' + r'[^.]{0,40}?\b' + 'Nev' + r'er\b'),
        # The verify token stays put once generated.
        re.compile('verify token' + '.{0,80}?' + 'nev' + 'er changes',
                   re.IGNORECASE),
        # The callback URL is the base URL plus a fixed path.
        re.compile(r'\b' + 'alw' + 'ays' + r'\s+`?(?:<|&lt;)?web\.base\.url',
                   re.IGNORECASE),
        # "Not always" answers a question; it is a negation, not a promise.
        re.compile(r'\bnot\s+' + 'alw' + r'ays\b', re.IGNORECASE),
    )


BANNED = _banned_pattern()
ALLOWED = _allowed_patterns()

_COMMENT = re.compile(r'<!--.*?-->', re.DOTALL)
_STYLE_BLOCK = re.compile(r'<style\b[^>]*>.*?</style\s*>',
                          re.DOTALL | re.IGNORECASE)
_STYLE_ATTR = re.compile(r'''style\s*=\s*(?:"[^"]*"|'[^']*')''',
                         re.IGNORECASE)
# Real tags only, so a placeholder such as <web.base.url> stays in the text.
_TAG = re.compile(r'</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>')


def _blank(match):
    # Drop the text but keep its line breaks, so line numbers still match
    # the source file.
    return re.sub(r'[^\n]', '', match.group(0))


def _visible_text(text):
    """Text a reader sees: no comments, CSS, inline styles or tags."""
    for pattern in (_COMMENT, _STYLE_BLOCK, _STYLE_ATTR):
        text = pattern.sub(_blank, text)
    text = _TAG.sub(lambda m: _blank(m) if '\n' in m.group(0) else ' ', text)
    return html.unescape(text)


def _banned_hits(text):
    """Return (line, phrase) for every banned phrase not covered by the
    allowlist."""
    text = _visible_text(text)
    allowed = [m.span() for pattern in ALLOWED for m in pattern.finditer(text)]
    hits = []
    for match in BANNED.finditer(text):
        start, end = match.span()
        if any(a <= start and end <= b for a, b in allowed):
            continue
        line = text.count('\n', 0, start) + 1
        hits.append((line, ' '.join(match.group(0).split())))
    return hits


def _flat(text):
    """Visible text on one line, so wrapping doesn't hide a phrase."""
    return ' '.join(_visible_text(text).split())


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


@tagged('post_install', '-at_install')
class TestPublishedClaims(TransactionCase):

    def _root(self):
        root = get_module_path('meta_lead_ads')
        self.assertTrue(root and os.path.isdir(root))
        return os.path.normpath(root)

    def _html_sources(self):
        root = self._root()
        return [(rel, _read(os.path.join(root, rel))) for rel in HTML_FILES]

    def _doc_sources(self):
        # The docs live in the repository, not in the module zip or the
        # deployed addons folder, so only read the ones that are there.
        parent = os.path.dirname(self._root())
        found = []
        for rel in DOC_FILES:
            path = os.path.join(parent, rel)
            if os.path.isfile(path):
                found.append((rel, _read(path)))
        return found

    def _manifest(self):
        return ast.literal_eval(_read(os.path.join(self._root(), MANIFEST)))

    def _sources(self):
        sources = self._html_sources()
        manifest = self._manifest()
        self.assertTrue(manifest.get('summary'))
        sources.append((MANIFEST + ' summary', manifest['summary']))
        sources.extend(self._doc_sources())
        return sources

    def test_shipped_copy_has_no_absolute_claims(self):
        found = []
        for name, text in self._sources():
            self.assertTrue(text.strip(), name)
            for line, phrase in _banned_hits(text):
                found.append('%s:%d %r' % (name, line, phrase))
        self.assertFalse(
            found, "absolute claims in the shipped copy:\n%s"
            % '\n'.join(found))

    def test_banned_pattern_self_check(self):
        caught = (
            'designed so you ' + 'nev' + 'er lose a lead',
            'Noth' + 'ing is lo' + 'st.',
            'exa' + 'ctly-on' + 'ce creation',
            'delivery is ' + 'guar' + 'anteed',
            'a ' + 'one-cli' + 'ck retry',
            'loss' + 'less capture',
            '100' + '% of leads',
            'every lead arrives, ' + 'alw' + 'ays',
            '<p>exa' + 'ctly\n  on' + 'ce</p>',
        )
        for line in caught:
            with self.subTest(line=line):
                self.assertTrue(_banned_hits(line))
        missed = (
            '<img src="a.jpg" style="width:100' + '%"/>',
            'style="width:100' + '%"',
            'set expiration to Nev' + 'er',
            'Choose an expiration (<b>60 days</b> or <b>Nev' + 'er</b>)',
            'the verify token nev' + 'er changes',
            'alw' + 'ays <web.base.url>/meta/webhook',
            'alw' + 'ays <code>&lt;web.base.url&gt;/meta_lead_ads/webhook</code>',
            'alw' + 'ays `<web.base.url>/meta_lead_ads/webhook`',
            'Not alw' + 'ays. Retry runs the lead again.',
            '<style>table { width: 100' + '%; }</style>',
            '<!-- the ' + 'nev' + 'er-shown comment -->',
            'each Meta lead ID creates at most one CRM lead',
        )
        for line in missed:
            with self.subTest(line=line):
                self.assertFalse(_banned_hits(line))

    def test_uninstall_copy_names_dependent_fields(self):
        # Odoo removes a custom field computed from or related to a field
        # it deletes, and a custom one2many whose inverse is that field,
        # whoever owns them, so the copy can't say they are kept.
        html_sources = self._html_sources()
        self.assertEqual(len(html_sources), 2)
        old = 'other custom fields are ' + 'not touched'
        needed = ('computed from, related to, or the one2many inverse of '
                  'a promoted field',
                  'check that view afterwards')
        for name, text in html_sources + self._doc_sources():
            flat = _flat(text)
            with self.subTest(surface=name):
                for phrase in needed:
                    self.assertTrue(phrase in flat,
                                    '%s lacks %r' % (name, phrase))
                self.assertFalse(old in flat.lower(),
                                 '%s still has %r' % (name, old))

    def test_quiet_copy_names_rejected_calls(self):
        # A rejected signature or a turned-off subscription also leaves the
        # webhook quiet, so Quiet can't be explained as no leads only.
        old = 'only means none ' + 'arrived recently'
        root = self._root()
        guide = 'static/doc/setup_guide.html'
        surfaces = [(guide, _read(os.path.join(root, guide)))]
        surfaces += [(name, text) for name, text in self._doc_sources()
                     if name == 'docs/connecting-to-meta.md']
        surfaces.append(
            (DASHBOARD_XML, _read(os.path.join(root, DASHBOARD_XML))))
        new = 'rejected calls from Meta'
        footnote = 'turns red ' + 'only'
        for name, text in surfaces:
            flat = _flat(text)
            with self.subTest(surface=name):
                self.assertTrue(new in flat, '%s lacks %r' % (name, new))
                for gone in (old, footnote):
                    self.assertFalse(gone in flat,
                                     '%s still has %r' % (name, gone))

    def test_conversion_copy_counts_direct_opportunities_separately(self):
        # A lead created straight as an opportunity was never converted, so
        # the copy has to say those are counted apart from the rate.
        root = self._root()
        store = 'static/description/index.html'
        surfaces = [(store, _read(os.path.join(root, store)))]
        surfaces += [(name, text) for name, text in self._doc_sources()
                     if name == 'README.md']
        new = 'created directly as opportunities'
        old = 'lead→opportunity and won conversion'
        for name, text in surfaces:
            flat = _flat(text)
            with self.subTest(surface=name):
                self.assertTrue(new in flat, '%s lacks %r' % (name, new))
                self.assertFalse(old in flat,
                                 '%s still has %r' % (name, old))

    def test_archived_page_copy_says_leads_wait(self):
        # Leads of an archived Page are held, not dropped, and come back
        # once the Page is active again; the troubleshooting copy says so.
        root = self._root()
        guide = 'static/doc/setup_guide.html'
        surfaces = [(guide, _read(os.path.join(root, guide)))]
        surfaces += [(name, text) for name, text in self._doc_sources()
                     if name == 'docs/connecting-to-meta.md']
        new = 'wait in the Sync Log as failed until you reactivate the Page'
        for name, text in surfaces:
            with self.subTest(surface=name):
                self.assertTrue(new in _flat(text),
                                '%s lacks %r' % (name, new))

    def test_copy_covers_companies_routing_recovery_and_privacy(self):
        # The store page and both guides name the screens a new admin needs
        # for companies, Page routing, recovery and personal data, with the
        # labels the views use.
        root = self._root()
        store = 'static/description/index.html'
        guide = 'static/doc/setup_guide.html'
        needed = {
            store: ('Company Override', 'Lead Routing', 'Erase Personal Data',
                    'Data retention', 'Webhook Events', 'Removed Meta Leads',
                    'Does it work with several companies?'),
            guide: ('Company Override', 'Lead Company', 'Create As',
                    'Default Salesperson', 'Default Sales Team',
                    'System Health', 'Retry Selected', 'Resume Lead Reads',
                    'Removed Meta Leads', 'Erase Personal Data',
                    'Sync log retention', 'Processed webhook rows',
                    'Failed rows'),
        }
        surfaces = [(rel, _read(os.path.join(root, rel))) for rel in needed]
        surfaces += [(name, text) for name, text in self._doc_sources()
                     if name == 'docs/connecting-to-meta.md']
        for name, text in surfaces:
            flat = _flat(text)
            with self.subTest(surface=name):
                for phrase in needed.get(name, needed[guide]):
                    self.assertTrue(phrase in flat,
                                    '%s lacks %r' % (name, phrase))
        ids = ('companies', 'routing', 'health', 'recovery',
               'archived-pages', 'privacy', 'troubleshooting')
        text = _read(os.path.join(root, guide))
        for anchor in ids:
            with self.subTest(anchor=anchor):
                self.assertIn('id="%s"' % anchor, text)

    def test_manifest_summary_says_at_most_one(self):
        summary = ' '.join(self._manifest()['summary'].split())
        phrase = 'at most one CRM lead per Meta lead ID'
        self.assertTrue(phrase in summary, 'summary lacks %r' % phrase)

    def test_setup_guide_has_a_tab_icon(self):
        # The guide opens in a tab of its own. Without an icon link the
        # browser asks for /favicon.ico, which Odoo only serves with the
        # website app, and logs a 404 on every other database.
        root = self._root()
        text = _COMMENT.sub(
            '', _read(os.path.join(root, 'static/doc/setup_guide.html')))
        head = re.search(r'<head\b[^>]*>(.*?)</head\s*>', text,
                         re.DOTALL | re.IGNORECASE)
        self.assertTrue(head, 'setup guide has no <head>')
        hrefs = []
        for tag in re.findall(r'<link\b[^>]*>', head.group(1), re.IGNORECASE):
            rel = re.search(r'''\brel\s*=\s*["']([^"']*)["']''', tag, re.I)
            href = re.search(r'''\bhref\s*=\s*["']([^"']*)["']''', tag, re.I)
            if rel and href and 'icon' in rel.group(1).lower().split():
                hrefs.append(href.group(1))
        self.assertTrue(hrefs, 'setup guide head lacks <link rel="icon">')
        prefix = '/meta_lead_ads/'
        for href in hrefs:
            self.assertTrue(href.startswith(prefix),
                            'icon %r is not served by this module' % href)
            path = os.path.join(root, *href[len(prefix):].split('/'))
            self.assertTrue(os.path.isfile(path),
                            'icon %r is not in the module' % href)

# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Phone dedup key, shared by the stored lead column, the ingest lookup and
upgrade scripts.

Nothing here uses the ORM, so a migration script can import this module
before the models are set up.
"""
import re

# Odoo treats phonenumbers as optional; tests patch this name to None to
# exercise the fallback.
try:
    import phonenumbers
except ImportError:
    phonenumbers = None


def phone_dedup_key(raw, country_code=None, phone_code=None):
    """Return the digits-only dedup key for ``raw``, or False.

    A number written with '+' (or '00' when a country is known) is read as
    written. Any other number is read in the given country: ``country_code``
    is the ISO code ('EG') and ``phone_code`` the calling code (20). With no
    country the key is just the digits, as before.

    The key never carries the '+', so the library path (E.164 minus '+') and
    the fallback agree, and keys stay valid if phonenumbers is installed later.
    Without the library, or when it can't parse the number or finds it
    invalid, the fallback rules apply: one trunk '0' is dropped and the
    calling code put in front. Digits that already start with the calling
    code and are at least 8 digits longer than it are taken as typed with
    the code but without the '+', so the code isn't added twice.

    Every path drops the leading '1' of an 11-digit key, so North American
    keys stay what they were.

    Known limits:
    (a) fallback only: a national number typed without its trunk '0' that
        happens to begin with the calling code digits and is long enough is
        read as already international. At worst that misses a match.
    (b) both modes: because of the North American '1' strip, '+1 201 555
        0123' and '2015550123' typed without '+' for an Egypt company share
        the key '2015550123'. Dedup pools are per company, so this only
        matters inside one company that has both kinds of lead.
    """
    if not isinstance(raw, str):
        return False
    text = raw.strip()
    digits = re.sub(r'\D', '', text)
    if not digits:
        return False
    plus = text.startswith('+')
    international = plus or bool(country_code and digits.startswith('00'))

    key = None
    if phonenumbers is not None and (international or country_code):
        try:
            number = phonenumbers.parse(text, None if plus else country_code)
        except phonenumbers.NumberParseException:
            number = None
        if number is not None and phonenumbers.is_valid_number(number):
            key = re.sub(r'\D', '', phonenumbers.format_number(
                number, phonenumbers.PhoneNumberFormat.E164))

    if key is None:
        if plus:
            # Calling codes never start with 0, so zeros after a '+' are noise.
            key = digits.lstrip('0')
        elif country_code and digits.startswith('00'):
            # '00' is the usual way to dial out, so what follows is the
            # calling code and the number.
            key = digits[2:]
        elif phone_code:
            pc = str(phone_code)
            if digits.startswith('0'):
                # National form: drop the trunk '0' and add the calling code.
                rest = digits[1:]
                key = pc + rest if rest else ''
            elif digits.startswith(pc) and len(digits) >= len(pc) + 8:
                # Typed with the calling code but no '+'.
                key = digits
            else:
                key = pc + digits
        else:
            key = digits

    if len(key) == 11 and key.startswith('1'):
        key = key[1:]
    return key or False

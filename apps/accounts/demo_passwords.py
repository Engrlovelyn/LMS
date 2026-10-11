"""Deterministic demo passwords for seeded and imported accounts.

Imported staff previously received a random 16-character password that was
never recorded anywhere, so every imported account was effectively
un-signinable. Staff now get a reproducible password derived from their first
name, which matches the seeded official accounts (``lovelyn12345``).

This is intended for the pilot/demo dataset only. First names are not unique,
so two staff sharing a first name also share a password.
"""

import re

PASSWORD_SUFFIX = "12345"


def default_password_for(first_name):
    """Return ``<first name>12345``.

    Non-alphanumeric characters are stripped, so "Ma." yields "ma12345"
    rather than "ma.12345", and a blank name falls back to a generic value
    rather than producing a password that is just the suffix.
    """
    base = re.sub(r"[^a-z0-9]+", "", (first_name or "").lower())
    return "{}{}".format(base or "user", PASSWORD_SUFFIX)

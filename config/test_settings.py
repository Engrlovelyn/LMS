"""Test settings.

Kept as a thin layer over the base settings; a SQLite test database is already
selected automatically whenever Django's test runner is invoked (see the
``"test" in sys.argv`` switch in ``config/settings.py``). This module exists so
``python manage.py test --settings=config.test_settings`` also works.
"""

from .settings import *  # noqa: F401,F403

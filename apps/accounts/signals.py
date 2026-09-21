"""Role-group sync is handled entirely by the signals in accounts/models.py,
which reconcile group membership against the user's active academic-year role
assignments (`_sync_user_groups`).

This module is kept as a no-op so the `ready()` import in apps.py keeps working
and there is exactly one source of truth for group membership.
"""
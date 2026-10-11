"""Server-side single active session enforcement.

The project's non-functional requirements allow one authenticated device per
account at a time. ``LoginSession`` already records every sign-in, so this
module turns that record into the enforcement point:

* ``active_session_for`` decides whether an account already has a *live*
  session on another device. Stale rows (their Django session deleted or
  expired, or idle past the configured timeout) are purged first so an
  abandoned browser session can never lock a user out permanently.
* ``release_session`` is called on logout so signing out frees the account.
* ``release_all_sessions`` closes every device, used after a password reset or
  an administrative action that should force a fresh sign-in.

Enforcement happens inside the login view under a ``select_for_update`` lock on
the user row, so two simultaneous sign-in requests cannot both pass the check.
"""

from datetime import timedelta

from django.contrib.sessions.models import Session
from django.utils import timezone

from .models import LoginSession

DEFAULT_TIMEOUT_MINUTES = 30

ACTIVE_SESSION_MESSAGE = (
    "This account is already signed in on another device. Sign out there "
    "first, then try again. If you cannot reach that device, contact your "
    "System Administrator to end the other session."
)


def timeout_minutes():
    """Idle timeout from System Settings, or the built-in default."""
    try:
        from apps.system_settings.models import SystemConfiguration

        return SystemConfiguration.get_solo().session_timeout_minutes or 0
    except Exception:
        return DEFAULT_TIMEOUT_MINUTES


def _is_live(login_session, now):
    """True when ``login_session`` still backs a usable browser session.

    Liveness is decided from server-side state only: the Django session row
    must exist and not have expired, and the device must not have been idle
    longer than the configured timeout. A client-supplied session key alone is
    never trusted, because a forged or stale value simply will not match a
    live server-side row.
    """
    if not login_session.session_key:
        return False

    session = Session.objects.filter(session_key=login_session.session_key).first()
    if session is None or session.expire_date <= now:
        return False

    minutes = timeout_minutes()
    if minutes and login_session.last_active_at:
        if (now - login_session.last_active_at) > timedelta(minutes=minutes):
            return False
    return True


def active_session_for(user, now=None):
    """Return a live ``LoginSession`` for ``user``, purging stale rows.

    ``None`` means the account has no active device and may sign in.
    """
    now = now or timezone.now()
    live = None
    stale_ids = []
    for login_session in LoginSession.objects.filter(user=user):
        if _is_live(login_session, now):
            # Keep the newest live row; anything else is a duplicate.
            if live is None:
                live = login_session
            else:
                stale_ids.append(login_session.pk)
        else:
            stale_ids.append(login_session.pk)

    if stale_ids:
        Session.objects.filter(
            session_key__in=LoginSession.objects.filter(pk__in=stale_ids)
            .exclude(session_key__isnull=True)
            .values_list("session_key", flat=True)
        ).delete()
        LoginSession.objects.filter(pk__in=stale_ids).delete()

    return live


def release_session(session_key):
    """Delete the ``LoginSession`` row backing ``session_key`` (on logout)."""
    if not session_key:
        return 0
    queryset = LoginSession.objects.filter(session_key=session_key)
    count = queryset.count()
    queryset.delete()
    return count


def release_other_sessions(user, keep_session_key=None):
    """End every device for ``user`` except ``keep_session_key``.

    Used at sign-in: the incoming device keeps its own Django session row (so
    the session middleware can still save it), while any superseded device is
    removed. Deleting the *current* row here would raise ``SessionInterrupted``
    when the middleware next saves the response.
    """
    queryset = LoginSession.objects.filter(user=user)
    if keep_session_key:
        queryset = queryset.exclude(session_key=keep_session_key)
    keys = list(
        queryset.exclude(session_key__isnull=True).values_list("session_key", flat=True)
    )
    if keys:
        Session.objects.filter(session_key__in=keys).delete()
    count, _ = queryset.delete()
    return count


def record_login_session(user, session_key, device_info="", ip_address=None):
    """Record the single device row for a freshly authenticated session."""
    LoginSession.objects.filter(user=user, session_key=session_key).delete()
    return LoginSession.objects.create(
        user=user,
        device_info=device_info,
        ip_address=ip_address,
        session_key=session_key,
    )


def release_all_sessions(user):
    """End every device for ``user`` and drop the matching Django sessions."""
    keys = list(
        LoginSession.objects.filter(user=user)
        .exclude(session_key__isnull=True)
        .values_list("session_key", flat=True)
    )
    if keys:
        Session.objects.filter(session_key__in=keys).delete()
    deleted, _ = LoginSession.objects.filter(user=user).delete()
    return deleted


def touch_session(session_key, now=None):
    """Refresh ``last_active_at`` for the device holding ``session_key``.

    Called from the session middleware, throttled by the caller, so a long
    interactive session is never mistaken for an abandoned one.
    """
    if not session_key:
        return
    now = now or timezone.now()
    LoginSession.objects.filter(session_key=session_key).update(last_active_at=now)

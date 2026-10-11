"""Session inactivity timeout and forced password change for authenticated users.

The idle limit comes from the singleton SystemConfiguration (default 30 min,
configurable in System Settings). When a logged-in user exceeds it, their
session is ended and they are sent back to the login page, which displays a
"session expired" notice.
"""

from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

DEFAULT_TIMEOUT_MINUTES = 30

# Endpoints a user with an outstanding password change still has to reach.
# Everything else is bounced to the change-password screen -- otherwise the
# session flag set at login is advisory only and a user can simply click a
# dashboard link instead of choosing their own password.
PASSWORD_CHANGE_EXEMPT_PREFIXES = (
    "/accounts/logout/",
    "/accounts/change-password/",
    "/accounts/api/login/",
    "/accounts/api/forgot/",
    "/static/",
    "/media/",
)


class NoCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.get("Content-Type", "").startswith("text/html"):
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
        return response


class SessionTimeoutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            timeout_minutes = self._timeout_minutes()
            now = timezone.now()
            last_activity = request.session.get("last_activity_at")
            if timeout_minutes and last_activity:
                if (now.timestamp() - last_activity) > timeout_minutes * 60:
                    session_key = request.session.session_key
                    logout(request)
                    # Free the single-device slot: an idle timeout is an
                    # abandoned session, not a lock the user must wait out.
                    from .session_guard import release_session

                    release_session(session_key)
                    request.session["session_expired"] = True
                    return redirect(reverse("accounts:login_page"))
            request.session["last_activity_at"] = now.timestamp()
            self._touch_login_session(request, now)
        return self.get_response(request)

    @staticmethod
    def _touch_login_session(request, now):
        """Refresh the device row's last_active_at, at most once a minute.

        Without this the login-time timestamp would be the only one recorded,
        and a genuinely active session would look abandoned as soon as the
        timeout window elapsed from sign-in.
        """
        last = request.session.get("login_session_touched_at")
        if last and (now.timestamp() - last) < 60:
            return
        from .session_guard import touch_session

        touch_session(request.session.session_key, now)
        request.session["login_session_touched_at"] = now.timestamp()

    @staticmethod
    def _timeout_minutes():
        try:
            from apps.system_settings.models import SystemConfiguration

            return SystemConfiguration.get_solo().session_timeout_minutes or 0
        except Exception:
            return DEFAULT_TIMEOUT_MINUTES


class MustChangePasswordMiddleware:
    """Hold a user on the change-password screen until they pick their own password.

    Set by ``User.must_change_password``, which an administrator's
    forgot-password recovery turns on: the account is handed a password the
    administrator knows, so letting the user keep it defeats the point.

    The check is on the model rather than the session flag so it survives a
    session refresh and applies on every request, including ones that never
    pass through the login view.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and getattr(user, "must_change_password", False)
            and not self._is_exempt(request)
        ):
            request.session["force_password_change"] = True
            return redirect(reverse("accounts:change_password") + "?reset=1")
        return self.get_response(request)

    @staticmethod
    def _is_exempt(request):
        path = request.path
        if any(path.startswith(prefix) for prefix in PASSWORD_CHANGE_EXEMPT_PREFIXES):
            return True
        # The dashboard's own POST/GET endpoints run while the user is parked on
        # the change-password screen; a redirect loop through them would trap
        # the browser, so they are allowed to fail normally instead.
        return path.startswith("/api/")
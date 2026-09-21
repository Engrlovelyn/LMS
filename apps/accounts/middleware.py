"""Session inactivity timeout for authenticated users.

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
                    logout(request)
                    request.session["session_expired"] = True
                    return redirect(reverse("accounts:login_page"))
            request.session["last_activity_at"] = now.timestamp()
        return self.get_response(request)

    @staticmethod
    def _timeout_minutes():
        try:
            from apps.system_settings.models import SystemConfiguration

            return SystemConfiguration.get_solo().session_timeout_minutes or 0
        except Exception:
            return DEFAULT_TIMEOUT_MINUTES
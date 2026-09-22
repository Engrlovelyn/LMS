"""Maintenance mode enforcement.

When ``SystemConfiguration.maintenance_mode`` is enabled, all normal traffic is
blocked with HTTP 503. System Administrators (and superusers) as well as the
authentication endpoints stay reachable so the platform can be serviced and
users can still log in/out.
"""

from django.http import HttpResponse

EXEMPT_PREFIXES = (
    "/admin",
    "/static",
    "/media",
    "/accounts/login",
    "/accounts/api/login",
    "/accounts/logout",
    "/accounts/verify",
    "/accounts/reset",
    "/accounts/api/forgot",
)

MAINTENANCE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SVI LMS - Under Maintenance</title>
  <style>
    body { font-family: 'Plus Jakarta Sans', Arial, sans-serif; background: #f4f7fc;
           color: #1e2e42; display: flex; min-height: 100vh; margin: 0;
           align-items: center; justify-content: center; text-align: center; }
    .box { max-width: 460px; padding: 40px; background: #fff; border-radius: 14px;
           box-shadow: 0 4px 24px rgba(10,36,99,.12); }
    h1 { color: #0a2463; font-size: 22px; margin: 0 0 12px; }
    p { color: #4a637f; line-height: 1.6; margin: 0; }
  </style>
</head>
<body>
  <div class="box">
    <h1>SVI LMS is under maintenance</h1>
    <p>We are performing scheduled maintenance and will be back shortly.
       Please try again later. If you are a System Administrator, you can still
       sign in to manage the platform.</p>
  </div>
</body>
</html>"""


class MaintenanceModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_exempt(request.path) or not self._maintenance_on():
            return self.get_response(request)

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            if user.is_superuser or user.groups.filter(name="system_admin").exists():
                return self.get_response(request)

        return HttpResponse(
            MAINTENANCE_HTML, status=503, content_type="text/html"
        )

    @staticmethod
    def _is_exempt(path):
        return path.startswith(EXEMPT_PREFIXES)

    @staticmethod
    def _maintenance_on():
        try:
            from .models import SystemConfiguration

            return SystemConfiguration.get_solo().maintenance_mode
        except Exception:
            return False

"""Temporary localhost-only smoke server for dedicated test accounts.

Every request is bound to a user whose address includes ``test`` and runs in a
PostgreSQL read-only transaction. Only GET/HEAD are accepted.
"""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.modules["live_readonly_smoke"] = sys.modules[__name__]

import django

django.setup()

from django.conf import settings

settings.DEBUG = True
settings.MIDDLEWARE.append("live_readonly_smoke.LiveReadOnlyMiddleware")

from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.http import HttpResponse
from django.core.wsgi import get_wsgi_application
from django.contrib.staticfiles.handlers import StaticFilesHandler
from wsgiref.simple_server import make_server

class LiveReadOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.META.get("REMOTE_ADDR") not in {"127.0.0.1", "::1"}:
            return HttpResponse(status=403)
        if request.method not in {"GET", "HEAD"}:
            return HttpResponse("Read-only smoke server", status=405)
        role = request.headers.get("X-Smoke-Role", "")
        if not role:
            request.user = AnonymousUser()
            return self.get_response(request)

        connection.set_autocommit(False)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
            from apps.accounts.models import User

            request.user = (
                User.objects.filter(email__icontains="test", groups__name=role)
                .order_by("pk")
                .first()
            )
            if request.user is None:
                return HttpResponse("No dedicated test account for this role", status=404)
            request.session = {}
            response = self.get_response(request)
            return response
        finally:
            connection.rollback()
            connection.set_autocommit(True)


application = StaticFilesHandler(get_wsgi_application())


if __name__ == "__main__":
    print("Read-only test server: http://127.0.0.1:8020", flush=True)
    make_server("127.0.0.1", 8020, application).serve_forever()

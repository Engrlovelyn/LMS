"""
ASGI config for config project.

Exposes both the HTTP (Django) and WebSocket (realtime chat) applications.
Served by `python manage.py runserver` in dev and `daphne config.asgi:application`
in production.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

import apps.communication.routing as communication_routing

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(
            URLRouter(communication_routing.websocket_urlpatterns)
        ),
    }
)
from django.urls import re_path

from apps.communication import consumers

websocket_urlpatterns = [
    re_path(r"^ws/chat/$", consumers.ChatConsumer.as_asgi()),
    re_path(r"^ws/live/$", consumers.LiveConsumer.as_asgi()),
]

import json

from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer

from apps.accounts.models import User
from apps.communication.models import GroupMessage, PrivateMessage
from apps.virtual_classrooms.models import VirtualClassroom


def _display_name(user):
    if hasattr(user, "employee_profile"):
        p = user.employee_profile
        return f"{p.first_name} {p.last_name}".strip() or user.email
    if hasattr(user, "student_profile"):
        p = user.student_profile
        return f"{p.first_name} {p.last_name}".strip() or user.email
    return user.email



class ChatConsumer(WebsocketConsumer):
    """
    Realtime chat over Redis. The frontend opens one socket to /ws/chat/.

    Rooms:
      user-<id>    -> private messages addressed to that user
      class-<id>   -> group messages for that classroom

    On connect the user joins their own DM room plus every classroom they
    belong to, so they receive live messages without polling. Messages are
    written to PostgreSQL here (same GroupMessage/PrivateMessage models the
    /api/messages/ endpoint uses) and then broadcast to the room.
    """

    def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            # 4401 is "unauthorised" in the 4000-4999 application-private range.
            # 4001 is not a valid close code (the private range starts at 4000),
            # so browsers and the ASGI spec both reject it and the socket just
            # drops with no reason the client can read.
            self.close(code=4401)
            return
        self.user_room = f"user-{self.user.pk}"
        for room in [self.user_room] + [f"class-{vc.pk}" for vc in self._classrooms()]:
            async_to_sync(self.channel_layer.group_add)(room, self.channel_name)
        self.accept()
        self.send(text_data=json.dumps({"type": "hello", "me": self.user.pk}))

    def disconnect(self, close_code):
        if not getattr(self, "user", None) or not self.user.is_authenticated:
            return
        rooms = [self.user_room] + [f"class-{vc.pk}" for vc in self._classrooms()]
        for room in rooms:
            async_to_sync(self.channel_layer.group_discard)(room, self.channel_name)

    def _classrooms(self):
        from apps.communication.views import _classrooms_for_user

        return list(_classrooms_for_user(self.user))

    def receive(self, text_data):
        try:
            data = json.loads(text_data or "{}")
        except ValueError:
            self._error("Invalid message.")
            return
        if data.get("action") == "send":
            self._handle_send(data)
        else:
            self._error("Unknown action.")

    def _handle_send(self, data):
        body = (data.get("body") or "").strip()
        if not body:
            self._error("Message text is required.")
            return

        kind = data.get("kind")
        user = self.user

        if kind == "dm":
            peer = User.objects.filter(pk=data.get("peer_id"), status=User.Status.ACTIVE).first()
            if peer is None or peer.pk == user.pk:
                self._error("Recipient not found.")
                return
            # Same rule as POST /api/messages/ - a check on only one transport
            # is not a check, otherwise this socket would be a way around it.
            from apps.communication.access import can_message

            if not can_message(user, peer):
                self._error("You do not share a class with this user.")
                return
            msg = PrivateMessage.objects.create(sender=user, recipient=peer, content=body)
            payload = self._payload(msg, kind="dm", thread=f"p-{peer.pk}", peer_id=peer.pk)
            for room in (f"user-{peer.pk}", f"user-{user.pk}"):
                async_to_sync(self.channel_layer.group_send)(
                    room, {"type": "chat.message", "payload": payload}
                )
            return

        if kind == "group":
            classroom = VirtualClassroom.objects.filter(pk=data.get("classroom_id")).first()
            if classroom is None or classroom not in self._classrooms():
                self._error("You do not have access to this class chat.")
                return
            msg = GroupMessage.objects.create(sender=user, virtual_classroom=classroom, content=body)
            payload = self._payload(msg, kind="group", thread=f"c-{classroom.pk}", classroom_id=classroom.pk)
            async_to_sync(self.channel_layer.group_send)(
                f"class-{classroom.pk}", {"type": "chat.message", "payload": payload}
            )
            return

        self._error("Invalid message kind.")

    def chat_message(self, event):
        self.send(text_data=json.dumps(event["payload"]))

    def _payload(self, msg, kind, thread, peer_id=None, classroom_id=None):
        sender = msg.sender
        return {
            "type": "chat",
            "id": msg.pk,
            "kind": kind,
            "thread": thread,
            "peer_id": peer_id,
            "classroom_id": classroom_id,
            "sender_id": sender.pk,
            "sender": _display_name(sender),
            "text": msg.content,
            "time": msg.sent_at.strftime("%Y-%m-%d %H:%M"),
        }

    def _error(self, message):
        self.send(text_data=json.dumps({"type": "error", "message": message}))


class LiveConsumer(WebsocketConsumer):
    """
    "Something changed" signal used by every dashboard to stay current without
    a page refresh. One socket per dashboard at /ws/live/.

    Rooms joined on connect:
      school       - every authenticated user (school-wide changes)
      user-<id>    - changes that concern only this person
      class-<id>   - changes in each virtual classroom the user belongs to

    The server sends ``{"type": "live", "classroom_id": ..., "kinds": [...]}``
    and the client re-fetches only the panel that the kind affects. Payloads
    carry no record contents, so nothing is duplicated in the push payload and
    the API remains the only source of truth.
    """

    def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            self.close(code=4401)
            return
        from apps.communication.realtime import class_room, user_room

        rooms = ["school", user_room(self.user.pk)] + [class_room(vc.pk) for vc in self._classrooms()]
        for room in rooms:
            async_to_sync(self.channel_layer.group_add)(room, self.channel_name)
        self.rooms = rooms
        # No handshake: ``hello`` existed so a client could learn its own id
        # before the first event. Every dashboard that reads the socket already
        # knew who it was, so the greeting only ever appeared as an unread
        # first message that a client would have to skip. Nothing depends on it.
        self.accept()

    def disconnect(self, close_code):
        if not getattr(self, "user", None) or not self.user.is_authenticated:
            return
        for room in getattr(self, "rooms", []):
            async_to_sync(self.channel_layer.group_discard)(room, self.channel_name)

    def _classrooms(self):
        from apps.communication.views import _classrooms_for_user

        return list(_classrooms_for_user(self.user))

    def receive(self, text_data=None, bytes_data=None):
        # This socket is server -> client only. The browser may send pings;
        # anything else is ignored on purpose so it cannot mutate anything.
        return

    def live_data(self, event):
        payload = dict(event.get("payload") or {})
        payload["type"] = "live"
        self.send(text_data=json.dumps(payload))
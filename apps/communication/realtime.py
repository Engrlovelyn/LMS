"""
Realtime "something changed" notifications.

Every dashboard keeps itself current without a page refresh. When a teacher
uploads a material, publishes an assessment, grades a submission, posts an
announcement or sends a class message, the write path calls one of the helpers
below. The payload is pushed to:

  * ``class-<id>``  - everyone in that virtual classroom (teacher + enrolled
    students). This is what makes classroom work appear on the right students'
    screens, and only those.
  * ``user-<id>``   - one person, for things that are personal (a private
    message, a grade on their own submission, being enrolled or removed).

Clients listen on ``/ws/live/`` (see ``LiveConsumer``) and re-fetch only the
panel the event affects, so nothing is pushed as raw HTML and there is no
second source of truth for what a panel should show.

``kinds`` is a short vocabulary the client filters on, for example
``{"materials"}``, ``{"assessment", "grade"}``. Pass ``kind="*"`` to mean
"anything in this classroom changed".

None of these helpers raise. Realtime is an enhancement; if Redis is down the
request must still succeed and the page must still work on refresh.
"""

import logging

logger = logging.getLogger(__name__)


def _channel_layer():
    try:
        from channels.layers import get_channel_layer

        return get_channel_layer()
    except Exception:  # pragma: no cover - channels not installed
        return None


def class_room(classroom_id):
    return f"class-{classroom_id}"


def user_room(user_id):
    return f"user-{user_id}"


async def _group_send_all(layer, rooms, message):
    for room in rooms:
        await layer.group_send(room, message)


def _dispatch(rooms, payload):
    """Push to the channel layer from either a request or a running event loop.

    These helpers are called from two very different places: ordinary Django
    views (synchronous, no loop running) and async code that calls them directly
    from inside a coroutine. ``async_to_sync`` cannot cover the second case -- it
    refuses to run when an event loop is already active in the same thread.

    When a loop is running we schedule the send on *that* loop and return
    immediately, rather than running it to completion in a worker thread. That
    matters for correctness, not just convenience: a channel layer such as
    ``InMemoryChannelLayer`` delivers into the receiving consumer's own asyncio
    queue, and doing that from a foreign thread is not safe, so the message
    never arrives. Callers in async code are already awaiting the socket, so the
    task gets to run as soon as they yield.
    """
    layer = _channel_layer()
    if layer is None:
        return False
    message = {"type": "live.data", "payload": payload}
    try:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_group_send_all(layer, rooms, message))
        else:
            loop.create_task(_group_send_all(layer, rooms, message))
        return True
    except Exception as exc:  # pragma: no cover - depends on Redis
        logger.warning("live notify failed for %s: %s", rooms, exc)
        return False


def _send(rooms, payload):
    rooms = [r for r in rooms if r]
    if not rooms:
        return False
    return _dispatch(rooms, payload)


def _safe_send(rooms, payload):
    """Call ``_send`` and swallow any failure it raises.

    ``_send`` already handles channel-layer problems, but the module contract
    is that none of these helpers raise, and callers pass the module's own
    ``_send`` through in places where a broken layer (or a test double standing
    in for one) can raise from inside it. A notification is an enhancement, so
    it must never be the reason a save fails.
    """
    try:
        return _send(rooms, payload)
    except Exception as exc:  # pragma: no cover - depends on Redis
        logger.warning("live notify failed for %s: %s", rooms, exc)
        return False


def notify_classroom(classroom_id, kind="*", actor=None, extra=None):
    """Tell everyone in a virtual classroom that something in it changed."""
    if not classroom_id:
        return False
    kinds = [kind] if isinstance(kind, str) else list(kind or [])
    payload = {
        "classroom_id": int(classroom_id),
        "kinds": kinds,
        "actor_id": getattr(actor, "pk", None),
        "actor": _actor_name(actor),
    }
    if extra:
        payload.update(extra)
    return _safe_send([class_room(classroom_id)], payload)


def notify_classrooms(classroom_ids, kind="*", actor=None, extra=None):
    """Same as notify_classroom for several classrooms (e.g. publishing to all)."""
    ids = [cid for cid in (classroom_ids or []) if cid]
    if not ids:
        return False
    kinds = [kind] if isinstance(kind, str) else list(kind or [])
    for cid in ids:
        payload = {
            "classroom_id": int(cid),
            "kinds": kinds,
            "actor_id": getattr(actor, "pk", None),
            "actor": _actor_name(actor),
        }
        if extra:
            payload.update(extra)
        _safe_send([class_room(cid)], payload)
    return True


def notify_user(user_id, kind="*", actor=None, extra=None):
    """Tell one person something changed that only concerns them."""
    if not user_id:
        return False
    kinds = [kind] if isinstance(kind, str) else list(kind or [])
    payload = {
        "classroom_id": None,
        "kinds": kinds,
        "scope": "user",
        "actor_id": getattr(actor, "pk", None),
        "actor": _actor_name(actor),
    }
    if extra:
        payload.update(extra)
    return _safe_send([user_room(user_id)], payload)


def notify_users(user_ids, kind="*", actor=None, extra=None):
    for uid in [u for u in (user_ids or []) if u]:
        notify_user(uid, kind=kind, actor=actor, extra=extra)


def notify_school(kind="*", actor=None, extra=None):
    """School-wide change (announcements, calendar events, structure edits).

    Everyone is in the ``school`` room, which ``LiveConsumer`` joins for every
    authenticated user, so this reaches all dashboards at once.
    """
    kinds = [kind] if isinstance(kind, str) else list(kind or [])
    payload = {
        "classroom_id": None,
        "kinds": kinds,
        "scope": "school",
        "actor_id": getattr(actor, "pk", None),
        "actor": _actor_name(actor),
    }
    if extra:
        payload.update(extra)
    return _safe_send(["school"], payload)


def _resolve_enrolled_ids(classroom_id):
    """Evaluate the enrolment queryset even when a loop is running.

    ``sync_to_async`` returns a coroutine, and this helper is a plain function
    called synchronously -- returning that coroutine would hand the caller an
    un-awaited object, so the ids were never fetched and the direct nudge was
    skipped. The queryset is therefore evaluated on a dedicated worker thread
    and the result is joined, which is safe here precisely because this query
    touches no channel layer.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _enrolled_user_ids(classroom_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_enrolled_user_ids, classroom_id).result()


def _enrolled_user_ids(classroom_id):
    from apps.virtual_classrooms.models import VirtualClassroomEnrollment

    return [
        i
        for i in VirtualClassroomEnrollment.objects.filter(
            virtual_classroom_id=classroom_id,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).values_list("student__user_id", flat=True)
        if i
    ]


def notify_enrolled_students(classroom_id, kind="*", actor=None, extra=None, exclude=None):
    """Classroom change plus a direct nudge to each enrolled student.

    A student who is *just* enrolled has not opened the page yet and therefore
    has not joined ``class-<id>``, so the classroom event alone would miss them.

    The enrolment lookup is a queryset, so when this is called from async
    context it has to move to a thread. Evaluated inline it raises
    SynchronousOnlyOperation, which the guard below used to swallow -- making
    this helper silently return False and skip the direct nudge entirely.
    """
    notify_classroom(classroom_id, kind=kind, actor=actor, extra=extra)
    try:
        ids = _resolve_enrolled_ids(classroom_id)
        skip = getattr(exclude, "pk", None)
        notify_users([i for i in ids if i and i != skip], kind=kind, actor=actor, extra=extra)
    except Exception as exc:  # pragma: no cover
        logger.warning("live notify to enrolled students failed: %s", exc)
        return False
    return True


def _actor_name(user):
    """A display name for the person who caused a change.

    These helpers are called from async context as well as from views, and
    ``user.employee_profile`` is a lazy reverse OneToOne -- reading it issues a
    query, which raises SynchronousOnlyOperation on the event loop. So only a
    profile that the caller already loaded (``select_related`` or
    ``prefetch_related``) is used, and anything else falls back to the email
    rather than triggering a query that would break the notification.
    """
    if not user:
        return None
    cached = getattr(user, "_prefetched_objects_cache", None) or {}
    for attr in ("employee_profile", "student_profile"):
        profile = cached.get(attr)
        if profile is None:
            # Already-cached one-to-one lives in the field cache, not the
            # prefetch cache; reading it from there cannot hit the database.
            profile = getattr(getattr(user, "_state", None), "fields_cache", {}).get(attr)
        if profile is not None:
            name = f"{profile.first_name} {profile.last_name}".strip()
            if name:
                return name
    return getattr(user, "email", None)

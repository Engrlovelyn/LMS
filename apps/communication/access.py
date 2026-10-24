"""
Who is allowed to exchange direct messages with whom.

A direct message is only ever between a student and the teacher of a class they
are both in, or a teacher and a student of a class they teach. That rule lives in
here so the REST endpoint and the WebSocket consumer cannot drift apart - a
check that only exists on one transport is not a check.

Staff keep school-wide reach, matching ``_classrooms_for_user``.
"""

from apps.accounts.models import User
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

STAFF_GROUPS = {
    "system_admin",
    "registrar",
    "principal",
    "elementary_principal",
    "assistant_principal",
}


def can_message(user, peer):
    """True when ``user`` may open a direct conversation with ``peer``."""
    if user is None or peer is None:
        return False
    if user.pk == peer.pk:
        return False
    if not getattr(user, "is_authenticated", False) or not getattr(peer, "is_authenticated", False):
        return False

    from apps.communication.views import _classrooms_for_user

    roles = set(user.groups.values_list("name", flat=True))
    if user.is_superuser or roles & STAFF_GROUPS:
        return True

    # A student may write to the teacher of a class they are enrolled in.
    if "student" in roles and hasattr(user, "student_profile"):
        return _classrooms_for_user(user).filter(teacher=peer).exists()

    # A teacher may write to a student enrolled in a class they teach.
    if "teacher" in roles:
        return (
            _classrooms_for_user(user)
            .filter(
                enrollments__student__user=peer,
                enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            )
            .exists()
        )

    return False


def display_name(user):
    """A person's name, falling back to their email when no profile exists."""
    if user is None:
        return ""
    for attr in ("employee_profile", "student_profile"):
        profile = getattr(user, attr, None)
        if profile is not None:
            name = "{} {}".format(profile.first_name, profile.last_name).strip()
            return name or user.email
    return user.email


def messageable_contacts(user):
    """Everyone ``user`` may start a direct conversation with.

    Returns ``(contacts, by_classroom)``:

    ``contacts``
        A flat, de-duplicated picker list: ``{id, name, role, classroom_id}``.
    ``by_classroom``
        The same people keyed by classroom id, so a dashboard opened on one
        virtual classroom can offer only that classroom's counterpart - a student
        sees that class's teacher, a teacher sees that class's students.

    This is the same directional rule :func:`can_message` enforces, so the list
    can never offer someone the send path would then reject. Without it a
    conversation could only ever be started from history: a student who had never
    written to their teacher had no contact to pick at all.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return [], {}

    from apps.communication.views import _classrooms_for_user

    roles = set(user.groups.values_list("name", flat=True))
    is_staff = bool(user.is_superuser or (roles & STAFF_GROUPS))
    wants_teachers = is_staff or ("student" in roles)
    wants_students = is_staff or ("teacher" in roles)
    if not (wants_teachers or wants_students):
        return [], {}

    contacts = []
    by_classroom = {}
    seen = set()
    vc_ids = list(_classrooms_for_user(user).values_list("pk", flat=True))
    if not vc_ids:
        return [], {}

    for vc in VirtualClassroom.objects.filter(pk__in=vc_ids).select_related("teacher"):
        bucket = []
        if wants_teachers and vc.teacher_id and vc.teacher_id != user.pk and vc.teacher.status == User.Status.ACTIVE:
            entry = {"id": vc.teacher_id, "name": display_name(vc.teacher), "role": "Teacher", "classroom_id": vc.pk}
            bucket.append(entry)
            if vc.teacher_id not in seen:
                seen.add(vc.teacher_id)
                contacts.append(entry)
        if wants_students:
            # Every learner enrolled in the room, whatever their account status:
            # a teacher (or staff member) picks recipients from the class roster,
            # and learners are created "pending_verification" until they activate
            # their account, so keying the picker on ACTIVE hid the very class
            # the teacher runs. Only archived accounts are left out.
            enrolls = (
                VirtualClassroomEnrollment.objects.filter(
                    virtual_classroom=vc,
                    status=VirtualClassroomEnrollment.STATUS_ACTIVE,
                )
                .exclude(student__user__status=User.Status.ARCHIVED)
                .select_related("student__user")
                .distinct()
            )
            for enrollment in enrolls:
                su = enrollment.student.user
                if su.pk == user.pk:
                    continue
                entry = {"id": su.pk, "name": display_name(su), "role": "Student", "classroom_id": vc.pk}
                bucket.append(entry)
                if su.pk not in seen:
                    seen.add(su.pk)
                    contacts.append(entry)
        by_classroom[str(vc.pk)] = bucket

    contacts.sort(key=lambda c: (c["role"], c["name"]))
    return contacts, by_classroom

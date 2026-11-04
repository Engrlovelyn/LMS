"""Creation and access helpers for the persistent header-bell notifications.

Every producer (a class announcement, a learning material placed in a
classroom, an assessment assigned to a classroom) funnels through the three
``notify_*`` helpers here rather than building ``Notification`` rows inline.
That keeps three rules in one place:

  * only *active* classroom members are recipients, and the author is skipped;
  * ``get_or_create`` on the ``(recipient, type, source)`` key, so editing a
    published object never doubles the red dot;
  * a single failure (a bad classroom id, no students) can never break the save
    that triggered it -- a notification is an enhancement.

The module never imports ``assessments`` or ``teaching_learning`` at import
time; the two producers that need their model constants do so lazily, matching
the import direction already used by ``assessments.views._notify``.
"""

import logging
from urllib.parse import urlencode

from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.virtual_classrooms.models import VirtualClassroomEnrollment

from .models import Notification

logger = logging.getLogger(__name__)


def active_student_user_ids(vc):
    """The user ids of every student actively enrolled in ``vc``.

    Students are identified by the ``VirtualClassroomEnrollment`` row, which is
    exactly what the realtime helpers use, so the bell and the live refresh
    always agree about who counts as "in this class".
    """
    if vc is None:
        return []
    return [
        uid
        for uid in VirtualClassroomEnrollment.objects.filter(
            virtual_classroom=vc,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).values_list("student__user_id", flat=True)
        if uid
    ]


def _reverse_or_blank(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:  # pragma: no cover - a URL rename would surface in tests
        return ""


def _teacher_dashboard_target(n, user):
    """Build an in-app destination for the active teacher dashboard SPA."""
    if not user or not user.groups.filter(name="teacher").exists():
        return ""

    classroom_id = n.virtual_classroom_id
    record_id = n.source_id
    record_type = ""
    tab = "dashboard"

    if n.source_type == "assessment_proctoring_event":
        from apps.assessments.models import AssessmentAssignment, AssessmentProctoringEvent
        from apps.virtual_classrooms.models import VirtualClassroomEnrollment

        event = AssessmentProctoringEvent.objects.filter(pk=n.source_id).first()
        if not event or not classroom_id:
            return ""
        assignment_exists = AssessmentAssignment.objects.filter(
            assessment_id=event.assessment_id,
            virtual_classroom_id=classroom_id,
            virtual_classroom__teacher=user,
            virtual_classroom__status="active",
            virtual_classroom__enrollments__student_id=event.student_id,
            virtual_classroom__enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).exists()
        if not assignment_exists:
            return ""
        tab = "activitylog"
        record_type = "proctoring"
    elif n.notification_type == Notification.TYPE_ASSESSMENT:
        from apps.assessments.models import AssessmentAssignment

        tab = "assessment"
        record_type = "assessment"
        if n.source_type in ("assessment_assignment", "assessment_reminder"):
            assignment = AssessmentAssignment.objects.filter(pk=n.source_id).first()
            if not assignment:
                return ""
            if classroom_id and assignment.virtual_classroom_id != classroom_id:
                return ""
            classroom_id = assignment.virtual_classroom_id
            record_id = assignment.assessment_id
    elif n.source_type == "resource_placement":
        from apps.teaching_learning.models import LearningResourcePlacement

        placement = LearningResourcePlacement.objects.filter(pk=n.source_id).first()
        if not placement:
            return ""
        classroom_id = placement.virtual_classroom_id
        record_id = placement.pk
        record_type = "resource"
        tab = "tla"
    elif n.notification_type == Notification.TYPE_MATERIAL:
        tab = "tla"
        record_type = "resource"
        if n.source_type == "resource" and classroom_id:
            from apps.teaching_learning.models import LearningResourcePlacement

            placement = LearningResourcePlacement.objects.filter(
                resource_id=n.source_id, virtual_classroom_id=classroom_id
            ).order_by("-placed_at").first()
            if placement:
                record_id = placement.pk
    elif n.source_type in ("class_announcement", "school_announcement"):
        tab = "announcements"
        record_type = "announcement"
        if n.source_type == "school_announcement":
            record_id = "school-{}".format(n.source_id)
    elif n.source_type == "attendance":
        tab = "attendance"
        record_type = "attendance"
    elif n.notification_type == Notification.TYPE_MESSAGE:
        tab = "messages"
        record_type = "message"

    if classroom_id:
        from apps.virtual_classrooms.models import VirtualClassroom

        if not VirtualClassroom.objects.filter(
            pk=classroom_id, teacher=user, status="active"
        ).exists():
            return ""

    params = {"notification_tab": tab}
    if classroom_id:
        params["notification_classroom"] = classroom_id
    if record_id and record_type:
        params["notification_record_type"] = record_type
        params["notification_record"] = record_id
    return "{}?{}".format(reverse("dashboards:teacher"), urlencode(params))


def _student_dashboard_target(n, user):
    """Resolve a student's notification to the active student dashboard SPA."""
    if not user or getattr(user, "student_profile", None) is None:
        return ""

    classroom_id = n.virtual_classroom_id
    record_id = n.source_id
    record_type = ""
    tab = "announcements"

    if n.notification_type == Notification.TYPE_ASSESSMENT:
        from apps.assessments.models import AssessmentAssignment

        tab = "assessments"
        record_type = "assessment"
        if n.source_type in ("assessment_assignment", "assessment_reminder"):
            assignment = AssessmentAssignment.objects.filter(pk=n.source_id).first()
            if not assignment:
                return ""
            if classroom_id and assignment.virtual_classroom_id != classroom_id:
                return ""
            classroom_id = assignment.virtual_classroom_id
            record_id = assignment.assessment_id
    elif n.source_type == "resource_placement":
        from apps.teaching_learning.models import LearningResourcePlacement

        placement = LearningResourcePlacement.objects.filter(pk=n.source_id).first()
        if not placement:
            return ""
        classroom_id = placement.virtual_classroom_id
        record_id = placement.pk
        record_type = "resource"
        tab = "materials"
    elif n.notification_type == Notification.TYPE_MATERIAL:
        tab = "materials"
        record_type = "resource"
    elif n.source_type in ("class_announcement", "school_announcement"):
        tab = "announcements"
        record_type = "announcement"
        if n.source_type == "school_announcement":
            record_id = "school-{}".format(n.source_id)
    elif n.source_type == "attendance":
        tab = "attendance"
        record_type = "attendance"
    elif n.notification_type == Notification.TYPE_MESSAGE:
        tab = "messages"
        record_type = "message"

    if classroom_id:
        from apps.virtual_classrooms.models import VirtualClassroomEnrollment

        if not VirtualClassroomEnrollment.objects.filter(
            virtual_classroom_id=classroom_id,
            student__user=user,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).exists():
            return ""

    params = {"notification_tab": tab}
    if classroom_id:
        params["notification_classroom"] = classroom_id
    if record_id and record_type:
        params["notification_record_type"] = record_type
        params["notification_record"] = record_id
    return "{}?{}".format(reverse("dashboards:student"), urlencode(params))


def _other_dashboard_target(n, user):
    """Keep school-wide announcement links inside each active role dashboard."""
    if not user or n.source_type != "school_announcement":
        return ""
    dashboards = (
        ("assistant_principal", "dashboards:academic_coordinator"),
        ("principal", "dashboards:principal"),
        ("elementary_principal", "dashboards:principal"),
        ("registrar", "dashboards:registrar"),
        ("system_admin", "dashboards:system_admin"),
        ("quality_audit", "dashboards:quality_audit"),
    )
    for role, route_name in dashboards:
        if user.groups.filter(name=role).exists():
            params = urlencode({
                "notification_tab": "announcements",
                "notification_record_type": "announcement",
                "notification_record": "school-{}".format(n.source_id),
                "notification_id": n.pk,
            })
            return "{}?{}".format(reverse(route_name), params)
    return ""


def notification_target(n, user=None):
    """The most specific page a notification should open."""
    # The standalone teacher dashboard is the current UI. Route teacher bell
    # clicks there and let its query handler open the selected classroom/tab.
    teacher_target = _teacher_dashboard_target(n, user)
    if teacher_target:
        return teacher_target
    student_target = _student_dashboard_target(n, user)
    if student_target:
        return student_target
    other_target = _other_dashboard_target(n, user)
    if other_target:
        return other_target
    if n.notification_type == Notification.TYPE_ASSESSMENT and n.source_type in ("assessment", "assessment_assignment") and n.source_id:
        from apps.assessments.models import Assessment, AssessmentAssignment

        if n.source_type == "assessment_assignment":
            assignment = AssessmentAssignment.objects.filter(pk=n.source_id).first()
            assessment_id = assignment.assessment_id if assignment else None
            classroom_id = assignment.virtual_classroom_id if assignment else None
            if assignment and n.virtual_classroom_id and classroom_id != n.virtual_classroom_id:
                assessment_id = None
        else:
            assessment_id = n.source_id if Assessment.objects.filter(pk=n.source_id).exists() else None
            classroom_id = n.virtual_classroom_id
            if assessment_id and classroom_id and not AssessmentAssignment.objects.filter(
                assessment_id=assessment_id, virtual_classroom_id=classroom_id
            ).exists():
                assessment_id = None
        if not assessment_id:
            return ""
        if classroom_id:
            url = _reverse_or_blank("classrooms:classroom_detail", classroom_id)
            if url:
                return "{}?tab=assessment#assessment-{}".format(url, assessment_id)
        url = _reverse_or_blank("assessments:assessment_detail", assessment_id)
        if url:
            return url
        url = _reverse_or_blank("assessments:assessment_submit", assessment_id)
        if url:
            return url
    if n.notification_type == Notification.TYPE_ASSESSMENT and n.source_type == "assessment_reminder" and n.source_id:
        from apps.assessments.models import AssessmentAssignment

        assignment = AssessmentAssignment.objects.filter(pk=n.source_id).first()
        if assignment and (
            not n.virtual_classroom_id
            or assignment.virtual_classroom_id == n.virtual_classroom_id
        ):
            url = _reverse_or_blank("classrooms:classroom_detail", assignment.virtual_classroom_id)
            if url:
                return "{}?tab=assessment#assessment-{}".format(url, assignment.assessment_id)
        return ""
    if n.source_type == "resource_placement" and n.source_id:
        from apps.teaching_learning.models import LearningResource, LearningResourcePlacement

        placement = LearningResourcePlacement.objects.filter(pk=n.source_id).select_related(
            "virtual_classroom", "resource"
        ).first()
        if placement and placement.virtual_classroom_id and placement.resource.status == LearningResource.STATUS_ACTIVE:
            url = _reverse_or_blank("teaching:class_materials", placement.virtual_classroom_id)
            if url:
                return "{}#resource-placement-{}".format(url, placement.pk)
            url = _reverse_or_blank("classrooms:classroom_detail", placement.virtual_classroom_id)
            if url:
                return "{}?tab=materials".format(url)
        return ""
    if n.notification_type == Notification.TYPE_MATERIAL and n.source_type == "resource" and n.source_id and n.virtual_classroom_id:
        from apps.teaching_learning.models import LearningResource, LearningResourcePlacement

        placement = LearningResourcePlacement.objects.filter(
            resource_id=n.source_id,
            virtual_classroom_id=n.virtual_classroom_id,
            resource__status=LearningResource.STATUS_ACTIVE,
        ).order_by("-placed_at").first()
        if placement:
            url = _reverse_or_blank("teaching:class_materials", n.virtual_classroom_id)
            if url:
                return "{}#resource-placement-{}".format(url, placement.pk)
        return ""
    if n.source_type in ("class_announcement", "school_announcement") and n.source_id:
        if n.source_type == "class_announcement" and n.virtual_classroom_id:
            base = _reverse_or_blank("classrooms:classroom_detail", n.virtual_classroom_id)
            if base:
                return "{}?tab=announcements&type=class#announcement-{}".format(base, n.source_id)
        base = _reverse_or_blank("communication:announcements")
        if base:
            kind = "school" if n.source_type == "school_announcement" else "class"
            return "{}?type={}#announcement-{}".format(base, kind, n.source_id)
        if n.source_type == "class_announcement" and n.virtual_classroom_id:
            base = _reverse_or_blank("classrooms:classroom_detail", n.virtual_classroom_id)
            if base:
                return "{}?tab=announcements#announcement-{}".format(base, n.source_id)
    if n.source_type == "attendance" and n.virtual_classroom_id:
        from apps.attendance.models import AttendanceSession

        session = AttendanceSession.objects.filter(
            pk=n.source_id, virtual_classroom_id=n.virtual_classroom_id
        ).first()
        base = _reverse_or_blank("classrooms:classroom_detail", n.virtual_classroom_id)
        if base:
            fragment = "#attendance-session-{}".format(session.pk) if session else ""
            return "{}?tab=attendance{}".format(base, fragment)
    if n.notification_type == Notification.TYPE_MESSAGE:
        if n.source_type == "private_message" and n.source_id:
            from .models import PrivateMessage

            msg = PrivateMessage.objects.filter(pk=n.source_id).first()
            if msg:
                base = _reverse_or_blank("communication:private_message_thread", msg.sender_id)
                return "{}#message-{}".format(base, msg.pk) if base else ""
        if n.source_type == "group_message" and n.source_id:
            from .models import GroupMessage

            msg = GroupMessage.objects.filter(pk=n.source_id).first()
            if msg:
                base = _reverse_or_blank("communication:group_message_thread", msg.virtual_classroom_id)
                return "{}#message-{}".format(base, msg.pk) if base else ""
        return ""
    # Fallback to stored url
    if n.url:
        return n.url
    return ""


def _bulk_create(recipients, *, virtual_classroom, notification_type, source_type, source_id, title, message, url, exclude_user_id=None):
    """Idempotently create one notification per recipient, returning the count.

    ``get_or_create`` is used per recipient rather than ``bulk_create`` because
    the unique constraint has to be honoured on a re-run; a class is small
    (tens of students), so the extra queries are cheap next to a publish action.
    """
    created = 0
    for uid in recipients:
        if exclude_user_id and uid == exclude_user_id:
            continue
        _, was_created = Notification.objects.get_or_create(
            recipient_id=uid,
            notification_type=notification_type,
            source_type=source_type,
            source_id=source_id,
            defaults={
                "virtual_classroom": virtual_classroom,
                "title": title,
                "message": message,
                "url": url,
            },
        )
        created += int(was_created)
    return created


def notify_class_announcement(announcement):
    """Bell notification for a classroom announcement.

    Recipients follow the announcement's ``target_role``: students get
    student-only and everyone notices, the classroom's teacher gets
    teacher-only and everyone notices. The author is always skipped.
    """
    from .models import ClassAnnouncement, TARGET_STUDENTS, TARGET_TEACHERS

    if announcement is None or announcement.status != ClassAnnouncement.STATUS_ACTIVE:
        return 0
    vc = announcement.virtual_classroom
    target = announcement.target_role or "all"
    recipients = []
    if target in ("all", TARGET_STUDENTS):
        recipients.extend(active_student_user_ids(vc))
    if target in ("all", TARGET_TEACHERS) and vc is not None and vc.teacher_id:
        recipients.append(vc.teacher_id)
    recipients = list(dict.fromkeys(recipients))
    if not recipients:
        return 0
    body = (announcement.content or "").strip()
    base = _reverse_or_blank("communication:announcements")
    if base:
        url = "{}?type=class#announcement-{}".format(base, announcement.pk)
    else:
        base2 = _reverse_or_blank("classrooms:classroom_detail", vc.pk)
        url = "{}?tab=announcements#announcement-{}".format(base2, announcement.pk) if base2 else ""
    return _bulk_create(
        recipients,
        virtual_classroom=vc,
        notification_type=Notification.TYPE_ANNOUNCEMENT,
        source_type="class_announcement",
        source_id=announcement.pk,
        title="New Class Announcement",
        message=(body[:140] + "…") if len(body) > 140 else body,
        url=url,
        exclude_user_id=announcement.posted_by_id,
    )


def notify_school_announcement(announcement):
    """Bell notification for a school-wide announcement.

    Recipients are every active user who may actually read the notice, using the
    same ``readable_audiences`` rule the announcement list applies. A
    whole-school notice reaches everyone; a campus-scoped one reaches only the
    users classified into that campus (its principals and the students/teachers
    whose classrooms sit in that grade band), so the bell never lights for a
    notice the recipient would then be refused on the list page.
    """
    from django.contrib.auth import get_user_model

    from .models import SchoolAnnouncement

    if announcement is None or announcement.status != SchoolAnnouncement.STATUS_ACTIVE:
        return 0
    from apps.accounts import campus as campus_helpers

    audience = announcement.audience or campus_helpers.WHOLE_SCHOOL
    User = get_user_model()
    active = list(
        User.objects.filter(is_active=True, status=User.Status.ACTIVE).values_list(
            "pk", flat=True
        )
    )
    if audience == campus_helpers.WHOLE_SCHOOL:
        recipients = active
    else:
        recipients = [
            uid
            for uid in active
            if audience in campus_helpers.readable_audiences(User.objects.get(pk=uid))
        ]
    # Role-based audience sits on top of the campus audience: a notice can be
    # campus-scoped *and* addressed to students or teachers only.
    from .models import user_matches_target_role

    target_role = announcement.target_role or "all"
    if target_role != "all":
        recipients = [
            uid
            for uid in recipients
            if user_matches_target_role(User.objects.get(pk=uid), target_role)
        ]
    if not recipients:
        return 0
    body = (announcement.content or "").strip()
    return _bulk_create(
        recipients,
        virtual_classroom=None,
        notification_type=Notification.TYPE_ANNOUNCEMENT,
        source_type="school_announcement",
        source_id=announcement.pk,
        title=(announcement.title or "School Announcement")[:200],
        message=(body[:140] + "…") if len(body) > 140 else body,
        url=_reverse_or_blank("communication:announcements"),
        exclude_user_id=announcement.posted_by_id,
    )


def notify_resource_placement(placement):
    """Bell notification for a material attached to a classroom.

    Keyed on the *placement* (not the resource): attaching the same resource to
    a second classroom is a genuinely new event for the students in that
    classroom, while editing the resource's title is not.
    """
    from apps.teaching_learning.models import LearningResource, LearningResourcePlacement

    if not isinstance(placement, LearningResourcePlacement):
        return 0
    resource = placement.resource
    if resource is None or resource.status != LearningResource.STATUS_ACTIVE:
        return 0
    vc = placement.virtual_classroom
    recipients = active_student_user_ids(vc)
    if not recipients:
        return 0
    return _bulk_create(
        recipients,
        virtual_classroom=vc,
        notification_type=Notification.TYPE_MATERIAL,
        source_type="resource_placement",
        source_id=placement.pk,
        title="New Learning Material",
        message=(resource.title or "Learning resource")[:180],
        url=_reverse_or_blank("teaching:class_materials", vc.pk),
        exclude_user_id=placement.placed_by_id,
    )


def notify_assessment_assignment(assignment):
    """Bell notification for an assessment assigned to a classroom.

    Skipped while the assessment is still in the future: a scheduled assessment
    gets its bell row from ``notify_assessment_available`` when its
    ``available_at`` passes, so the student is told when they can actually
    start, not before. If it is already available the row is created now and the
    later availability sweep is a no-op thanks to the unique key.
    """
    from apps.assessments.models import Assessment, AssessmentAssignment

    if not isinstance(assignment, AssessmentAssignment):
        return 0
    assessment = assignment.assessment
    if assessment is None or assessment.status != Assessment.STATUS_ACTIVE:
        return 0
    now = timezone.now()
    if assessment.available_at and assessment.available_at > now:
        return 0
    vc = assignment.virtual_classroom
    recipients = active_student_user_ids(vc)
    if not recipients:
        return 0
    deadline = assessment.submission_deadline
    message = assessment.title or "New assessment"
    if deadline:
        message = f"{message} · due {timezone.localtime(deadline):%b %d, %Y %I:%M %p}"
    return _bulk_create(
        recipients,
        virtual_classroom=vc,
        notification_type=Notification.TYPE_ASSESSMENT,
        source_type="assessment",
        source_id=assessment.pk,
        title="New Assessment",
        message=message[:200],
        url=_reverse_or_blank("assessments:assessment_detail", assessment.pk),
        exclude_user_id=assessment.created_by_id,
    )


def notify_assessment_available(assessment):
    """Bell notification once a scheduled assessment's window opens.

    Fans out to every classroom the assessment is assigned to and shares the
    ``("assessment", <pk>)`` key with ``notify_assessment_assignment`` so the
    two paths cannot both light the bell for the same assessment.
    """
    from apps.assessments.models import Assessment

    if not isinstance(assessment, Assessment) or assessment.status != Assessment.STATUS_ACTIVE:
        return 0
    created = 0
    for assignment in assessment.assignments.all():
        created += notify_assessment_assignment(assignment)
    return created


def sync_attendance_notification(
    *, user_id, virtual_classroom, session, is_absent, absent_label="absent"
):
    """Create or clear the bell notice for one student's attendance session.

    A student is notified once per attendance session when marked absent, and
    the notice is removed if the teacher later corrects the record to present
    or late -- so the bell never keeps a stale absence. Idempotent: re-saving
    the same session does not multiply the red dot, because the
    ``(recipient, type, source_type, source_id)`` key already holds one row.
    """
    if not user_id or session is None:
        return 0
    lookup = {
        "recipient_id": user_id,
        "notification_type": Notification.TYPE_ATTENDANCE,
        "source_type": "attendance",
        "source_id": session.pk,
    }
    if not is_absent:
        Notification.objects.filter(**lookup).delete()
        return 0
    subject = (
        virtual_classroom.subject.name
        if virtual_classroom is not None and virtual_classroom.subject_id
        else "your class"
    )
    session_date = getattr(session, "session_date", None)
    url = _reverse_or_blank("classrooms:classroom_detail", getattr(virtual_classroom, "pk", None))
    if url:
        url = "{}?tab=attendance".format(url)
    _, was_created = Notification.objects.get_or_create(
        **lookup,
        defaults={
            "virtual_classroom": virtual_classroom,
            "title": "Marked {}".format(absent_label),
            "message": "You were marked {} in {} on {}.".format(
                absent_label,
                subject,
                session_date.strftime("%b %d, %Y") if session_date else "",
            ),
            "url": url,
        },
    )
    return int(was_created)


def notify_private_message(message):
    """Persist a bell notice for the recipient of a direct message."""
    from .models import PrivateMessage

    if not isinstance(message, PrivateMessage):
        return 0
    try:
        _, created = Notification.objects.get_or_create(
            recipient_id=message.recipient_id,
            notification_type=Notification.TYPE_MESSAGE,
            source_type="private_message",
            source_id=message.pk,
            defaults={
                "title": "New Message",
                "message": (message.content or "New message")[:200],
                "url": _reverse_or_blank("communication:private_message_thread", message.sender_id),
            },
        )
        return int(created)
    except Exception:
        logger.exception("Could not persist a private-message notification")
        return 0


def notify_group_message(message):
    """Persist one class-message notice per active classmate, excluding sender."""
    from .models import GroupMessage

    if not isinstance(message, GroupMessage):
        return 0
    vc = message.virtual_classroom
    recipients = set(active_student_user_ids(vc))
    if vc.teacher_id:
        recipients.add(vc.teacher_id)
    recipients.discard(message.sender_id)
    url = _reverse_or_blank("communication:group_message_thread", vc.pk)
    created = 0
    try:
        for uid in recipients:
            _, was_created = Notification.objects.get_or_create(
                recipient_id=uid,
                notification_type=Notification.TYPE_MESSAGE,
                source_type="group_message",
                source_id=message.pk,
                defaults={
                    "virtual_classroom": vc,
                    "title": "New Class Message",
                    "message": (message.content or "New message")[:200],
                    "url": "{}#message-{}".format(url, message.pk) if url else "",
                },
            )
            created += int(was_created)
        return created
    except Exception:
        logger.exception("Could not persist a group-message notification")
        return created


def can_access(notification, user):
    """Whether ``user`` may still open ``notification``'s target.

    Notifications are only ever created for active classroom members, but a
    student can be unenrolled (or an account re-roled) before they click. When
    the notification is class-scoped this re-checks the same enrolment the
    creation code used, on the click path where the database is reachable.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if notification.recipient_id != user.pk and not user.is_superuser:
        return False
    if notification.notification_type == Notification.TYPE_ASSESSMENT and notification.source_id:
        from apps.assessments.models import Assessment, AssessmentAssignment, AssessmentProctoringEvent
        from apps.virtual_classrooms.models import VirtualClassroomEnrollment

        if notification.source_type == "assessment_proctoring_event":
            event = AssessmentProctoringEvent.objects.filter(pk=notification.source_id).first()
            if not event or not notification.virtual_classroom_id:
                return False
            return AssessmentAssignment.objects.filter(
                assessment_id=event.assessment_id,
                virtual_classroom_id=notification.virtual_classroom_id,
                virtual_classroom__teacher=user,
                virtual_classroom__status="active",
                virtual_classroom__enrollments__student_id=event.student_id,
                virtual_classroom__enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            ).exists()
        if notification.source_type == "assessment":
            if not Assessment.objects.filter(pk=notification.source_id).exists():
                return False
            if notification.virtual_classroom_id and not AssessmentAssignment.objects.filter(
                assessment_id=notification.source_id,
                virtual_classroom_id=notification.virtual_classroom_id,
            ).exists():
                return False
        elif notification.source_type in ("assessment_assignment", "assessment_reminder"):
            assignment = AssessmentAssignment.objects.filter(pk=notification.source_id).first()
            if not assignment or (
                notification.virtual_classroom_id
                and assignment.virtual_classroom_id != notification.virtual_classroom_id
            ):
                return False
    if notification.notification_type == Notification.TYPE_MATERIAL and notification.source_type == "resource_placement" and notification.source_id:
        from apps.teaching_learning.models import LearningResourcePlacement

        if not LearningResourcePlacement.objects.filter(
            pk=notification.source_id,
            virtual_classroom_id=notification.virtual_classroom_id,
        ).exists():
            return False
    if notification.notification_type == Notification.TYPE_ATTENDANCE and notification.source_id:
        from apps.attendance.models import AttendanceSession

        if not AttendanceSession.objects.filter(
            pk=notification.source_id,
            virtual_classroom_id=notification.virtual_classroom_id,
        ).exists():
            return False
    # Re-check the role audience at click time, so a notification generated
    # before an account changed role cannot still open a notice it may no
    # longer read.
    if notification.source_type in ("school_announcement", "class_announcement") and notification.source_id:
        from .models import ClassAnnouncement, SchoolAnnouncement, user_matches_target_role

        model = (
            SchoolAnnouncement
            if notification.source_type == "school_announcement"
            else ClassAnnouncement
        )
        source = model.objects.filter(pk=notification.source_id).first()
        if source is None or getattr(source, "status", "active") != "active":
            return False
        if not user_matches_target_role(
            user, getattr(source, "target_role", "all")
        ):
            return False
        if notification.source_type == "class_announcement" and (
            source.virtual_classroom_id != notification.virtual_classroom_id
        ):
            return False
        if notification.source_type == "school_announcement":
            from apps.accounts import campus as campus_helpers

            if (source.audience or campus_helpers.WHOLE_SCHOOL) not in campus_helpers.readable_audiences(user):
                return False
    if notification.notification_type == Notification.TYPE_MESSAGE:
        if user.is_superuser:
            return True
        if notification.source_type == "private_message" and notification.source_id:
            from .models import PrivateMessage
            from .access import can_message

            msg = PrivateMessage.objects.filter(pk=notification.source_id).first()
            return bool(msg and user.pk == msg.recipient_id and can_message(user, msg.sender))
        if notification.source_type == "group_message" and notification.source_id:
            from .models import GroupMessage

            msg = GroupMessage.objects.filter(pk=notification.source_id).first()
            if not msg:
                return False
            vc = msg.virtual_classroom
            if vc.teacher_id == user.pk:
                return True
            return VirtualClassroomEnrollment.objects.filter(
                virtual_classroom=vc,
                student__user=user,
                status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            ).exists()
        return False
    vc = notification.virtual_classroom
    if vc is None:
        return True
    if user.is_superuser:
        return True
    if vc.teacher_id == user.pk:
        return True
    return VirtualClassroomEnrollment.objects.filter(
        virtual_classroom=vc,
        student__user=user,
        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).exists()

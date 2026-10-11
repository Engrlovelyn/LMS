"""Helpers that bring a newly created Virtual Classroom up to spec:
- pre-populate its Topic outline from the subject's TopicTemplate(s)
- auto-enroll the section's enrolled students for core (applied) subjects
"""

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone

from apps.academic_structure.models import StudentEnrollment, Subject

from .models import VirtualClassroom, VirtualClassroomEnrollment


def assignable_teachers():
    """Every active account that may hold a Virtual Classroom.

    Matches how the rest of the app populates teacher pickers (the "teacher"
    group). Superuser accounts are deliberately excluded: they are admin logins
    such as the school owner or a developer account, not classroom teachers.
    """
    return get_user_model().objects.filter(groups__name="teacher", is_active=True)


def teacher_conflict_in_section(section, teacher, exclude_classroom=None):
    """The Virtual Classroom that already holds ``teacher`` in ``section``.

    One teacher owns exactly one classroom per section, so a student never sees
    the same teacher twice in their list of virtual classrooms. Returns None
    when the teacher is free (or when only ``exclude_classroom`` holds them).
    """
    if not section or not teacher:
        return None
    clashes = VirtualClassroom.objects.filter(section=section, teacher=teacher)
    if exclude_classroom is not None and exclude_classroom.pk:
        clashes = clashes.exclude(pk=exclude_classroom.pk)
    return clashes.order_by("id").first()


def pick_section_teacher(section, preferred=None, exclude_classroom=None):
    """Choose a teacher for a new classroom in ``section``.

    ``preferred`` (usually the section adviser or the teacher carried over from
    a prior year) wins whenever they are not already teaching another classroom
    in that section. Otherwise the least-loaded available teacher is used, so
    spread stays even as classrooms are created. Returns None only when there
    are no assignable teachers at all.
    """
    if preferred and not teacher_conflict_in_section(section, preferred, exclude_classroom):
        return preferred

    taken = set(
        VirtualClassroom.objects.filter(section=section)
        .exclude(pk=exclude_classroom.pk if exclude_classroom is not None and exclude_classroom.pk else None)
        .exclude(teacher__isnull=True)
        .values_list("teacher_id", flat=True)
    )
    ranked = (
        assignable_teachers()
        .annotate(room_count=Count("teaching_classrooms", distinct=True))
        .order_by("room_count", "id")
    )
    for candidate in ranked:
        if candidate.pk not in taken:
            return candidate
    return None


def sync_section_vc_enrollments(student, year, remove_other_sections=True):
    """Ensure a student is enrolled ONLY in the active virtual classrooms of
    their current section for the given academic year.

    Creates missing enrollments for the section's VCs and, when
    ``remove_other_sections`` is True, removes active enrollments in VCs that
    belong to any other section (e.g. a different grade). Those stale rows are
    leftovers from a previous section assignment and are what caused students
    to see virtual classrooms from other grade levels.
    """
    se = (
        StudentEnrollment.objects.filter(
            student=student,
            academic_year=year,
            status=StudentEnrollment.STATUS_ENROLLED,
        )
        .order_by("-id")
        .first()
    )
    if se is None or se.section is None:
        return

    expected = VirtualClassroom.objects.filter(
        section=se.section,
        academic_year=year,
        status=VirtualClassroom.STATUS_ACTIVE,
    )
    expected_ids = set(expected.values_list("id", flat=True))

    active = VirtualClassroomEnrollment.objects.filter(
        student=student, status=VirtualClassroomEnrollment.STATUS_ACTIVE
    ).select_related("virtual_classroom")
    active_by_id = {}
    for e in active:
        active_by_id[e.virtual_classroom_id] = e
        if (
            remove_other_sections
            and e.virtual_classroom.academic_year_id == year.pk
            and e.virtual_classroom_id not in expected_ids
        ):
            e.status = VirtualClassroomEnrollment.STATUS_REMOVED
            e.removed_at = timezone.now()
            e.save(update_fields=["status", "removed_at"])

    existing_ids = {
        vc_id
        for vc_id, e in active_by_id.items()
        if e.status == VirtualClassroomEnrollment.STATUS_ACTIVE
    }
    new_ids = expected_ids - existing_ids
    if new_ids:
        VirtualClassroomEnrollment.objects.bulk_create(
            [
                VirtualClassroomEnrollment(virtual_classroom_id=vc_id, student=student)
                for vc_id in new_ids
            ]
        )


def populate_classroom_defaults(classroom, auto_enroll=True):
    """Populate topics and (optionally) auto-enroll students for a new VC.

    Both operations are idempotent — safe to call on an existing classroom.
    """
    from apps.teaching_learning.models import Topic

    if not classroom.topics.exists():
        templates = list(classroom.subject.topic_templates.all())
        if templates:
            Topic.objects.bulk_create(
                [
                    Topic(
                        virtual_classroom=classroom,
                        name=template.name,
                        order=template.order,
                    )
                    for template in templates
                ]
            )

    if auto_enroll and classroom.subject.subject_type == Subject.TYPE_CORE:
        enrolled_ids = set(
            StudentEnrollment.objects.filter(
                section=classroom.section,
                academic_year=classroom.academic_year,
                status=StudentEnrollment.STATUS_ENROLLED,
            ).values_list("student_id", flat=True)
        )
        existing_ids = set(
            VirtualClassroomEnrollment.objects.filter(
                virtual_classroom=classroom
            ).values_list("student_id", flat=True)
        )
        new_ids = enrolled_ids - existing_ids
        if new_ids:
            VirtualClassroomEnrollment.objects.bulk_create(
                [
                    VirtualClassroomEnrollment(
                        virtual_classroom=classroom, student_id=student_id
                    )
                    for student_id in new_ids
                ]
            )
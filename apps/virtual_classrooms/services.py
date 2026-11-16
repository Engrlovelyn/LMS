"""Helpers that bring a newly created Virtual Classroom up to spec:
- pre-populate its Topic outline from the subject's TopicTemplate(s)
- auto-enroll the section's enrolled students for core (applied) subjects
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.utils import timezone

from apps.academic_structure.models import Section, StudentEnrollment, Subject

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


_GRADING_ORDINALS = {
    "1": "1ST",
    "1st": "1ST",
    "first": "1ST",
    "2": "2ND",
    "2nd": "2ND",
    "second": "2ND",
    "3": "3RD",
    "3rd": "3RD",
    "third": "3RD",
    "4": "4TH",
    "4th": "4TH",
    "fourth": "4TH",
}
_GRADING_TYPES = {
    "quarter": "QUARTER",
    "quarterly": "QUARTER",
    "semester": "SEMESTER",
    "semestral": "SEMESTER",
    "trimester": "TRIMESTER",
}
# A grading *format* (the school year's structure) is not a grading *period*.
# The dashboards' older selects only know the format ("Quarter", "Trimester"),
# and sending that back as a period used to create a second classroom beside
# the existing year-long one. Those generic words therefore mean "no specific
# period" and normalise to an empty string.
_GENERIC_GRADING = {
    "quarter",
    "quarterly",
    "semester",
    "semestral",
    "trimester",
    "whole academic year",
    "whole year",
    "year long",
    "year-long",
    "n/a",
    "na",
    "none",
}


def normalize_grading_period(value):
    """Return a canonical grading-period code for ``value``.

    Accepts every shape the dashboards and imports send -- ``1ST_TRIMESTER``,
    ``1st Trimester``, ``Trimester 1``, ``First Trimester`` -- and collapses
    the generic format names (``Quarter``/``Semester``/``Trimester``) to an
    empty period. That keeps the ``unique_vc_per_subject_section_year_period``
    constraint meaningful and stops the registrar's localStorage bridge from
    duplicating every year-long classroom on each save.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    key = " ".join(text.lower().replace("_", " ").replace("-", " ").split())
    if key in _GENERIC_GRADING:
        return ""
    ordinal = None
    period_type = None
    for token in key.split():
        ordinal = _GRADING_ORDINALS.get(token, ordinal)
        period_type = _GRADING_TYPES.get(token, period_type)
    if ordinal and period_type:
        return "{}_{}".format(ordinal, period_type)
    return text


def _coerce_grade(value):
    """Return a numeric grade from ``7``, ``"7"`` or ``"Grade 7"``."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        return int(digits) if digits else None


def resolve_section(academic_year, section_id=None, name="", grade=None):
    """Find a Section in ``academic_year`` by id, then by name (+grade)."""
    qs = Section.objects.filter(academic_year=academic_year)
    if section_id not in (None, ""):
        try:
            section = qs.filter(pk=int(section_id)).first()
        except (TypeError, ValueError):
            section = None
        if section is not None:
            return section
    name = str(name or "").strip()
    if not name:
        return None
    qs = qs.filter(name=name)
    grade = _coerce_grade(grade)
    if grade is not None:
        qs = qs.filter(grade_level=grade)
    return qs.order_by("-grade_level").first()


def resolve_subject(academic_year, subject_id=None, name="", grade=None):
    """Find a Subject in ``academic_year`` by id, then by name (+grade)."""
    qs = Subject.objects.filter(academic_year=academic_year)
    if subject_id not in (None, ""):
        try:
            subject = qs.filter(pk=int(subject_id)).first()
        except (TypeError, ValueError):
            subject = None
        if subject is not None:
            return subject
    name = str(name or "").strip()
    if not name:
        return None
    qs = qs.filter(name=name)
    grade = _coerce_grade(grade)
    if grade is not None:
        qs = qs.filter(grade_level=grade)
    return qs.order_by("-grade_level").first()


def create_virtual_classroom(
    *,
    academic_year,
    section,
    subject,
    teacher=None,
    grading_period="",
    schedule="",
    theme_color=None,
    auto_enroll=True,
):
    """Validate and persist one Virtual Classroom.

    Returns ``(classroom, created)``. Raises ``ValidationError`` when a
    required field is missing or the section/subject/teacher do not fit. The
    create is idempotent on (subject, section, year, period): posting the same
    classroom twice updates it instead of failing on the unique constraint or
    creating a duplicate.
    """
    if academic_year is None:
        raise ValidationError("Select an academic year.")
    if section is None:
        raise ValidationError("Select a section for the virtual classroom.")
    if subject is None:
        raise ValidationError("Select a subject for the virtual classroom.")
    if section.academic_year_id != academic_year.pk:
        raise ValidationError("The section does not belong to the selected academic year.")
    if subject.academic_year_id != academic_year.pk:
        raise ValidationError("The subject does not belong to the selected academic year.")
    if section.grade_level != subject.grade_level:
        raise ValidationError(
            "The subject's grade level must match the section's grade level."
        )

    period = normalize_grading_period(grading_period)
    existing = VirtualClassroom.objects.filter(
        subject=subject,
        section=section,
        academic_year=academic_year,
        grading_period=period,
    ).first()

    if teacher is not None:
        clash = teacher_conflict_in_section(
            section, teacher, exclude_classroom=existing
        )
        if clash is not None:
            raise ValidationError(
                "{} already teaches {} in {}. Pick a different teacher so each "
                "classroom has its own.".format(
                    teacher.get_full_name() or teacher.email,
                    clash.subject,
                    section,
                )
            )
    elif existing is not None and existing.teacher_id:
        # Re-posting an existing classroom without a teacher must not reassign
        # it; keep whoever is already teaching it.
        teacher = existing.teacher
    else:
        teacher = pick_section_teacher(
            section,
            preferred=getattr(section, "adviser", None),
            exclude_classroom=existing,
        )

    created = existing is None
    if created:
        classroom = VirtualClassroom.objects.create(
            subject=subject,
            section=section,
            academic_year=academic_year,
            grading_period=period,
            teacher=teacher,
            schedule=str(schedule or "").strip(),
            theme_color=theme_color or "#4F46E5",
        )
    else:
        classroom = existing
        if teacher is not None:
            classroom.teacher = teacher
        if theme_color:
            classroom.theme_color = theme_color
        if schedule:
            classroom.schedule = str(schedule).strip()
        classroom.save()

    populate_classroom_defaults(classroom, auto_enroll=auto_enroll)
    return classroom, created


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
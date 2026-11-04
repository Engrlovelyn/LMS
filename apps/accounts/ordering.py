"""Shared student ordering rule for the whole LMS."""

from django.db.models import Case, F, IntegerField, OuterRef, Subquery, Value, When

MALE = "M"
FEMALE = "F"


def _grade_expression(queryset, prefix):
    """Return the grade-level expression for the rows in ``queryset``.

    A learner no longer stores their section directly on ``StudentProfile``;
    the section lives on ``StudentEnrollment``. Rows that are enrollments
    themselves (``StudentEnrollment`` or ``VirtualClassroomEnrollment``) carry
    the section for that row, which is what historical rosters must sort by.
    Plain ``StudentProfile`` rows fall back to the learner's current enrolled
    section through a subquery so the annotation cannot multiply the rows.
    """
    model_fields = {field.name for field in queryset.model._meta.get_fields()}
    if prefix and "section" in model_fields:
        return F("section__grade_level")
    if prefix and "virtual_classroom" in model_fields:
        return F("virtual_classroom__section__grade_level")

    from apps.academic_structure.models import StudentEnrollment

    student_ref = "pk" if not prefix else "{}pk".format(prefix)
    current_grade = (
        StudentEnrollment.objects.filter(
            student_id=OuterRef(student_ref),
            status=StudentEnrollment.STATUS_ENROLLED,
        )
        .order_by("-academic_year__start_date")
        .values("section__grade_level")[:1]
    )
    return Subquery(current_grade, output_field=IntegerField())


def order_students(queryset, *, prefix="", leading_order=()):
    """Order students by grade level ascending, then surname A-Z."""
    sex_field = "{}sex".format(prefix)
    last_field = "{}last_name".format(prefix)
    first_field = "{}first_name".format(prefix)

    return queryset.annotate(
        _grade_sort=_grade_expression(queryset, prefix),
        _sex_rank=Case(
            When(**{sex_field: MALE}, then=Value(0)),
            When(**{sex_field: FEMALE}, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        )
    ).order_by(
        *leading_order,
        F("_grade_sort").asc(nulls_last=True),
        "_sex_rank",
        F(last_field).asc(nulls_last=True),
        F(first_field).asc(nulls_last=True),
        "pk",
    )


def order_teachers(queryset, *, profile_prefix="employee_profile__"):
    """Order staff rows by employee surname, then first name and account name."""
    last_name = "{}last_name".format(profile_prefix)
    first_name = "{}first_name".format(profile_prefix)
    return queryset.order_by(
        F(last_name).asc(nulls_last=True),
        F(first_name).asc(nulls_last=True),
        "email",
        "pk",
    )

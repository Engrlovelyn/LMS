"""Shared student ordering rule for the whole LMS.

Presentation feedback asked for one consistent learner order everywhere:

1. Male students first, then female.
2. Within each group, by last name A-Z.
3. Same last name: by first name A-Z.
4. Anything else (blank/missing sex) sorts after the two known groups; missing
   names sort last via ``NULLS LAST``.

The rule is applied at the queryset level so pagination, search and filtering
all operate on the same order. ``StudentProfile.sex`` stores ``M``/``F`` (see
``accounts.models.SexChoices``); the school's real records are never rewritten
to force an order.
"""

from django.db.models import Case, F, IntegerField, Value, When

MALE = "M"
FEMALE = "F"


def order_students(queryset, *, prefix="", leading_order=()):
    """Return ``queryset`` ordered by the school's learner-ordering rule.

    ``prefix`` lets the same rule run across a relation, e.g.
    ``order_students(enrollments, prefix="student__")`` for
    ``VirtualClassroomEnrollment`` rows.

    ``leading_order`` is applied before the sex/name rule, for the per-year
    rosters that must stay grouped by school year, grade level and section and
    only fall back to male-first within each group.
    """
    sex_field = "{}sex".format(prefix)
    last_field = "{}last_name".format(prefix)
    first_field = "{}first_name".format(prefix)

    return queryset.annotate(
        _sex_rank=Case(
            When(**{sex_field: MALE}, then=Value(0)),
            When(**{sex_field: FEMALE}, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        )
    ).order_by(
        *leading_order,
        "_sex_rank",
        F(last_field).asc(nulls_last=True),
        F(first_field).asc(nulls_last=True),
        "pk",
    )

"""Campus scope: which half of the school a principal's dashboard represents.

The school runs a Junior/High School campus and an Elementary campus. Both are
served by the same dashboards and the same templates -- what differs is the data
each principal is allowed to see. The Elementary Principal must not be able to
read high-school grade books or post an announcement the high-school side then
reads, and vice versa.

Scope is derived from the role codes a user holds rather than stored on the
user, so it cannot drift out of sync with the role assignment:

===========================  =========================================
Role code                    Campus scope
===========================  =========================================
``elementary_principal``     Elementary
``principal``                High School
``assistant_principal``      High School (unless also Elementary)
``system_admin``             Whole School
everything else              Whole School
===========================  =========================================

Because scope is per-role rather than per-user, one person can legitimately
hold both principal roles and see both campuses; the grade-band predicates below
union the bands rather than picking one.

Grade bands follow the usual Philippine K-12 split: Elementary is Grades 1-6,
Junior High School is Grades 7-10 and Senior High School is Grades 11-12, both
of which sit inside the High School campus here.
"""

from apps.academic_structure.models import Section, Subject

ELEMENTARY = "elementary"
HIGH_SCHOOL = "high_school"
WHOLE_SCHOOL = "whole_school"

CAMPUS_SCOPES = (
    (ELEMENTARY, "Elementary"),
    (HIGH_SCHOOL, "High School"),
    (WHOLE_SCHOOL, "Whole School"),
)

CAMPUS_LABELS = {
    ELEMENTARY: "Elementary",
    HIGH_SCHOOL: "High School",
    WHOLE_SCHOOL: "Whole School",
}

ELEMENTARY_GRADE_MIN = 1
ELEMENTARY_GRADE_MAX = 6
HIGH_SCHOOL_GRADE_MIN = 7
HIGH_SCHOOL_GRADE_MAX = 12

# Which campus each role code speaks for. Roles absent from this map are not
# campus-bound and see everything.
ROLE_CAMPUS = {
    "elementary_principal": ELEMENTARY,
    "principal": HIGH_SCHOOL,
    # An Assistant Principal is scoped by whichever principal role they sit
    # under; resolved in `campus_scopes_for` rather than fixed here.
    "assistant_principal": HIGH_SCHOOL,
}


def grade_bands_for(scopes):
    """Return the (min, max) grade pairs a set of scopes may read.

    ``WHOLE_SCHOOL`` collapses to a single full 1-12 band so every downstream
    query can stay a simple ``grade_level__range``, with no special case.
    """
    scopes = set(scopes or ())
    if not scopes or WHOLE_SCHOOL in scopes:
        return [(ELEMENTARY_GRADE_MIN, HIGH_SCHOOL_GRADE_MAX)]

    bands = []
    if ELEMENTARY in scopes:
        bands.append((ELEMENTARY_GRADE_MIN, ELEMENTARY_GRADE_MAX))
    if HIGH_SCHOOL in scopes:
        bands.append((HIGH_SCHOOL_GRADE_MIN, HIGH_SCHOOL_GRADE_MAX))
    return bands or [(ELEMENTARY_GRADE_MIN, HIGH_SCHOOL_GRADE_MAX)]


def role_codes_for(user):
    """Active-year role codes for a user, or [] for an anonymous one."""
    if not user or not getattr(user, "is_authenticated", False):
        return []
    return list(
        user.role_assignments.filter(academic_year__status="ACTIVE")
        .values_list("role__code", flat=True)
        .distinct()
    )


def campus_scopes_for(user):
    """Every campus this user may read, as a set of scope strings.

    A user holding both principal roles gets both campuses. A user holding no
    campus-bound role (a teacher, a registrar, Quality Assurance, or anyone with
    no role assignment at all) reads the whole school -- scoping must never
    hide data from someone who was never restricted.
    """
    codes = role_codes_for(user)
    scopes = {ROLE_CAMPUS[c] for c in codes if c in ROLE_CAMPUS}
    return scopes or {WHOLE_SCHOOL}


def campus_scope_for(user):
    """The single campus label to show on a dashboard header.

    When a user spans both campuses this returns "Whole School", because a
    header reading "Elementary" would understate what they can actually open.
    """
    scopes = campus_scopes_for(user)
    if len(scopes) > 1:
        return WHOLE_SCHOOL
    return next(iter(scopes))


def campus_label_for(user):
    return CAMPUS_LABELS.get(campus_scope_for(user), CAMPUS_LABELS[WHOLE_SCHOOL])


def can_post_to_campus(user, campus):
    """May this user post an announcement scoped to ``campus``?"""
    if campus in (None, "", WHOLE_SCHOOL):
        # A whole-school post is the one both principals are allowed to make.
        return True
    return campus in campus_scopes_for(user)


def visible_campuses(user):
    """The campuses a principal may currently post to, as a list of codes.

    A single-campus principal still sees ``whole_school``: `can_post_to_campus`
    (and every server-side gate) allows both principals to post a whole-school
    notice, so the picker must offer it rather than hide it.
    """
    scopes = campus_scopes_for(user)
    if WHOLE_SCHOOL in scopes or len(scopes) > 1:
        return [ELEMENTARY, HIGH_SCHOOL, WHOLE_SCHOOL]
    ordered = sorted(scopes)
    if WHOLE_SCHOOL not in ordered:
        ordered.append(WHOLE_SCHOOL)
    return ordered


def _band_filter(queryset, field="grade_level", scopes=None):
    """Apply the caller's grade bands to a Section/Subject queryset."""
    bands = grade_bands_for(scopes)
    if len(bands) == 1:
        low, high = bands[0]
        return queryset.filter(**{"{}__range".format(field): (low, high)})
    from django.db.models import Q

    clause = Q()
    for low, high in bands:
        clause |= Q(**{"{}__range".format(field): (low, high)})
    return queryset.filter(clause)


def sections_for_campus(queryset, scopes):
    """Narrow a Section queryset to the grade bands the caller may read."""
    return _band_filter(queryset, "grade_level", scopes)


def enrollments_for_campus(queryset, scopes):
    """Narrow a StudentEnrollment queryset to the caller's grade bands.

    A StudentEnrollment has no grade level of its own -- the grade belongs to
    the section it enrolls the student into, so the band has to be reached
    through ``section__grade_level``. Filtering on a bare ``grade_level``
    raises FieldError, which is why this helper exists separately from
    ``sections_for_campus`` rather than being a caller's mistake to repeat.
    """
    return _band_filter(queryset, "section__grade_level", scopes)


def subjects_for_campus(queryset, scopes):
    """Narrow a Subject queryset to the grade bands the caller may read."""
    return _band_filter(queryset, "grade_level", scopes)


def assessments_for_campus(queryset, scopes):
    """Narrow an Assessment queryset through its Subject's grade level.

    An Assessment carries no grade of its own -- it belongs to a Subject,
    which does -- so the band is reached through ``subject__grade_level``.
    Going through ``assessment_assignment`` instead would silently drop every
    assessment that has not been assigned to a classroom yet, which is the
    one an author is most likely to be looking for while creating it.
    """
    return _band_filter(queryset, "subject__grade_level", scopes)


def virtual_classrooms_for_campus(queryset, scopes):
    """Narrow a VirtualClassroom queryset by its section's grade band.

    A VirtualClassroom has no grade level of its own -- it inherits one through
    ``section``. Filtering on ``section__grade_level`` keeps the classroom tied
    to the band of the section it belongs to, which is what an administrator
    means when they say a classroom is a high-school classroom.
    """
    bands = grade_bands_for(scopes)
    if len(bands) == 1:
        low, high = bands[0]
        return queryset.filter(section__grade_level__range=(low, high))
    from django.db.models import Q

    clause = Q()
    for low, high in bands:
        clause |= Q(section__grade_level__range=(low, high))
    return queryset.filter(clause)


def scoped_classrooms(queryset, user):
    """Narrow a VirtualClassroom queryset to the caller's campus.

    Every module that hands an administrator "all classrooms" -- attendance,
    assessments, analytics, communication -- needs the same campus cut, and
    each of them also serves whole-school readers (system admin, registrar,
    Quality Assurance, a teacher with no campus-bound role).
    ``campus_scopes_for`` already answers "whole school" for those, so this
    helper only narrows when the caller is actually campus-bound.

    Without it a principal added to an ``ADMIN_ROLES`` tuple would pass the
    role gate and then be handed the other campus's grade books.
    """
    scopes = campus_scopes_for(user)
    if WHOLE_SCHOOL in scopes:
        return queryset
    return virtual_classrooms_for_campus(queryset, scopes)


def user_can_read_grade(user, grade_level):
    """True when this user may open a record belonging to ``grade_level``.

    The queryset helpers above can only narrow a query they were given; the
    single-object permission checks (may I open this classroom's attendance
    sheet?) need the same answer for one already-loaded row.
    """
    scopes = campus_scopes_for(user)
    if WHOLE_SCHOOL in scopes:
        return True
    return any(low <= grade_level <= high for low, high in grade_bands_for(scopes))


def class_announcements_for_campus(queryset, scopes):
    """Narrow a ClassAnnouncement queryset to the caller's grade bands.

    A ClassAnnouncement hangs off a VirtualClassroom, and the classroom hangs
    off a Section, so the grade band is two joins away rather than one. Passing
    a ClassAnnouncement queryset to ``virtual_classrooms_for_campus`` looks
    right and raises FieldError, because it filters ``section__grade_level``
    while the announcement has no ``section`` of its own.
    """
    return _band_filter(queryset, "virtual_classroom__section__grade_level", scopes)


def active_sections(scopes, academic_year_id=None):
    qs = Section.objects.filter(status=Section.STATUS_ACTIVE)
    if academic_year_id:
        qs = qs.filter(academic_year_id=academic_year_id)
    return sections_for_campus(qs, scopes)


def active_subjects(scopes, academic_year_id=None):
    qs = Subject.objects.filter(status=Subject.STATUS_ACTIVE)
    if academic_year_id:
        qs = qs.filter(academic_year_id=academic_year_id)
    return subjects_for_campus(qs, scopes)


def readable_audiences(user):
    """The announcement audiences (``whole_school``, ``elementary``,
    ``high_school``) this user may read.

    A school-wide notice is always readable, so ``whole_school`` is always in
    the returned set.

    Campus-bound roles (both principals, the Assistant Principal) read their
    own campus plus whole school. Everyone else -- teachers, students,
    staff -- is classified by the grade bands they actually touch through
    their virtual classrooms, so an Elementary Principal's announcement does
    not surface on a Grade 11 teacher's dashboard, and a High School
    announcement stays off an elementary student's feed.

    A user with classrooms on both campuses (or, at the other extreme, with no
    classrooms at all -- a newly created teacher, a registrar) reads the whole
    school: the filter never hides data from someone it has no evidence to
    restrict.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return {WHOLE_SCHOOL}

    campus_codes = {
        ROLE_CAMPUS[code]
        for code in role_codes_for(user)
        if code in ROLE_CAMPUS
    }
    if campus_codes:
        return {WHOLE_SCHOOL} | campus_codes

    # Not a campus-bound role: infer the campus from the classrooms. A role
    # code like "teacher" or "student" carries no campus, so there is nothing
    # else to go on.
    from apps.virtual_classrooms.models import (
        VirtualClassroom,
        VirtualClassroomEnrollment,
    )

    vcs = VirtualClassroom.objects.filter(status=VirtualClassroom.STATUS_ACTIVE)
    if user.groups.filter(name="teacher").exists():
        vcs = vcs.filter(teacher=user)
    elif hasattr(user, "student_profile"):
        vcs = vcs.filter(
            enrollments__student=user.student_profile,
            enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        )
    else:
        return {WHOLE_SCHOOL}

    grades = list(vcs.values_list("section__grade_level", flat=True).distinct())
    if not grades:
        return {WHOLE_SCHOOL}

    sees_elementary = any(ELEMENTARY_GRADE_MIN <= g <= ELEMENTARY_GRADE_MAX for g in grades)
    sees_high_school = any(HIGH_SCHOOL_GRADE_MIN <= g <= HIGH_SCHOOL_GRADE_MAX for g in grades)
    if sees_elementary and sees_high_school:
        return {WHOLE_SCHOOL}
    return {WHOLE_SCHOOL} | ({ELEMENTARY} if sees_elementary else {HIGH_SCHOOL})


def filter_school_announcements(queryset, user):
    """Narrow a SchoolAnnouncement queryset to what ``user`` may read.

    The audience is decided at post time and stored on the row, so a later
    role change never retroactively hides or exposes a notice -- ``posted_by``
    deliberately does not drive this filter.
    """
    audiences = readable_audiences(user)
    # Legacy rows predating the audience column may carry the empty string;
    # those pre-date the campus split and must stay readable by everyone.
    return queryset.filter(audience__in=audiences | {""})
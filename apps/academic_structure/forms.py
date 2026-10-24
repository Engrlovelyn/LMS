from django import forms
from django.db.models import Q

from apps.accounts.models import StudentProfile, User
from apps.accounts.ordering import order_students

from .models import (
    AcademicStructureVersion,
    AcademicYear,
    Section,
    StudentEnrollment,
    Subject,
)

WIDGET_TYPES = (
    forms.TextInput,
    forms.Select,
    forms.Textarea,
    forms.NumberInput,
    forms.EmailInput,
    forms.DateInput,
)


def apply_form_control(fields):
    for field in fields.values():
        if isinstance(field.widget, WIDGET_TYPES):
            field.widget.attrs["class"] = "form-control"


class AcademicYearForm(forms.ModelForm):
    class Meta:
        model = AcademicYear
        fields = ["label", "start_date", "end_date", "status"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)


class SectionForm(forms.ModelForm):
    class Meta:
        model = Section
        fields = ["academic_year", "name", "grade_level", "adviser", "status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)
        self.fields["academic_year"].queryset = AcademicYear.objects.filter(
            status=AcademicYear.STATUS_ACTIVE
        )
        self.fields["adviser"].queryset = User.objects.filter(status=User.Status.ACTIVE).order_by("email")


class SubjectForm(forms.ModelForm):
    class Meta:
        model = Subject
        fields = ["academic_year", "name", "grade_level", "subject_type", "grading_period", "status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)
        self.fields["academic_year"].queryset = AcademicYear.objects.filter(
            status=AcademicYear.STATUS_ACTIVE
        )


class StudentEnrollmentUpdateForm(forms.ModelForm):
    """Edit an existing student enrollment: move the learner to another section
    in the same academic year, or archive the enrollment."""

    class Meta:
        model = StudentEnrollment
        fields = ["section", "status"]

    def __init__(self, *args, enrollment=None, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)
        self.enrollment = enrollment
        if enrollment is not None:
            self.fields["section"].queryset = Section.objects.filter(
                academic_year=enrollment.academic_year,
                status=Section.STATUS_ACTIVE,
            )


class StructureFinalizeForm(forms.ModelForm):
    class Meta:
        model = AcademicStructureVersion
        fields = ["grading_period_type", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)


class RolloverForm(forms.Form):
    target_academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE),
        empty_label="Select target academic year...",
        help_text=(
            "Sections, subjects and virtual classrooms are copied from the source "
            "year into this year. Items that already exist there are skipped."
        ),
    )

    def __init__(self, *args, source_year=None, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)
        if source_year is not None:
            # The target is always the active year (the institution runs one
            # academic year at a time), so the source is normally the archived
            # previous year and is excluded from the choices.
            self.fields["target_academic_year"].queryset = AcademicYear.objects.filter(
                status=AcademicYear.STATUS_ACTIVE
            ).exclude(pk=source_year.pk)


class PromoteForm(forms.Form):
    source_section = forms.ModelChoiceField(
        queryset=Section.objects.filter(status=Section.STATUS_ACTIVE),
        empty_label="Select source section...",
    )
    target_section = forms.ModelChoiceField(
        queryset=Section.objects.filter(status=Section.STATUS_ACTIVE),
        empty_label="Select target section...",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)


def assignable_students(section):
    """Students that may still be placed in ``section`` for its year.

    A learner holds only one active enrollment per academic year (see the
    ``one_active_enrollment_per_student_year`` constraint), so anyone already
    enrolled in *some other* section this year is deliberately left out: they
    are already placed, and silently pulling them across would move them out of
    a class their teacher has already been told about. Transferring a whole
    roster (see ``SectionStudentMoveForm``) or Promote Students is the explicit
    route for a move. That leaves the un-enrolled learners plus the ones already
    standing in this section.
    """
    year_pk = section.academic_year_id
    elsewhere = StudentEnrollment.objects.filter(
        academic_year_id=year_pk, status=StudentEnrollment.STATUS_ENROLLED
    ).exclude(section=section).values_list("student_id", flat=True)
    return order_students(
        StudentProfile.objects.exclude(id__in=elsewhere).select_related("user")
    )


class SectionStudentAssignForm(forms.Form):
    """Add learners to a section one checkbox at a time."""

    students = forms.ModelMultipleChoiceField(
        queryset=StudentProfile.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Students",
    )

    def __init__(self, *args, section=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.section = section
        if section is not None:
            self.fields["students"].queryset = assignable_students(section)
        self.fields["students"].label = False


def copyable_source_sections(section):
    """Sections whose roster may be copied into ``section``.

    This covers both directions the registrar actually needs:

    * **previous years** -- a section being rebuilt for a new school year is
      seeded from the same section last year, which is a promotion: each learner
      gets a fresh enrollment in the *current* year.
    * **sister sections this year** -- the two halves of a year level were
      balanced out, so learners are transferred across.

    A source from an earlier year must be matched on grade level; two sections
    named alike in different grade levels are different cohorts.
    """
    return (
        Section.objects.filter(
            Q(academic_year_id=section.academic_year_id)
            | Q(academic_year__status=AcademicYear.STATUS_ARCHIVED)
        )
        .exclude(pk=section.pk)
        .filter(status=Section.STATUS_ACTIVE)
        .filter(grade_level=section.grade_level)
        .select_related("academic_year")
        .order_by("-academic_year__start_date", "name")
        .distinct()
    )


class SectionStudentMoveForm(forms.Form):
    """Copy a whole roster into a section, from this year or an earlier one."""

    source_section = forms.ModelChoiceField(
        queryset=Section.objects.none(),
        empty_label="Select source section...",
        label="Copy students from",
        help_text=(
            "Every learner enrolled in the chosen section is copied. Learners "
            "already enrolled elsewhere this school year are moved here; their "
            "previous year enrollment is left on record."
        ),
    )

    def __init__(self, *args, section=None, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)
        self.section = section
        if section is not None:
            self.fields["source_section"].queryset = copyable_source_sections(section)

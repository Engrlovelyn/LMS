from django import forms

from apps.accounts.models import StudentProfile, User

from .models import AcademicStructureVersion, AcademicYear, Section, StudentEnrollment, Subject

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


class StudentEnrollmentForm(forms.ModelForm):
    class Meta:
        model = StudentEnrollment
        fields = ["student", "academic_year", "section"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)
        self.fields["academic_year"].queryset = AcademicYear.objects.filter(
            status=AcademicYear.STATUS_ACTIVE
        )
        self.fields["section"].queryset = Section.objects.filter(
            status=Section.STATUS_ACTIVE
        )
        enrolled_ids = StudentEnrollment.objects.filter(
            status=StudentEnrollment.STATUS_ENROLLED
        ).values_list("student_id", flat=True)
        self.fields["student"].queryset = StudentProfile.objects.exclude(
            id__in=enrolled_ids
        ).order_by("lrn")


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
    )

    def __init__(self, *args, source_year=None, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)
        if source_year is not None:
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

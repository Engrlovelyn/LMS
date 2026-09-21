from django import forms

from apps.accounts.models import StudentProfile
from apps.academic_structure.models import AcademicYear, Section, Subject

from .models import VirtualClassroom


class VirtualClassroomForm(forms.ModelForm):
    class Meta:
        model = VirtualClassroom
        fields = [
            "academic_year",
            "section",
            "subject",
            "teacher",
            "grading_period",
            "theme_color",
            "background_image",
            "status",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["theme_color"].widget = forms.TextInput(
            attrs={"type": "color", "class": "form-control"}
        )
        self.fields["academic_year"].queryset = AcademicYear.objects.filter(status="ACTIVE")
        self.fields["section"].queryset = Section.objects.filter(status="ACTIVE")
        self.fields["subject"].queryset = Subject.objects.filter(status="ACTIVE")

    def clean(self):
        cleaned = super().clean()
        academic_year = cleaned.get("academic_year")
        section = cleaned.get("section")
        subject = cleaned.get("subject")
        if section and subject:
            if section.grade_level != subject.grade_level:
                self.add_error(
                    "subject",
                    "The subject's grade level must match the section's grade level.",
                )
            if academic_year and (
                section.academic_year_id != academic_year.pk
                or subject.academic_year_id != academic_year.pk
            ):
                self.add_error(
                    "academic_year",
                    "The section and subject must belong to the selected academic year.",
                )
        return cleaned


class VirtualClassroomEnrollForm(forms.Form):
    students = forms.ModelMultipleChoiceField(
        queryset=StudentProfile.objects.order_by("lrn"),
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["students"].label = False


class AddStudentForm(forms.Form):
    student = forms.ModelChoiceField(
        queryset=StudentProfile.objects.order_by("lrn"),
        empty_label="Select a student...",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["student"].widget.attrs["class"] = "form-control"

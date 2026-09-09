from django import forms

from apps.academic_structure.models import AcademicYear, Section, Subject


class EnrollmentFilterForm(forms.Form):
    academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.order_by("-start_date"),
        empty_label="All academic years",
        required=False,
    )
    section = forms.ModelChoiceField(
        queryset=Section.objects.order_by("grade_level", "name"),
        empty_label="All sections",
        required=False,
    )
    subject = forms.ModelChoiceField(
        queryset=Subject.objects.order_by("name"),
        empty_label="All subjects",
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def filtered_classrooms(self):
        from apps.virtual_classrooms.models import VirtualClassroom

        qs = (
            VirtualClassroom.objects.select_related(
                "academic_year", "section", "subject", "teacher"
            )
            .filter(status=VirtualClassroom.STATUS_ACTIVE)
            .order_by("section__grade_level", "section__name", "subject__name")
        )
        cleaned = self.cleaned_data
        if cleaned.get("academic_year"):
            qs = qs.filter(academic_year=cleaned["academic_year"])
        if cleaned.get("section"):
            qs = qs.filter(section=cleaned["section"])
        if cleaned.get("subject"):
            qs = qs.filter(subject=cleaned["subject"])
        return qs
from django import forms

from apps.academic_structure.models import AcademicYear

from .models import GeneratedReport


class SFReportForm(forms.Form):
    REPORT_TYPE_CHOICES = [
        (GeneratedReport.TYPE_SF1, "School Form 1 (SF1) - Learner Roster"),
        (GeneratedReport.TYPE_SF2, "School Form 2 (SF2) - Daily Attendance"),
    ]

    report_type = forms.ChoiceField(choices=REPORT_TYPE_CHOICES, label="Report Type")
    academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.all(),
        label="Academic Year",
        empty_label="Select an academic year",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["academic_year"].widget.attrs.update({"class": "form-control"})
        self.fields["report_type"].widget.attrs.update({"class": "form-control"})

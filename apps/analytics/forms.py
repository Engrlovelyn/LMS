from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.academic_structure.models import AcademicYear

from .models import GeneratedReport, SFReportTemplate
from .sf2 import validate_sf2_template_file
from .class_record import validate_class_record_template_file


class SFReportForm(forms.Form):
    REPORT_TYPE_CHOICES = [
        (GeneratedReport.TYPE_SF1, "School Form 1 (SF1) - Learner Roster"),
        (GeneratedReport.TYPE_SF2, "School Form 2 (SF2) - Daily Attendance"),
        (GeneratedReport.TYPE_SF3, "School Form 3 (SF3) - Summary of Attendance"),
    ]

    report_type = forms.ChoiceField(choices=REPORT_TYPE_CHOICES, label="Report Type")
    academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.all(),
        label="Academic Year",
        empty_label="Select an academic year",
    )
    report_month = forms.DateField(
        required=False,
        input_formats=["%Y-%m"],
        widget=forms.DateInput(attrs={"type": "month"}, format="%Y-%m"),
        label="Report Month",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["academic_year"].widget.attrs.update({"class": "form-control"})
        self.fields["report_type"].widget.attrs.update({"class": "form-control"})
        self.fields["report_month"].widget.attrs.update({"class": "form-control"})
        today = timezone.localdate()
        self.fields["report_month"].initial = today.replace(day=1)

    def clean(self):
        cleaned_data = super().clean()
        if (
            cleaned_data.get("report_type") == GeneratedReport.TYPE_SF2
            and not cleaned_data.get("report_month")
        ):
            self.add_error("report_month", "Choose a month for the SF2 report.")
        return cleaned_data


class SFReportTemplateForm(forms.ModelForm):
    class Meta:
        model = SFReportTemplate
        fields = ["name", "form_type", "description", "template_file", "field_mappings"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["field_mappings"].widget.attrs[
            "placeholder"
        ] = '{"sheet_name":"Term 1","first_data_row":14,"row_limit":47,"name_column":"B","assessment_columns":["C","D","E"]}'

    def clean(self):
        cleaned_data = super().clean()
        template_file = cleaned_data.get("template_file")
        if (
            template_file
            and cleaned_data.get("form_type") == SFReportTemplate.FORM_SF2
        ):
            try:
                validate_sf2_template_file(
                    template_file,
                    cleaned_data.get("field_mappings") or {},
                )
            except ValidationError as exc:
                self.add_error("template_file", exc)
        if (
            template_file
            and cleaned_data.get("form_type") == SFReportTemplate.FORM_CLASS_RECORD
        ):
            try:
                validate_class_record_template_file(
                    template_file,
                    cleaned_data.get("field_mappings") or {},
                )
            except ValidationError as exc:
                self.add_error("template_file", exc)
        return cleaned_data

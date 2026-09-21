from django.db import models


class GeneratedReport(models.Model):
    TYPE_SUBMISSION_REPORT = "submission_report"
    TYPE_SF1 = "sf1"
    TYPE_SF2 = "sf2"
    REPORT_TYPE_CHOICES = [
        (TYPE_SUBMISSION_REPORT, "Submission and Assessment Report"),
        (TYPE_SF1, "School Form 1 (SF1)"),
        (TYPE_SF2, "School Form 2 (SF2)"),
    ]

    report_type = models.CharField(max_length=30, choices=REPORT_TYPE_CHOICES)
    academic_year = models.ForeignKey(
        "academic_structure.AcademicYear", on_delete=models.PROTECT, related_name="generated_reports"
    )
    scope_description = models.CharField(max_length=255)
    parameters = models.JSONField(default=dict, blank=True)
    file = models.FileField(upload_to="generated_reports/")
    generated_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="generated_reports"
    )
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [
            ("view_school_wide_analytics", "View School-Wide Analytics & Reports"),
            ("view_class_level_analytics", "View Class-Level Analytics"),
            ("generate_attendance_trend_analytics", "Generate Attendance Trend Analytics"),
            ("generate_submission_assessment_reports", "Generate Submission and Assessment Reports"),
            ("manage_sf_report_templates", "Manage SF Report Templates"),
            ("generate_sf_reports", "Generate SF Reports"),
            ("monitor_student_activity_logs", "Monitor Student Activity Logs"),
        ]

def __str__(self):
        return f"{self.get_report_type_display()} - {self.scope_description} ({self.generated_at:%Y-%m-%d})"


class SFReportTemplate(models.Model):
    FORM_SF1 = "sf1"
    FORM_SF2 = "sf2"
    FORM_SF4 = "sf4"
    FORM_SF5 = "sf5"
    FORM_SF9 = "sf9"
    FORM_SF10 = "sf10"
    FORM_TYPE_CHOICES = [
        (FORM_SF1, "School Form 1 (SF1) - Learner Roster"),
        (FORM_SF2, "School Form 2 (SF2) - Daily Attendance"),
        (FORM_SF4, "School Form 4 (SF4) - Monthly Report"),
        (FORM_SF5, "School Form 5 (SF5) - Report on Promotion"),
        (FORM_SF9, "School Form 9 (SF9) - Learner Progress Report Card"),
        (FORM_SF10, "School Form 10 (SF10) - Learner's Permanent Academic Record"),
    ]

    name = models.CharField(max_length=150)
    form_type = models.CharField(max_length=10, choices=FORM_TYPE_CHOICES)
    description = models.CharField(max_length=255, blank=True)
    template_file = models.FileField(upload_to="sf_report_templates/")
    field_mappings = models.JSONField(default=dict, blank=True,
                                      help_text="Maps SF template fields to system data fields, e.g. {\"LRN\": \"lrn\", \"Last Name\": \"last_name\"}.")
    is_active = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="sf_report_templates"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["form_type", "name"]

    def __str__(self):
        return self.name

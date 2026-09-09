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

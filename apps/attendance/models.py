from django.db import models
from django.core.exceptions import ValidationError


class AttendanceSession(models.Model):
    virtual_classroom = models.ForeignKey(
        "virtual_classrooms.VirtualClassroom", on_delete=models.PROTECT, related_name="attendance_sessions"
    )
    session_date = models.DateField()
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="recorded_attendance_sessions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["virtual_classroom", "session_date"],
                name="unique_session_per_vc_per_date",
            )
        ]
        ordering = ["-session_date"]
        permissions = [
            ("record_class_attendance", "Record Class Attendance"),
            ("generate_attendance_report", "Generate Attendance Report"),
        ]

    def __str__(self):
        return f"{self.virtual_classroom} - {self.session_date}"


class AttendanceRecord(models.Model):
    PRESENT_ON_TIME = "present_on_time"
    PRESENT_LATE = "present_late"
    ABSENT_EXCUSED = "absent_excused"
    ABSENT_UNEXCUSED = "absent_unexcused"
    STATUS_CHOICES = [
        (PRESENT_ON_TIME, "Present - On Time"),
        (PRESENT_LATE, "Present - Late"),
        (ABSENT_EXCUSED, "Absent - Excused"),
        (ABSENT_UNEXCUSED, "Absent - Unexcused"),
    ]

    session = models.ForeignKey(AttendanceSession, on_delete=models.CASCADE, related_name="records")
    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.PROTECT, related_name="attendance_records"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    remark = models.TextField(blank=True)
    attachment = models.FileField(upload_to="attendance_attachments/", null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "student"],
                name="unique_attendance_record_per_student_session",
            )
        ]
        permissions = [
            ("view_attendance_records", "View Attendance Records"),
            ("update_attendance_record_custom", "Update Attendance Record"),
        ]

    def clean(self):
        if self.status == self.ABSENT_EXCUSED and not self.remark and not self.attachment:
            raise ValidationError("An excused absence requires a note or a supporting attachment.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student} - {self.session} ({self.get_status_display()})"

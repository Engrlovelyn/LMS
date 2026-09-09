from django.db import models
from django.core.exceptions import ValidationError


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------
class Enrollment(models.Model):
    """
    A student's enrollment record in a specific virtual classroom for a
    given academic year.

    Tracks the enrollment lifecycle (pending -> enrolled -> dropped/
    completed) separately from the VirtualClassroom itself, so that
    enrollment-specific concerns (approval, dropping, history) don't
    bloat the virtual_classrooms app.
    """

    STATUS_PENDING = "pending"
    STATUS_ENROLLED = "enrolled"
    STATUS_DROPPED = "dropped"
    STATUS_COMPLETED = "completed"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ENROLLED, "Enrolled"),
        (STATUS_DROPPED, "Dropped"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_REJECTED, "Rejected"),
    ]

    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.PROTECT, related_name="enrollments"
    )
    virtual_classroom = models.ForeignKey(
        "virtual_classrooms.VirtualClassroom", on_delete=models.PROTECT, related_name="enrollments"
    )
    academic_year = models.ForeignKey(
        "academic_structure.AcademicYear", on_delete=models.PROTECT, related_name="enrollments"
    )

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=STATUS_PENDING)

    enrolled_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_enrollments",
        help_text="Staff/teacher/admin who approved or processed this enrollment, if applicable.",
    )

    requested_at = models.DateTimeField(auto_now_add=True)
    enrolled_at = models.DateTimeField(null=True, blank=True)
    dropped_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    remarks = models.TextField(blank=True)

    # Flexible metadata for enrollment-specific details that may vary
    # without requiring schema changes, e.g.
    # {"drop_reason": "transferred_section", "waitlist_position": 3}
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "virtual_classroom"],
                name="unique_enrollment_per_student_per_vc",
            )
        ]
        indexes = [
            models.Index(fields=["student", "status"]),
            models.Index(fields=["virtual_classroom", "status"]),
        ]
        permissions = [
            ("enroll_student_custom", "Enroll Student"),
            ("approve_enrollment_custom", "Approve Enrollment"),
            ("drop_student_custom", "Drop Student from Classroom"),
            ("view_enrollment_custom", "View Enrollment"),
            ("manage_enrollment_custom", "Manage Enrollment"),
        ]

    def clean(self):
        if self.status == self.STATUS_ENROLLED and not self.enrolled_at:
            raise ValidationError("An enrolled record must have an enrolled_at timestamp.")
        if self.status == self.STATUS_DROPPED and not self.dropped_at:
            raise ValidationError("A dropped record must have a dropped_at timestamp.")

    @property
    def is_active(self):
        return self.status == self.STATUS_ENROLLED

    def __str__(self):
        return f"{self.student} - {self.virtual_classroom} ({self.get_status_display()})"


# ---------------------------------------------------------------------------
# EnrollmentHistory
# ---------------------------------------------------------------------------
class EnrollmentHistory(models.Model):
    """
    Audit trail of status changes for an Enrollment record — e.g. pending ->
    enrolled, enrolled -> dropped. Kept separate from Enrollment itself so
    the current state stays simple while still preserving a full history.
    """

    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="history")
    from_status = models.CharField(max_length=15, choices=Enrollment.STATUS_CHOICES, blank=True)
    to_status = models.CharField(max_length=15, choices=Enrollment.STATUS_CHOICES)
    changed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="enrollment_changes"
    )
    reason = models.TextField(blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.enrollment} : {self.from_status} -> {self.to_status}"
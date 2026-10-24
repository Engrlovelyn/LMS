from django.db import models


class VirtualClassroom(models.Model):
    STATUS_ACTIVE, STATUS_ARCHIVED = "active", "archived"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    academic_year = models.ForeignKey(
        "academic_structure.AcademicYear", on_delete=models.PROTECT, related_name="virtual_classrooms"
    )
    section = models.ForeignKey(
        "academic_structure.Section", on_delete=models.PROTECT, related_name="virtual_classrooms"
    )
    subject = models.ForeignKey(
        "academic_structure.Subject", on_delete=models.PROTECT, related_name="virtual_classrooms"
    )
    teacher = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="teaching_classrooms"
    )
    grading_period = models.CharField(max_length=15, blank=True, help_text="e.g. 1ST_TRIMESTER")
    schedule = models.CharField(max_length=100, blank=True, help_text="e.g. Mon–Wed 8:00–9:30 AM")
    theme_color = models.CharField(max_length=7, default="#4F46E5")
    background_image = models.ImageField(upload_to="vc_backgrounds/", null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "section", "academic_year", "grading_period"],
                name="unique_vc_per_subject_section_year_period",
            )
        ]
        permissions = [
            ("create_virtual_classroom", "Create Virtual Classroom"),
            ("view_virtual_classroom", "View Virtual Classroom"),
            ("add_student_to_class", "Add Student to Class"),
            ("archive_student_from_class", "Archive Student from Class"),
            ("enroll_students_to_vc", "Enroll Students to Virtual Classroom"),
            ("update_virtual_classroom", "Update Virtual Classroom"),
            ("archive_virtual_classroom", "Archive Virtual Classroom"),
        ]

    def __str__(self):
        return f"{self.subject} - {self.section} ({self.academic_year})"


class VirtualClassroomEnrollment(models.Model):
    STATUS_ACTIVE, STATUS_REMOVED = "active", "removed"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_REMOVED, "Removed"),
    ]

    virtual_classroom = models.ForeignKey(VirtualClassroom, on_delete=models.PROTECT, related_name="enrollments")
    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.PROTECT, related_name="vc_enrollments"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    enrolled_at = models.DateTimeField(auto_now_add=True)
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["virtual_classroom", "student"],
                condition=models.Q(status="active"),
                name="unique_active_enrollment_per_vc",
            )
        ]

    def __str__(self):
        return f"{self.student} - {self.virtual_classroom}"
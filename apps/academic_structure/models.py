from django.db import models
from django.core.exceptions import ValidationError


class AcademicYear(models.Model):
    STATUS_ACTIVE, STATUS_ARCHIVED = "ACTIVE", "ARCHIVED"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    label = models.CharField(max_length=9, unique=True, help_text="Format: YYYY-YYYY, e.g. 2026-2027")
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["status"],
                condition=models.Q(status="ACTIVE"),
                name="only_one_active_academic_year",
            )
        ]
        permissions = [
            ("add_academic_year_custom", "Add Academic Year"),
            ("update_academic_year", "Update Academic Year"),
            ("archive_academic_year", "Archive Academic Year"),
            ("manage_academic_structure_template", "Manage Academic Structure Template"),
            ("rollover_academic_year", "Rollover Academic Year"),
            ("promote_student", "Promote Student"),
            ("validate_section_subject_coverage", "Validate Section Subject Coverage"),
        ]

    def clean(self):
        if self.end_date <= self.start_date:
            raise ValidationError("End date must be after start date.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.label


class AcademicStructureVersion(models.Model):
    QUARTER, SEMESTER, TRIMESTER = "QUARTER", "SEMESTER", "TRIMESTER"
    GRADING_PERIOD_TYPE_CHOICES = [
        (QUARTER, "Quarter"),
        (SEMESTER, "Semester"),
        (TRIMESTER, "Trimester"),
    ]

    academic_year = models.OneToOneField(
        AcademicYear,
        on_delete=models.PROTECT,
        related_name="structure_version"
    )
    grading_period_type = models.CharField(max_length=10, choices=GRADING_PERIOD_TYPE_CHOICES)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Gate for the "finish" step of Manage Academic Structure Template (Table 3.4).
    # A structure cannot be finalized until every grade level with active Sections
    # for its Academic Year also has Subjects defined (see Table 3.11 Add Subject).
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.academic_year.label} - {self.get_grading_period_type_display()}"


class Section(models.Model):
    STATUS_ACTIVE, STATUS_ARCHIVED = "ACTIVE", "ARCHIVED"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    GRADE_LEVEL_CHOICES = [(i, f"Grade {i}") for i in range(1, 13)]

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="sections")
    name = models.CharField(max_length=30, help_text="e.g. Rose, STEM-A, Newton")
    grade_level = models.PositiveSmallIntegerField(choices=GRADE_LEVEL_CHOICES)
    adviser = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="advised_sections"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "grade_level", "name"],
                name="unique_section_per_year_and_grade",
            )
        ]
        permissions = [
            ("add_section_custom", "Add Section"),
            ("update_section_custom", "Update Section"),
            ("archive_section_custom", "Archive Section"),
        ]

    def __str__(self):
        return f"Grade {self.grade_level} - {self.name} ({self.academic_year.label})"


class Subject(models.Model):
    STATUS_ACTIVE, STATUS_ARCHIVED = "ACTIVE", "ARCHIVED"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    TYPE_CORE, TYPE_ELECTIVE = "APPLIED", "ELECTIVE"
    SUBJECT_TYPE_CHOICES = [
        (TYPE_CORE, "Applied"),
        (TYPE_ELECTIVE, "Elective"),
    ]

    GRADE_LEVEL_CHOICES = [(i, f"Grade {i}") for i in range(1, 13)]

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="subjects")
    name = models.CharField(max_length=100, help_text="e.g. Disciplines and Ideas in the Social Sciences")
    grade_level = models.PositiveSmallIntegerField(choices=GRADE_LEVEL_CHOICES)
    subject_type = models.CharField(max_length=10, choices=SUBJECT_TYPE_CHOICES, default=TYPE_CORE)
    grading_period = models.CharField(max_length=15, blank=True, help_text="e.g. 1ST_TRIMESTER")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "name", "grade_level", "grading_period"],
                name="unique_subject_per_year_grade_period",
            )
        ]
        permissions = [
            ("add_subject_custom", "Add Subject"),
            ("update_subject_custom", "Update Subject"),
            ("archive_subject_custom", "Archive Subject"),
        ]

    def __str__(self):
        return f"{self.name} (Grade {self.grade_level}, {self.academic_year.label})"
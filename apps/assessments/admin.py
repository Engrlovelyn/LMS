from django.contrib import admin
from .models import (
    Assessment,
    AssessmentPart,
    AssessmentAssignment,
    AssessmentProctoringEvent,
    Submission,
    SubmissionResponse,
    GradingFormula,
    GradingComponent,
    StudentGrade,
    AssessmentNotification,
)


class AssessmentPartInline(admin.TabularInline):
    model = AssessmentPart
    extra = 0


@admin.register(Assessment)
class AssessmentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "assessment_method",
        "assessment_type",
        "subject",
        "academic_year",
        "lifecycle_stage",
        "status",
    )
    list_filter = ("assessment_method", "assessment_type", "lifecycle_stage", "status")
    search_fields = ("title",)
    inlines = [AssessmentPartInline]


@admin.register(AssessmentPart)
class AssessmentPartAdmin(admin.ModelAdmin):
    list_display = ("assessment", "order", "question_type", "points")
    list_filter = ("question_type",)


@admin.register(AssessmentAssignment)
class AssessmentAssignmentAdmin(admin.ModelAdmin):
    list_display = ("assessment", "virtual_classroom", "assigned_by", "is_original", "assigned_at")
    list_filter = ("virtual_classroom", "is_original")


class SubmissionResponseInline(admin.TabularInline):
    model = SubmissionResponse
    extra = 0


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "student",
        "assessment_assignment",
        "submitted_at",
        "is_late",
        "total_score",
        "grading_status",
        "graded_at",
    )
    list_filter = ("is_late", "grading_status", "assessment_assignment")
    search_fields = ("student__last_name", "student__first_name")
    inlines = [SubmissionResponseInline]


class GradingComponentInline(admin.TabularInline):
    model = GradingComponent
    extra = 0


@admin.register(GradingFormula)
class GradingFormulaAdmin(admin.ModelAdmin):
    list_display = ("virtual_classroom", "structure_version", "created_at")
    inlines = [GradingComponentInline]


@admin.register(StudentGrade)
class StudentGradeAdmin(admin.ModelAdmin):
    list_display = ("student", "subject", "academic_year", "grading_period", "grade_value")
    list_filter = ("subject", "academic_year", "grading_period")
    search_fields = ("student__last_name", "student__first_name")


@admin.register(AssessmentNotification)
class AssessmentNotificationAdmin(admin.ModelAdmin):
    list_display = ("assessment", "student", "event_type", "is_read", "created_at")
    list_filter = ("event_type", "is_read")
    search_fields = ("student__last_name", "student__first_name")


@admin.register(AssessmentProctoringEvent)
class AssessmentProctoringEventAdmin(admin.ModelAdmin):
    list_display = ("assessment", "student", "event_type", "created_at")
    list_filter = ("event_type", "assessment")
    search_fields = ("student__last_name", "student__first_name")
    date_hierarchy = "created_at"
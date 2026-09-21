from django.contrib import admin
from .models import (
    AcademicYear,
    AcademicStructureVersion,
    Section,
    Subject,
)


@admin.register(AcademicYear)
class AcademicYearAdmin(admin.ModelAdmin):
    list_display = ("label", "start_date", "end_date", "status")
    list_filter = ("status",)


@admin.register(AcademicStructureVersion)
class AcademicStructureVersionAdmin(admin.ModelAdmin):
    list_display = ("academic_year", "grading_period_type")
    list_filter = ("grading_period_type",)


@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    list_display = ("name", "grade_level", "academic_year", "adviser", "status")
    list_filter = ("academic_year", "grade_level", "status")
    search_fields = ("name",)


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("name", "grade_level", "subject_type", "grading_period", "academic_year", "status")
    list_filter = ("academic_year", "grade_level", "subject_type", "status")
    search_fields = ("name",)
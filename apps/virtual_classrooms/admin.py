from django.contrib import admin
from .models import VirtualClassroom, VirtualClassroomEnrollment


class VirtualClassroomEnrollmentInline(admin.TabularInline):
    model = VirtualClassroomEnrollment
    extra = 0


@admin.register(VirtualClassroom)
class VirtualClassroomAdmin(admin.ModelAdmin):
    list_display = ("subject", "section", "academic_year", "teacher", "grading_period", "status")
    list_filter = ("academic_year", "status", "section")
    search_fields = ("subject__name", "section__name")
    inlines = [VirtualClassroomEnrollmentInline]


@admin.register(VirtualClassroomEnrollment)
class VirtualClassroomEnrollmentAdmin(admin.ModelAdmin):
    list_display = ("student", "virtual_classroom", "status", "enrolled_at")
    list_filter = ("status", "virtual_classroom")
    search_fields = ("student__last_name", "student__first_name")
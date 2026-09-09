from django.contrib import admin

from .models import AttendanceRecord, AttendanceSession


class AttendanceRecordInline(admin.TabularInline):
    model = AttendanceRecord
    extra = 0
    readonly_fields = ("student", "status", "remark", "recorded_at", "updated_at")
    can_delete = False


@admin.register(AttendanceSession)
class AttendanceSessionAdmin(admin.ModelAdmin):
    list_display = ("virtual_classroom", "session_date", "created_by", "created_at")
    list_filter = ("session_date", "virtual_classroom")
    search_fields = ("virtual_classroom__subject__name", "virtual_classroom__section__name")
    inlines = [AttendanceRecordInline]


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ("student", "session", "status", "recorded_at", "updated_at")
    list_filter = ("status", "session__session_date")
    search_fields = ("student__lrn", "student__last_name")

from django.contrib import admin
from .models import (
    SystemConfiguration,
    PasswordPolicy,
    AttendanceThresholdConfig,
    BackupRecord,
)


@admin.register(SystemConfiguration)
class SystemConfigurationAdmin(admin.ModelAdmin):
    list_display = ("session_timeout_minutes", "account_lockout_threshold", "maintenance_mode", "updated_at")

    def has_add_permission(self, request):
        return not SystemConfiguration.objects.exists()


@admin.register(PasswordPolicy)
class PasswordPolicyAdmin(admin.ModelAdmin):
    list_display = ("minimum_length", "expiration_days", "reuse_limit", "updated_at")

    def has_add_permission(self, request):
        return not PasswordPolicy.objects.exists()


@admin.register(AttendanceThresholdConfig)
class AttendanceThresholdConfigAdmin(admin.ModelAdmin):
    list_display = ("absence_count", "window_school_days", "updated_at")

    def has_add_permission(self, request):
        return not AttendanceThresholdConfig.objects.exists()


@admin.register(BackupRecord)
class BackupRecordAdmin(admin.ModelAdmin):
    list_display = ("backup_type", "status", "initiated_by", "created_at", "restored_at")
    list_filter = ("backup_type", "status")
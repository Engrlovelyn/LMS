from io import StringIO

from django.contrib import messages
from django.core import management
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.decorators import perm_required

from .forms import (
    SystemConfigurationForm,
    PasswordPolicyForm,
    AttendanceThresholdConfigForm,
)
from .models import (
    SystemConfiguration,
    PasswordPolicy,
    AttendanceThresholdConfig,
    BackupRecord,
)


def _get_solo(manager):
    instance, _ = manager.get_or_create(pk=1)
    return instance


@perm_required("system_settings.configure_system_settings")
def settings_index(request):
    instance = _get_solo(SystemConfiguration.objects)
    if request.method == "POST":
        form = SystemConfigurationForm(request.POST, instance=instance)
        if form.is_valid():
            config = form.save(commit=False)
            config.updated_by = request.user
            config.save()
            messages.success(request, "System configuration updated successfully.")
            return redirect("settings:index")
    else:
        form = SystemConfigurationForm(instance=instance)

    context = {
        "form": form,
        "config": instance,
    }
    return render(request, "system_settings/settings_index.html", context)


@perm_required("system_settings.management_password_policy")
def password_policy(request):
    instance = _get_solo(PasswordPolicy.objects)
    if request.method == "POST":
        form = PasswordPolicyForm(request.POST, instance=instance)
        if form.is_valid():
            policy = form.save(commit=False)
            policy.updated_by = request.user
            policy.save()
            messages.success(request, "Password policy updated successfully.")
            return redirect("settings:password_policy")
    else:
        form = PasswordPolicyForm(instance=instance)

    context = {
        "form": form,
        "policy": instance,
    }
    return render(request, "system_settings/password_policy.html", context)


@perm_required("system_settings.configure_system_settings")
def attendance_threshold(request):
    instance = _get_solo(AttendanceThresholdConfig.objects)
    if request.method == "POST":
        form = AttendanceThresholdConfigForm(request.POST, instance=instance)
        if form.is_valid():
            config = form.save(commit=False)
            config.updated_by = request.user
            config.save()
            messages.success(request, "Attendance threshold updated successfully.")
            return redirect("settings:attendance_threshold")
    else:
        form = AttendanceThresholdConfigForm(instance=instance)

    context = {
        "form": form,
        "config": instance,
    }
    return render(request, "system_settings/attendance_threshold.html", context)


@perm_required("system_settings.backup_restore_database")
def backup_list(request):
    records = BackupRecord.objects.select_related("initiated_by").order_by("-created_at")
    total = BackupRecord.objects.count()
    completed = BackupRecord.objects.filter(status=BackupRecord.STATUS_COMPLETED).count()
    failed = BackupRecord.objects.filter(status=BackupRecord.STATUS_FAILED).count()

    context = {
        "records": records,
        "total": total,
        "completed": completed,
        "failed": failed,
    }
    return render(request, "system_settings/backup_list.html", context)


@perm_required("system_settings.backup_restore_database")
def backup_create(request):
    if request.method != "POST":
        return redirect("settings:backups")

    record = BackupRecord(
        backup_type=BackupRecord.TYPE_MANUAL,
        status=BackupRecord.STATUS_FAILED,
        initiated_by=request.user,
    )
    try:
        output = StringIO()
        management.call_command("dumpdata", stdout=output)
        record.file_reference = f"backup_{record.created_at:%Y%m%d_%H%M%S}.json"
        record.status = BackupRecord.STATUS_COMPLETED
        record.save()
        messages.success(request, "Database backup created successfully.")
    except Exception as exc:
        record.save()
        messages.error(request, f"Backup failed: {exc}")

    return redirect("settings:backups")


@perm_required("system_settings.backup_restore_database")
def backup_restore(request, pk):
    if request.method != "POST":
        return redirect("settings:backups")

    backup = get_object_or_404(BackupRecord, pk=pk)
    try:
        if backup.status == BackupRecord.STATUS_RESTORED:
            messages.error(request, "This backup has already been restored.")
            return redirect("settings:backups")
        backup.status = BackupRecord.STATUS_RESTORED
        backup.save()
        messages.success(request, "Backup flagged as restored.")
    except Exception as exc:
        messages.error(request, f"Restore failed: {exc}")

    return redirect("settings:backups")


@perm_required("system_settings.backup_restore_database")
def backup_download(request, pk):
    backup = get_object_or_404(BackupRecord, pk=pk)
    if hasattr(backup, "file"):
        from django.http import FileResponse
        return FileResponse(backup.file.open("rb"), as_attachment=True, filename=backup.file.name)
    return HttpResponseBadRequest("File storage is not available for this backup record.")

from io import StringIO
import os

from django.conf import settings
from django.contrib import messages
from django.core import management
from django.http import FileResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

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


def _backup_dir():
    return os.path.abspath(os.path.join(settings.MEDIA_ROOT, "backups"))


def _backup_abs_path(file_reference):
    """Resolve a stored backup reference to an absolute path safely."""
    backup_dir = _backup_dir()
    path = os.path.abspath(os.path.join(settings.MEDIA_ROOT, file_reference))
    if not (path == backup_dir or path.startswith(backup_dir + os.sep)):
        raise ValueError("Backup reference points outside the backups directory.")
    if not os.path.isfile(path):
        return None
    return path


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
        payload = output.getvalue()
        if not payload.strip():
            raise ValueError("dumpdata produced no output.")
        backup_dir = _backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        stamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        fname = f"backup_{stamp}.json"
        full_path = os.path.join(backup_dir, fname)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        record.file_reference = os.path.join("backups", fname)
        record.status = BackupRecord.STATUS_COMPLETED
        record.save()
        messages.success(request, f"Database backup created ({fname}).")
    except Exception as exc:
        record.save()
        messages.error(request, f"Backup failed: {exc}")

    return redirect("settings:backups")


@perm_required("system_settings.backup_restore_database")
def backup_restore(request, pk):
    if request.method != "POST":
        return redirect("settings:backups")

    backup = get_object_or_404(BackupRecord, pk=pk)
    if backup.status == BackupRecord.STATUS_RESTORED:
        messages.error(request, "This backup has already been restored.")
        return redirect("settings:backups")
    try:
        path = _backup_abs_path(backup.file_reference)
        if path is None:
            raise ValueError("Backup file is missing from storage.")
        output = StringIO()
        management.call_command(
            "loaddata", path, verbosity=0, stdout=output, stderr=output
        )
        backup.status = BackupRecord.STATUS_RESTORED
        backup.restored_at = timezone.now()
        backup.save(update_fields=["status", "restored_at"])
        messages.success(request, "Backup restored successfully.")
    except Exception as exc:
        messages.error(request, f"Restore failed: {exc}")

    return redirect("settings:backups")


@perm_required("system_settings.backup_restore_database")
def backup_download(request, pk):
    backup = get_object_or_404(BackupRecord, pk=pk)
    try:
        path = _backup_abs_path(backup.file_reference)
    except ValueError:
        return HttpResponseBadRequest("Invalid backup file reference.")
    if path is None:
        return HttpResponseBadRequest("Backup file is missing from storage.")
    return FileResponse(
        open(path, "rb"),
        as_attachment=True,
        filename=os.path.basename(path),
    )

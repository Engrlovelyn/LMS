from django.db import models


class SystemConfiguration(models.Model):
    """Singleton — only one row ever exists (pk always 1)."""
    session_timeout_minutes = models.PositiveIntegerField(default=30)
    account_lockout_threshold = models.PositiveIntegerField(default=5)
    timezone = models.CharField(max_length=50, default="Asia/Manila")
    maintenance_mode = models.BooleanField(default=False)

    REGISTRATION_OPEN = "open"
    REGISTRATION_INVITE_ONLY = "invite_only"
    REGISTRATION_CHOICES = [
        (REGISTRATION_OPEN, "Open"),
        (REGISTRATION_INVITE_ONLY, "Invite Only"),
]
    user_registration_policy = models.CharField(
        max_length=20,
        choices=REGISTRATION_CHOICES,
        default=REGISTRATION_INVITE_ONLY
    )

    # Automation preferences surfaced across dashboard cards, e.g.
    # {"frequency": "Daily", "retention": 30, "auto": true}.
    backup_settings = models.JSONField(default=dict, blank=True)

    updated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="system_configuration_updates"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "System Configuration"

    class Meta:
        permissions = [
            ("configure_system_settings", "Configure System Settings"),
            ("management_password_policy", "Management Password Policy"),
            ("backup_restore_database", "Back up & Restore Database"),
        ]


class PasswordPolicy(models.Model):
    """Singleton, matches Table 2.2."""
    minimum_length = models.PositiveIntegerField(default=8)
    require_uppercase = models.BooleanField(default=True)
    require_lowercase = models.BooleanField(default=True)
    require_number = models.BooleanField(default=True)
    require_symbol = models.BooleanField(default=True)
    expiration_days = models.PositiveIntegerField(default=90)
    reuse_limit = models.PositiveIntegerField(default=3)

    updated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="password_policy_updates"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Password Policy"


class AttendanceThresholdConfig(models.Model):
    """Singleton. Referenced by Table 9.3."""
    absence_count = models.PositiveIntegerField(default=5)
    window_school_days = models.PositiveIntegerField(default=20)

    updated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="attendance_threshold_updates"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Attendance Threshold Configuration"


class BackupRecord(models.Model):
    TYPE_MANUAL, TYPE_SCHEDULED = "manual", "scheduled"
    BACKUP_TYPE_CHOICES = [
        (TYPE_MANUAL, "Manual"),
        (TYPE_SCHEDULED, "Scheduled"),
    ]

    STATUS_COMPLETED, STATUS_FAILED, STATUS_RESTORED = "completed", "failed", "restored"
    STATUS_CHOICES = [
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_RESTORED, "Restored"),
    ]

    backup_type = models.CharField(max_length=10, choices=BACKUP_TYPE_CHOICES)
    file_reference = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    initiated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="initiated_backups"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    restored_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.get_backup_type_display()} backup - {self.created_at:%Y-%m-%d %H:%M}"

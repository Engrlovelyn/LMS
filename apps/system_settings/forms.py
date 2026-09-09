from django import forms

from .models import (
    SystemConfiguration,
    PasswordPolicy,
    AttendanceThresholdConfig,
)


class SystemConfigurationForm(forms.ModelForm):
    class Meta:
        model = SystemConfiguration
        fields = [
            "session_timeout_minutes",
            "account_lockout_threshold",
            "timezone",
            "maintenance_mode",
            "user_registration_policy",
        ]
        widgets = {
            "session_timeout_minutes": forms.NumberInput(attrs={"class": "form-control"}),
            "account_lockout_threshold": forms.NumberInput(attrs={"class": "form-control"}),
            "timezone": forms.TextInput(attrs={"class": "form-control"}),
            "maintenance_mode": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "user_registration_policy": forms.Select(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["session_timeout_minutes"].help_text = "Minutes of inactivity before a user session expires."
        self.fields["account_lockout_threshold"].help_text = "Number of failed logins before an account is locked."
        self.fields["timezone"].help_text = "Default timezone applied across the system."
        self.fields["maintenance_mode"].help_text = "Enable to block normal user access during maintenance."
        self.fields["user_registration_policy"].help_text = "Who is allowed to register new accounts."


class PasswordPolicyForm(forms.ModelForm):
    class Meta:
        model = PasswordPolicy
        fields = [
            "minimum_length",
            "require_uppercase",
            "require_lowercase",
            "require_number",
            "require_symbol",
            "expiration_days",
            "reuse_limit",
        ]
        widgets = {
            "minimum_length": forms.NumberInput(attrs={"class": "form-control"}),
            "require_uppercase": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "require_lowercase": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "require_number": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "require_symbol": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "expiration_days": forms.NumberInput(attrs={"class": "form-control"}),
            "reuse_limit": forms.NumberInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["minimum_length"].help_text = "Minimum number of characters required for a password."
        self.fields["expiration_days"].help_text = "Number of days before a password must be changed."
        self.fields["reuse_limit"].help_text = "Number of previous passwords that cannot be reused."


class AttendanceThresholdConfigForm(forms.ModelForm):
    class Meta:
        model = AttendanceThresholdConfig
        fields = ["absence_count", "window_school_days"]
        widgets = {
            "absence_count": forms.NumberInput(attrs={"class": "form-control"}),
            "window_school_days": forms.NumberInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["absence_count"].help_text = "Number of absences that triggers the attendance warning."
        self.fields["window_school_days"].help_text = "Number of school days in the monitoring window."

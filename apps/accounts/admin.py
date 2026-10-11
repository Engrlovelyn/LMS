from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.utils import timezone

from .models import (
    User,
    Role,
    RoleAssignment,
    EmployeeProfile,
    StudentProfile,
    PasswordHistory,
    LoginSession,
    EmailVerificationToken,
    PasswordResetToken,
    ActivityLog,
)

# Guard against accidentally firing thousands of transactional mails at once.
RESEND_LIMIT = 50


class UserCreationForm(forms.ModelForm):
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ("email", "status")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["status"].initial = User.Status.PENDING_VERIFICATION

    def clean_email(self):
        email = self.cleaned_data.get("email") or ""
        return email.strip().lower()

    def clean_password2(self):
        p1 = self.cleaned_data.get("password1")
        p2 = self.cleaned_data.get("password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("Passwords do not match.")
        return p2

    def save(self, commit=True):
        user = super().save(commit=False)
        raw_password = self.cleaned_data["password1"]
        user.set_password(raw_password)
        user._temporary_password = raw_password
        if commit:
            user.save()
        return user


class UserChangeForm(forms.ModelForm):
    password = ReadOnlyPasswordHashField(
        help_text="Raw passwords are not stored, so there is no way to see "
                  "this user's password, but you can change it using the "
                  "change password form."
    )

    class Meta:
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = UserCreationForm
    form = UserChangeForm
    model = User

    list_display = ("email", "status", "is_staff", "is_active", "date_created")
    list_filter = ("status", "is_staff", "is_active", "must_change_password")
    search_fields = ("email",)
    ordering = ("email",)

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Status", {"fields": ("status", "failed_login_attempts", "locked_at",
                               "password_changed_at", "must_change_password")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "status", "password1", "password2"),
        }),
    )
    filter_horizontal = ("groups", "user_permissions")

    actions = ("mark_email_verified", "resend_verification_email")

    @admin.action(description="Mark email as verified (activate account)")
    def mark_email_verified(self, request, queryset):
        """Escape hatch for when the verification mail cannot be delivered.

        Promotes pending-verification accounts straight to active and closes
        out their outstanding tokens so the links stop working.
        """
        pending = queryset.filter(status=User.Status.PENDING_VERIFICATION)
        changed = 0
        for user in pending:
            now = timezone.now()
            user.status = User.Status.ACTIVE
            user.save(update_fields=["status", "is_active"])
            user.email_verification_tokens.filter(verified_at__isnull=True).update(
                verified_at=now
            )
            ActivityLog.objects.create(
                user=user,
                action="email_verified_by_admin",
                related_entity_type="User",
                related_entity_id=user.pk,
            )
            changed += 1
        if changed:
            messages.success(request, f"Activated {changed} account(s).")
        else:
            messages.warning(
                request, "No accounts pending verification were selected."
            )

    @admin.action(description="Resend email verification link")
    def resend_verification_email(self, request, queryset):
        from .views import _send_verification_email

        pending = queryset.filter(status=User.Status.PENDING_VERIFICATION)
        total = pending.count()
        if not total:
            messages.warning(
                request, "No accounts pending verification were selected."
            )
            return
        if total > RESEND_LIMIT:
            messages.error(
                request,
                f"{total} accounts selected. This action is capped at "
                f"{RESEND_LIMIT} to avoid tripping the mail provider's rate "
                f"limit - narrow the selection and try again.",
            )
            return

        sent = 0
        for user in pending:
            # The raw password is not recoverable from its hash, so the resent
            # mail tells the user to keep the one they already have.
            result = _send_verification_email(user, request, password="")
            if result.get("ok"):
                sent += 1
            else:
                messages.error(
                    request, f"{user.email}: {result.get('message')}"
                )
        if sent:
            messages.success(request, f"Sent {sent} verification email(s).")


    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change:
            return
        from .views import _send_verification_email
        result = _send_verification_email(
            obj,
            request,
            password=getattr(obj, "_temporary_password", ""),
        )
        if result.get("ok"):
            messages.success(request, f"Verification email sent to {obj.email}.")
        else:
            messages.error(
                request,
                f"{obj.email} was created but the verification email was NOT "
                f"sent: {result.get('message')}",
            )


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("code",)


@admin.register(RoleAssignment)
class RoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "academic_year", "date_assigned")
    list_filter = ("role", "academic_year")


@admin.register(EmployeeProfile)
class EmployeeProfileAdmin(admin.ModelAdmin):
    list_display = ("employee_number", "last_name", "first_name", "user")
    search_fields = ("employee_number", "last_name", "first_name")


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ("lrn", "last_name", "first_name", "user")
    search_fields = ("lrn", "last_name", "first_name")


admin.site.register(PasswordHistory)
admin.site.register(LoginSession)
admin.site.register(EmailVerificationToken)
admin.site.register(PasswordResetToken)
admin.site.register(ActivityLog)
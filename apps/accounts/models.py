from django.db import models
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.validators import RegexValidator


# ----------------------------------------
# USER
# ----------------------------------------
class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("status", User.Status.ACTIVE)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    class Status(models.TextChoices):
        PENDING_VERIFICATION = "pending_verification", "Pending Verification"
        PENDING_PASSWORD_SETUP = "pending_password_setup", "Pending Password Setup"
        ACTIVE = "active", "Active"
        LOCKED = "locked", "Locked"
        ARCHIVED = "archived", "Archived"

    email = models.EmailField(unique=True)
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING_VERIFICATION
    )
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    password_changed_at = models.DateTimeField(null=True, blank=True)
    # When the lockout policy last locked this account. Null while the account
    # has never been locked; cleared on unlock so the dashboard never shows a
    # stale lock date for an account that is already active again.
    locked_at = models.DateTimeField(null=True, blank=True)
    # Set when an administrator hands the user a password out-of-band (the
    # forgot-password recovery path). Because the administrator knows the
    # password, the user is forced to choose their own at next sign-in instead
    # of being able to keep using the shared one.
    must_change_password = models.BooleanField(default=False)
    date_created = models.DateTimeField(auto_now_add=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def save(self, *args, **kwargs):
        self.is_active = self.status == self.Status.ACTIVE
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email

    def get_full_name(self):
        for rel in ("employee_profile", "student_profile"):
            try:
                prof = getattr(self, rel)
            except (EmployeeProfile.DoesNotExist, StudentProfile.DoesNotExist):
                continue
            if prof is not None and getattr(prof, "first_name", ""):
                parts = [prof.first_name]
                if getattr(prof, "middle_initial", ""):
                    parts.append(prof.middle_initial.rstrip(".") + ".")
                parts.append(prof.last_name or "")
                name = " ".join(part for part in parts if part).strip()
                if getattr(prof, "suffix", ""):
                    name = f"{name} {prof.suffix}"
                if name:
                    return name
        return self.email or ""

    def get_initials(self):
        name = self.get_full_name()
        if not name:
            return "U"
        parts = [p.rstrip(".") for p in name.replace("@", " ").split() if p.rstrip(".")]
        if not parts:
            return "U"
        if len(parts) == 1:
            return parts[0][0].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    class Meta:
        permissions = [
            ("create_employee_account", "Create Employee Account"),
            ("view_employee_account", "View Employee Account"),
            ("generate_student_account", "Generate Student Account"),
            ("verify_email_address", "Verify Email Address"),
            ("password_setup", "Password Setup"),
            ("create_student_profile_account", "Create Student Profile and Account"),
            ("view_student_record", "View Student Record"),
            ("update_student_record", "Update Student Record"),
            ("archive_student_record", "Archive Student Record"),
            ("bulk_import_student_records", "Bulk Import Student Records"),
            ("bulk_import_students_to_vc", "Bulk Import Students to Virtual Classroom"),
            ("update_employee_account", "Update Employee Account"),
            ("archive_account", "Archive Account"),
            ("reset_account_password", "Reset Account Password"),
            ("reset_forgotten_password", "Reset Forgotten Password"),
            ("unlock_account", "Unlock Locked Account"),
            ("resend_verification_email", "Resend Verification Email"),
            ("log_in_user", "Log In User"),
            ("manage_active_sessions", "Manage Active Sessions"),
            ("view_role_based_dashboard", "View Role-Based Dashboard"),
            ("role_based_access_control", "Role-Based Access Control"),
        ]


class PasswordHistory(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="password_history"
    )
    hashed_password = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} - {self.created_at:%Y-%m-%d}"


# ----------------------------------------
# ROLES
# ----------------------------------------
class Role(models.Model):
    class Code(models.TextChoices):
        SYSTEM_ADMIN = "system_admin", "System Administrator"
        REGISTRAR = "registrar", "Registrar"
        TEACHER = "teacher", "Teacher"
        # The principal role code predates the school being split into a
        # Junior/High School campus and an Elementary campus. It is kept as
        # `principal` (rather than renamed) so every existing assignment, group
        # and login redirect keeps working; only the display label says which
        # campus it governs. Scope itself lives on `Campus.Scope`, not on the
        # role, so an Assistant Principal can be scoped per campus too.
        PRINCIPAL = "principal", "High School Principal"
        ELEMENTARY_PRINCIPAL = "elementary_principal", "Elementary Principal"
        ASSISTANT_PRINCIPAL = "assistant_principal", "Assistant Principal"
        STUDENT = "student", "Student"
        QUALITY_AUDIT = "quality_audit", "Quality Assurance"

    code = models.CharField(max_length=20, choices=Code.choices, unique=True)

    def __str__(self):
        return self.get_code_display()


class RoleAssignment(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="role_assignments"
    )
    role = models.ForeignKey(
        Role,
        on_delete=models.PROTECT,
        related_name="assignments"
    )
    academic_year = models.ForeignKey(
        "academic_structure.AcademicYear",
        on_delete=models.PROTECT,
        related_name="role_assignments"
    )
    date_assigned = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role", "academic_year"],
                name="unique_user_role_per_academic_year",
            )
        ]

    def __str__(self):
        return f"{self.user} - {self.role} ({self.academic_year})"


# ----------------------------------------
# PROFILES
# ----------------------------------------
class SuffixChoices(models.TextChoices):
    JR = "Jr.", "Jr."
    SR = "Sr.", "Sr."
    III = "III", "III"
    IV = "IV", "IV"
    V = "V", "V"


class SexChoices(models.TextChoices):
    MALE = "M", "Male"
    FEMALE = "F", "Female"


middle_initial_validator = RegexValidator(
    regex=r"^[A-Za-z]\.?$",
    message="Middle initial must be a single letter, e.g. M or M."
)

phone_validator = RegexValidator(
    regex=r"^09\d{9}$",
    message="Contact number must be an 11-digit PH mobile number, e.g. 09171234567."
)


class EmployeeProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="employee_profile"
    )
    employee_number = models.CharField(max_length=15, unique=True)
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    middle_initial = models.CharField(
        max_length=2,
        blank=True,
        validators=[middle_initial_validator]
    )
    suffix = models.CharField(max_length=5, choices=SuffixChoices.choices, blank=True)
    contact_number = models.CharField(
        max_length=11,
        blank=True,
        validators=[phone_validator]
    )
    sex = models.CharField(
        max_length=1,
        choices=SexChoices.choices,
        blank=True,
    )
    birthdate = models.DateField(null=True, blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)

    def __str__(self):
        return f"{self.last_name}, {self.first_name}"


lrn_validator = RegexValidator(
    regex=r"^\d{12}$",
    message="LRN must be exactly 12 digits."
)


class StudentProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="student_profile"
    )
    lrn = models.CharField(
        "Learner Reference Number",
        max_length=12,
        unique=True,
        validators=[lrn_validator]
    )
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    middle_initial = models.CharField(
        max_length=2,
        blank=True,
        validators=[middle_initial_validator]
    )
    suffix = models.CharField(max_length=5, choices=SuffixChoices.choices, blank=True)
    birthdate = models.DateField()
    sex = models.CharField(max_length=1, choices=SexChoices.choices, blank=True)
    contact_number = models.CharField(max_length=20, blank=True)
    address = models.CharField(max_length=255, blank=True)
    guardian_name = models.CharField(max_length=100, blank=True)
    guardian_email = models.EmailField(blank=True)
    guardian_contact_number = models.CharField(max_length=20, blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)

    def __str__(self):
        return f"{self.last_name}, {self.first_name} ({self.lrn})"

    @property
    def section_display(self):
        """Return current enrolled section label (with grade level), or None."""
        enrollment = (
            self.enrollments.filter(status="ENROLLED")
            .select_related("section__academic_year")
            .order_by("-academic_year__start_date")
            .first()
        )
        if enrollment and enrollment.section:
            section = enrollment.section
            return f"Grade {section.grade_level} - {section.name}"
        return None


# ----------------------------------------
# SESSIONS & TOKENS
# ----------------------------------------
class LoginSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    device_info = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    session_key = models.CharField(max_length=40, blank=True, null=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(auto_now=True)
    is_current = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user} - {self.device_info or 'Unknown device'}"


class EmailVerificationToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="email_verification_tokens")
    token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user} - {'verified' if self.verified_at else 'pending'}"


class PasswordResetToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="password_reset_tokens")
    token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user} - {'used' if self.used_at else 'active'}"


# ----------------------------------------
# AUDIT
# ----------------------------------------
class ActivityLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="activity_logs")
    action = models.CharField(max_length=100)
    related_entity_type = models.CharField(max_length=100, blank=True)
    related_entity_id = models.PositiveIntegerField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} - {self.action} @ {self.timestamp}"


# ----------------------------------------
# GROUP SYNC (connects RoleAssignment to Django's native Group system)
# ----------------------------------------
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.contrib.auth.models import Group


def _sync_user_groups(user):
    from apps.academic_structure.models import AcademicYear

    active_year = AcademicYear.objects.filter(status="ACTIVE").first()
    if not active_year:
        return

    current_role_codes = set(
        RoleAssignment.objects
        .filter(user=user, academic_year=active_year)
        .values_list("role__code", flat=True)
    )

    all_role_groups = Group.objects.filter(name__in=Role.Code.values)
    for group in all_role_groups:
        if group.name in current_role_codes:
            user.groups.add(group)
        else:
            user.groups.remove(group)


@receiver(post_save, sender=RoleAssignment)
def role_assignment_saved(sender, instance, **kwargs):
    _sync_user_groups(instance.user)


@receiver(post_delete, sender=RoleAssignment)
def role_assignment_deleted(sender, instance, **kwargs):
    _sync_user_groups(instance.user)
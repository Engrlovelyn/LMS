import csv
import io
import secrets
import string

from django import forms

from apps.academic_structure.models import AcademicYear, Section

from .models import EmployeeProfile, Role, StudentProfile, User

WIDGET_TYPES = (
    forms.TextInput,
    forms.Select,
    forms.Textarea,
    forms.NumberInput,
    forms.EmailInput,
    forms.DateInput,
    forms.PasswordInput,
)


def apply_form_control(fields):
    for field in fields.values():
        if isinstance(field.widget, WIDGET_TYPES):
            field.widget.attrs["class"] = "form-control"


def generate_password(length=12):
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


class EmployeeAccountForm(forms.ModelForm):
    email = forms.EmailField(label="Email Address")
    initial_password = forms.CharField(
        label="Initial Password",
        required=False,
        widget=forms.PasswordInput(render_value=True),
        help_text="Leave blank to auto-generate a password.",
    )
    grant_staff_access = forms.BooleanField(
        label="Staff access (Django admin)",
        required=False,
    )

    class Meta:
        model = EmployeeProfile
        fields = ["employee_number", "first_name", "last_name",
                  "middle_initial", "suffix", "contact_number"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)


class EmployeeAccountUpdateForm(forms.ModelForm):
    email = forms.EmailField(label="Email Address")

    class Meta:
        model = EmployeeProfile
        fields = ["employee_number", "first_name", "last_name",
                  "middle_initial", "suffix", "contact_number"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)


class StudentAccountForm(forms.ModelForm):
    email = forms.EmailField(
        label="Email Address",
        required=False,
        help_text="Leave blank to auto-generate (lrn@student.sanvicente.edu.ph).",
    )
    initial_password = forms.CharField(
        label="Initial Password",
        required=False,
        widget=forms.PasswordInput(render_value=True),
        help_text="Leave blank to auto-generate a password.",
    )
    section = forms.ModelChoiceField(
        queryset=Section.objects.none(),
        required=False,
        label="Section (enroll in active year)",
    )

    class Meta:
        model = StudentProfile
        fields = ["lrn", "first_name", "last_name", "middle_initial",
                  "suffix", "birthdate", "sex", "contact_number", "address",
                  "guardian_name", "guardian_email", "guardian_contact_number"]
        widgets = {
            "birthdate": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)
        active_year = AcademicYear.objects.filter(
            status=AcademicYear.STATUS_ACTIVE
        ).first()
        if active_year:
            self.fields["section"].queryset = Section.objects.filter(
                status=Section.STATUS_ACTIVE,
                academic_year=active_year,
            )


class StudentRecordUpdateForm(forms.ModelForm):
    email = forms.EmailField(label="Email Address")
    status = forms.ChoiceField(choices=User.Status.choices, label="Account Status")

    class Meta:
        model = StudentProfile
        fields = ["lrn", "first_name", "last_name", "middle_initial",
                  "suffix", "birthdate", "sex", "contact_number", "address",
                  "guardian_name", "guardian_email", "guardian_contact_number"]
        widgets = {
            "birthdate": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)


class StudentRecordsImportForm(forms.Form):
    CSV_COLUMNS = [
        "lrn", "first_name", "last_name", "middle_initial", "suffix",
        "birthdate", "sex", "email", "contact_number", "address",
        "guardian_name", "guardian_email", "guardian_contact_number",
    ]
    academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE),
        label="Academic Year",
    )
    section = forms.ModelChoiceField(queryset=Section.objects.none(), label="Section")
    csv_file = forms.FileField(label="CSV File")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)
        active_year = AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).first()
        if active_year:
            self.fields["section"].queryset = Section.objects.filter(
                status=Section.STATUS_ACTIVE,
                academic_year=active_year,
            )


class VCStudentsImportForm(forms.Form):
    csv_file = forms.FileField(
        label="CSV File",
        help_text="Columns: lrn OR email (one per row).",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)


def parse_csv_rows(uploaded):
    """Return a list of dict rows from an uploaded CSV, or None on error."""
    try:
        text_stream = io.TextIOWrapper(uploaded.file, encoding="utf-8-sig")
        reader = csv.DictReader(text_stream)
        rows = [row for row in reader if any((v or "").strip() for v in row.values())]
        return rows if rows else None
    except Exception:
        return None


class ChangePasswordForm(forms.Form):
    """User changes their own password (enforced against PasswordPolicy)."""
    current_password = forms.CharField(
        label="Current Password", widget=forms.PasswordInput
    )
    new_password = forms.CharField(
        label="New Password", widget=forms.PasswordInput
    )
    confirm_password = forms.CharField(
        label="Confirm New Password", widget=forms.PasswordInput
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)

    def clean(self):
        cleaned = super().clean()
        new_password = cleaned.get("new_password") or ""
        if cleaned.get("new_password") != cleaned.get("confirm_password"):
            self.add_error("confirm_password", "Passwords do not match.")
        if new_password and len(new_password) < 8:
            self.add_error("new_password", "Password must be at least 8 characters.")
        return cleaned


class AdminResetPasswordForm(forms.Form):
    """System administrator resets another user's password."""
    new_password = forms.CharField(
        label="New Password",
        widget=forms.PasswordInput,
        min_length=8,
        help_text="The user will be asked to use this password next sign-in and it will be recorded in password history.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_control(self.fields)


class RoleAssignmentForm(forms.Form):
    user = forms.ModelChoiceField(queryset=User.objects.none(), label="User")
    role = forms.ModelChoiceField(queryset=Role.objects.none(), label="Role")
    academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE),
        label="Academic Year",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.contrib.auth.models import Group
        apply_form_control(self.fields)
        self.fields["user"].queryset = User.objects.filter(
            status=User.Status.ACTIVE
        ).order_by("email")
        self.fields["role"].queryset = Role.objects.all().order_by("code")
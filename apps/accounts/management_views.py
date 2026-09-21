from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.academic_structure.models import AcademicYear, StudentEnrollment
from apps.system_settings.models import PasswordPolicy
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .decorators import perm_required
from .forms import (
    EmployeeAccountForm,
    EmployeeAccountUpdateForm,
    StudentAccountForm,
    StudentRecordUpdateForm,
    StudentRecordsImportForm,
    VCStudentsImportForm,
    AdminResetPasswordForm,
    RoleAssignmentForm,
    generate_password,
    parse_csv_rows,
)
from .models import (
    ActivityLog,
    EmployeeProfile,
    PasswordHistory,
    Role,
    RoleAssignment,
    StudentProfile,
    User,
)


def _deactivate_login_access(user):
    """Archive a user: prevent login and remove every role group."""
    user.status = User.Status.ARCHIVED
    user.save()
    user.groups.clear()


def _make_student(email, password, profile_fields, section, academic_year, request=None):
    """Create (or update) a student User + StudentProfile and enroll it."""
    email = (email or "").strip().lower()
    lrn = profile_fields.pop("lrn")
    if not email:
        email = f"{lrn}@student.tass.edu.ph"

    student = StudentProfile.objects.filter(lrn=lrn).first()
    if student is None:
        user = User(email=email, status=User.Status.PENDING_VERIFICATION)
        user.set_password(password or generate_password())
        user.password_changed_at = timezone.now()
        user.save()
        PasswordHistory.objects.create(user=user, hashed_password=user.password)
        if request is not None:
            from .views import _send_verification_email
            _send_verification_email(user, request)
        student = StudentProfile(user=user, lrn=lrn, **profile_fields)
        student.save()
        ActivityLog.objects.create(
            user=request.user if request else None,
            action="student_account_created",
            related_entity_type="User",
            related_entity_id=user.pk,
        )
    else:
        for field, value in profile_fields.items():
            setattr(student, field, value)
        student.user.email = email
        student.user.save()
        student.save()

    if section is not None and academic_year is not None:
        StudentEnrollment.objects.get_or_create(
            student=student,
            academic_year=academic_year,
            defaults={"section": section, "status": StudentEnrollment.STATUS_ENROLLED},
        )
    return student


@perm_required("view_employee_account")
def employee_account_list(request):
    employees = EmployeeProfile.objects.select_related("user").order_by(
        "last_name", "first_name"
    )
    context = {
        "employees": employees,
        "total_employees": employees.count(),
        "active_employees": employees.filter(
            user__status=User.Status.ACTIVE
        ).count(),
    }
    return render(request, "accounts/employee_account_list.html", context)


@perm_required("create_employee_account")
def employee_account_add(request):
    form = EmployeeAccountForm(request.POST or None)
    generated_password = None
    created_email = ""
    if request.method == "POST":
        form = EmployeeAccountForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"].strip().lower()
            initial_password = form.cleaned_data["initial_password"] or generate_password()
            if User.objects.filter(email=email).exists():
                form.add_error("email", "A user with this email already exists.")
            else:
                user = User(email=email, status=User.Status.PENDING_VERIFICATION)
                user.set_password(initial_password)
                user.password_changed_at = timezone.now()
                user.is_staff = form.cleaned_data.get("grant_staff_access", False)
                user.save()
                PasswordHistory.objects.create(user=user, hashed_password=user.password)
                from .views import _send_verification_email
                _send_verification_email(user, request)
                profile = form.save(commit=False)
                profile.user = user
                profile.save()
                ActivityLog.objects.create(
                    user=request.user, action="employee_account_created",
                    related_entity_type="User", related_entity_id=user.pk,
                )
                generated_password = initial_password
                created_email = email
                messages.success(request, f"Employee account created for {email}.")
                form = EmployeeAccountForm()

    context = {
        "form": form,
        "title": "Add Employee Account",
        "generated_password": generated_password,
        "created_email": created_email,
    }
    return render(request, "accounts/employee_account_form.html", context)


@perm_required("update_employee_account")
def employee_account_update(request, pk):
    profile = get_object_or_404(
        EmployeeProfile.objects.select_related("user"), pk=pk
    )
    form = EmployeeAccountUpdateForm(
        request.POST or None, instance=profile,
        initial={"email": profile.user.email},
    )
    if request.method == "POST":
        if form.is_valid():
            new_email = form.cleaned_data["email"].strip().lower()
            conflict = User.objects.filter(email=new_email).exclude(pk=profile.user_id).exists()
            if conflict:
                form.add_error("email", "A user with this email already exists.")
            else:
                profile.user.email = new_email
                profile.user.save()
                form.save()
                ActivityLog.objects.create(
                    user=request.user,
                    action="employee_account_updated",
                    related_entity_type="User",
                    related_entity_id=profile.user_id,
                )
                messages.success(request, f"Employee account updated for {profile.user.email}.")
                return redirect("accounts:employee_account_list")
    return render(
        request,
        "accounts/employee_account_form.html",
        {"form": form, "title": f"Update Employee – {profile}", "generated_password": None, "created_email": ""},
    )


@perm_required("archive_account")
def employee_account_archive(request, pk):
    profile = get_object_or_404(
        EmployeeProfile.objects.select_related("user"), pk=pk
    )
    if request.method == "POST":
        email = profile.user.email
        _deactivate_login_access(profile.user)
        ActivityLog.objects.create(
            user=request.user,
            action="employee_account_archived",
            related_entity_type="User",
            related_entity_id=profile.user_id,
        )
        messages.success(request, f"Account archived: {email}.")
    return redirect("accounts:employee_account_list")


@perm_required("view_student_record")
def student_record_list(request):
    students = StudentProfile.objects.select_related("user").prefetch_related(
        "enrollments__section"
    ).order_by("last_name", "first_name")

    def current_section(student):
        enrollment = student.enrollments.filter(
            status=StudentEnrollment.STATUS_ENROLLED,
        ).select_related("section").first()
        return enrollment.section if enrollment else None

    context = {
        "students": students,
        "with_section": [current_section(s) for s in students],
        "total_students": students.count(),
        "total_enrolled": StudentEnrollment.objects.filter(
            status=StudentEnrollment.STATUS_ENROLLED
        ).count(),
    }
    return render(request, "accounts/student_record_list.html", context)


@perm_required("create_student_profile_account")
def student_record_add(request):
    form = StudentAccountForm(request.POST or None)
    generated_password = None
    if request.method == "POST":
        form = StudentAccountForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            profile_fields = {
                "lrn": data["lrn"],
                "first_name": data["first_name"],
                "last_name": data["last_name"],
                "middle_initial": data.get("middle_initial", ""),
                "suffix": data.get("suffix", ""),
                "birthdate": data["birthdate"],
                "guardian_name": data.get("guardian_name", ""),
                "guardian_email": data.get("guardian_email", ""),
            }
            email = data.get("email")
            if data["initial_password"]:
                generated_password = data["initial_password"]
            else:
                generated_password = generate_password()
            student = _make_student(
                email,
                generated_password,
                profile_fields,
                data.get("section"),
                AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).first(),
                request=request,
            )
            messages.success(request, f"Student account created for {student.user.email}.")
            created_email = student.user.email
            form = StudentAccountForm()
            return render(
                request,
                "accounts/student_account_form.html",
                {
                    "form": form,
                    "title": "Add Student Account",
                    "generated_password": generated_password,
                    "created_email": created_email,
                },
            )

    context = {
        "form": form,
        "title": "Add Student Account",
        "generated_password": generated_password,
        "created_email": "",
    }
    return render(request, "accounts/student_account_form.html", context)


@perm_required("generate_student_account")
def student_record_generate(request):
    form = StudentAccountForm(request.POST or None)
    generated_password = None
    if request.method == "POST":
        form = StudentAccountForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            profile_fields = {
                "lrn": data["lrn"],
                "first_name": data["first_name"],
                "last_name": data["last_name"],
                "middle_initial": data.get("middle_initial", ""),
                "suffix": data.get("suffix", ""),
                "birthdate": data["birthdate"],
                "guardian_name": data.get("guardian_name", ""),
                "guardian_email": data.get("guardian_email", ""),
            }
            email = data.get("email")
            generated_password = data["initial_password"] or generate_password()
            student = _make_student(
                email,
                generated_password,
                profile_fields,
                data.get("section"),
                AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).first(),
                request=request,
            )
            messages.success(request, f"Generated student account for {student.user.email}.")
            created_email = student.user.email
            form = StudentAccountForm()
            return render(
                request,
                "accounts/student_account_form.html",
                {
                    "form": form,
                    "title": "Generate Student Account",
                    "generated_password": generated_password,
                    "created_email": created_email,
                },
            )

    return render(
        request,
        "accounts/student_account_form.html",
        {
            "form": form,
            "title": "Generate Student Account",
            "generated_password": generated_password,
            "created_email": "",
        },
    )


@perm_required("update_student_record")
def student_record_update(request, pk):
    student = get_object_or_404(
        StudentProfile.objects.select_related("user"), pk=pk
    )
    form = StudentRecordUpdateForm(
        request.POST or None,
        instance=student,
        initial={
            "email": student.user.email,
            "status": student.user.status,
        },
    )
    if request.method == "POST":
        if form.is_valid():
            new_email = form.cleaned_data["email"].strip().lower()
            conflict = User.objects.filter(email=new_email).exclude(pk=student.user_id).exists()
            if conflict:
                form.add_error("email", "A user with this email already exists.")
            else:
                student.user.email = new_email
                student.user.status = form.cleaned_data["status"]
                student.user.save()
                form.save()
                if form.cleaned_data["status"] == User.Status.ARCHIVED:
                    student.user.groups.clear()
                ActivityLog.objects.create(
                    user=request.user,
                    action="student_record_updated",
                    related_entity_type="User",
                    related_entity_id=student.user_id,
                )
                messages.success(request, f"Student record updated for {student.user.email}.")
                return redirect("accounts:student_record_list")
    return render(
        request,
        "accounts/student_account_form.html",
        {
            "form": form,
            "title": f"Update Student – {student}",
            "generated_password": None,
            "created_email": "",
        },
    )


@perm_required("archive_student_record")
def student_record_archive(request, pk):
    student = get_object_or_404(
        StudentProfile.objects.select_related("user"), pk=pk
    )
    if request.method == "POST":
        email = student.user.email
        _deactivate_login_access(student.user)
        student.enrollments.filter(status=StudentEnrollment.STATUS_ENROLLED).update(
            status=StudentEnrollment.STATUS_ARCHIVED,
        )
        ActivityLog.objects.create(
            user=request.user,
            action="student_record_archived",
            related_entity_type="User",
            related_entity_id=student.user_id,
        )
        messages.success(request, f"Student record archived: {email}.")
    return redirect("accounts:student_record_list")


@perm_required("bulk_import_student_records")
def student_records_import(request):
    form = StudentRecordsImportForm(request.POST or None, request.FILES or None)
    report = None
    if request.method == "POST":
        form = StudentRecordsImportForm(request.POST, request.FILES)
        if form.is_valid():
            rows = parse_csv_rows(form.cleaned_data["csv_file"])
            if not rows:
                form.add_error("csv_file", "CSV is empty or could not be parsed.")
                report = None
            else:
                academic_year = form.cleaned_data["academic_year"]
                section = form.cleaned_data["section"]
                created, skipped, errors = 0, 0, []
                for idx, row in enumerate(rows, start=2):
                    lrn = (row.get("lrn") or "").strip()
                    first = (row.get("first_name") or "").strip()
                    last = (row.get("last_name") or "").strip()
                    if not lrn or not first or not last:
                        errors.append(f"Row {idx}: missing lrn/last_name/first_name")
                        continue
                    if len(lrn) != 12 or not lrn.isdigit():
                        errors.append(f"Row {idx}: invalid LRN {lrn!r}")
                        continue
                    try:
                        birthdate = row.get("birthdate") or "2000-01-01"
                        sex_raw = (row.get("sex") or "").strip().upper()
                        profile_fields = {
                            "lrn": lrn,
                            "first_name": first,
                            "last_name": last,
                            "middle_initial": (row.get("middle_initial") or "").strip(),
                            "suffix": (row.get("suffix") or "").strip(),
                            "birthdate": birthdate,
                            "sex": {"M": "M", "1": "M", "MALE": "M", "F": "F", "2": "F", "FEMALE": "F"}.get(sex_raw, ""),
                            "contact_number": (row.get("contact_number") or "").strip(),
                            "address": (row.get("address") or "").strip(),
                            "guardian_name": (row.get("guardian_name") or "").strip(),
                            "guardian_email": (row.get("guardian_email") or "").strip(),
                            "guardian_contact_number": (row.get("guardian_contact_number") or "").strip(),
                        }
                        email = (row.get("email") or "").strip()
                        if email and User.objects.filter(email=email.lower()).exists():
                            skipped += 1
                            continue
                        _make_student(email, None, profile_fields, section, academic_year, request=request)
                        created += 1
                    except Exception as exc:
                        errors.append(f"Row {idx}: {exc}")
                report = {"created": created, "skipped": skipped, "errors": errors}
                if created:
                    messages.success(request, f"Imported {created} student record(s).")
                elif not errors:
                    messages.warning(request, "No new student records were imported.")
    context = {"form": form, "report": report}
    return render(request, "accounts/student_records_import.html", context)


@perm_required("bulk_import_students_to_vc")
def vc_students_import(request, vc_pk):
    vc = get_object_or_404(VirtualClassroom, pk=vc_pk)
    form = VCStudentsImportForm(request.POST or None, request.FILES or None)
    report = None
    if request.method == "POST":
        form = VCStudentsImportForm(request.POST, request.FILES)
        if form.is_valid():
            rows = parse_csv_rows(form.cleaned_data["csv_file"])
            if not rows:
                form.add_error("csv_file", "CSV is empty or could not be parsed.")
            else:
                enrolled, skipped, errors = 0, 0, []
                for idx, row in enumerate(rows, start=2):
                    student = None
                    email = (row.get("email") or "").strip().lower()
                    lrn = (row.get("lrn") or "").strip()
                    if email:
                        user = User.objects.filter(email=email).first()
                        student = getattr(user, "student_profile", None) if user else None
                    if student is None and lrn:
                        student = StudentProfile.objects.filter(lrn=lrn).first()
                    if student is None:
                        skipped += 1
                        continue
                    try:
                        _, created = VirtualClassroomEnrollment.objects.get_or_create(
                            virtual_classroom=vc,
                            student=student,
                            defaults={"status": VirtualClassroomEnrollment.STATUS_ACTIVE},
                        )
                        if created:
                            enrolled += 1
                        else:
                            skipped += 1
                    except Exception as exc:
                        errors.append(f"Row {idx}: {exc}")
                report = {"enrolled": enrolled, "skipped": skipped, "errors": errors}
                if enrolled:
                    messages.success(request, f"Enrolled {enrolled} student(s) into {vc}.")
                elif not errors:
                    messages.warning(request, "No new students were enrolled.")
    return render(
        request,
        "accounts/vc_students_import.html",
        {"form": form, "vc": vc, "report": report},
    )

def _password_reuse_allowed(user, new_value):
    from django.contrib.auth.hashers import check_password
    limit = max(PasswordPolicy.get_solo().reuse_limit or 0, 1)
    history = PasswordHistory.objects.filter(user=user).order_by("-created_at")[:limit]
    return not any(check_password(new_value, h.hashed_password) for h in history)


def _apply_admin_password_reset(user, new_password):
    user.password = make_password(new_password)
    user.password_changed_at = timezone.now()
    user.failed_login_attempts = 0
    if user.status == User.Status.LOCKED:
        user.status = User.Status.ACTIVE
    user.save(update_fields=["password", "password_changed_at", "failed_login_attempts", "status"])
    PasswordHistory.objects.create(user=user, hashed_password=user.password)
    ActivityLog.objects.create(
        user=None,
        action="admin_password_reset",
        related_entity_type="User",
        related_entity_id=user.pk,
    )


@perm_required("reset_account_password")
def admin_reset_password(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = AdminResetPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        new_password = form.cleaned_data["new_password"]
        policy = PasswordPolicy.get_solo()
        problems = []
        if len(new_password) < policy.minimum_length:
            problems.append(f"at least {policy.minimum_length} characters")
        if policy.require_uppercase and not any(c.isupper() for c in new_password):
            problems.append("an uppercase letter")
        if policy.require_lowercase and not any(c.islower() for c in new_password):
            problems.append("a lowercase letter")
        if policy.require_number and not any(c.isdigit() for c in new_password):
            problems.append("a number")
        if policy.require_symbol and not any(not c.isalnum() for c in new_password):
            problems.append("a symbol")
        if problems:
            form.add_error("new_password", "Password must include " + ", ".join(problems) + ".")
        elif not _password_reuse_allowed(user, new_password):
            form.add_error("new_password", "This password was used recently. Choose a different password.")
        else:
            _apply_admin_password_reset(user, new_password)
            messages.success(request, f"Password reset for {user.email}.")
            return redirect("accounts:employee_account_list")

    context = {"form": form, "target_user": user}
    return render(request, "accounts/admin_reset_password.html", context)


@perm_required("accounts.role_based_access_control")
def role_assignment_list(request):
    assignments = RoleAssignment.objects.select_related(
        "user", "role", "academic_year"
    ).order_by("-academic_year__start_date", "role__code")
    context = {
        "assignments": assignments,
        "form": RoleAssignmentForm(),
        "roles": Role.objects.all(),
    }
    return render(request, "accounts/role_assignment_list.html", context)


@perm_required("accounts.role_based_access_control")
def role_assignment_add(request):
    if request.method != "POST":
        return redirect("accounts:role_assignment_list")
    form = RoleAssignmentForm(request.POST)
    if form.is_valid():
        user = form.cleaned_data["user"]
        role = form.cleaned_data["role"]
        academic_year = form.cleaned_data["academic_year"]
        RoleAssignment.objects.update_or_create(
            user=user,
            role=role,
            academic_year=academic_year,
        )
        ActivityLog.objects.create(
            user=request.user,
            action="role_assignment_added",
            related_entity_type="RoleAssignment",
            related_entity_id=user.pk,
        )
        messages.success(
            request,
            f"Assigned role '{role.get_code_display()}' to {user.email} for {academic_year}.",
        )
    else:
        for error in form.errors.values():
            messages.error(request, " ".join(error))
    return redirect("accounts:role_assignment_list")


@perm_required("accounts.role_based_access_control")
def role_assignment_remove(request, pk):
    if request.method != "POST":
        return redirect("accounts:role_assignment_list")
    assignment = get_object_or_404(RoleAssignment, pk=pk)
    user_id = assignment.user_id
    assignment.delete()
    ActivityLog.objects.create(
        user=request.user,
        action="role_assignment_removed",
        related_entity_type="RoleAssignment",
        related_entity_id=user_id,
    )
    messages.success(request, "Role assignment removed.")
    return redirect("accounts:role_assignment_list")

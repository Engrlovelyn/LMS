"""
SVI LMS JSON write bridge.

Every SPA dashboard persists its state to localStorage. These endpoints give
the front-ends a real database sink that mirrors those localStorage payloads,
reconciling (upsert / soft-archive) instead of deleting, so the manuscript's
"historical records retained" rule holds everywhere.

All endpoints are login-required, POST, JSON. The front-end JS sends the same
objects it previously stored in localStorage.
"""

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import FileResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_POST, require_GET

from apps.accounts.models import (
    ActivityLog,
    EmailVerificationToken,
    EmployeeProfile,
    PasswordHistory,
    Role,
    RoleAssignment,
    SexChoices,
    StudentProfile,
    SuffixChoices,
    TeacherSubject,
    User,
    _sync_user_groups,
)
from apps.academic_structure.models import AcademicStructureVersion, AcademicYear, Section, StudentEnrollment, Subject
from apps.assessments.models import (
    Assessment,
    AssessmentAssignment,
    AssessmentAttempt,
    AssessmentPart,
    Submission,
)
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.communication.models import (
    ClassAnnouncement,
    GroupMessage,
    Notification,
    PrivateMessage,
    SchoolAnnouncement,
    SchoolEvent,
    TARGET_ALL,
    TARGET_ROLE_CHOICES,
    user_matches_target_role,
)
from apps.communication import notifications as notif_service
from apps.governance.models import RoleAccessReview
from apps.system_settings.models import BackupRecord, PasswordPolicy, SystemConfiguration
from apps.teaching_learning.models import LearningResource, LearningResourcePlacement, Topic, TopicTemplate
from apps.teaching_learning.utils import (
    human_size,
    resource_size_bytes,
    resource_size_label,
)
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment
from apps.virtual_classrooms.services import (
    create_virtual_classroom,
    normalize_grading_period,
    pick_section_teacher,
    resolve_section,
    resolve_subject,
)

STATUS_ACTIVE = "active"

USER_STATUS_BY_LABEL = {
    "Active": User.Status.ACTIVE,
    "active": User.Status.ACTIVE,
    "Pending Verification": User.Status.PENDING_VERIFICATION,
    "pending_verification": User.Status.PENDING_VERIFICATION,
    "Pending Password Setup": User.Status.PENDING_PASSWORD_SETUP,
    "pending_password_setup": User.Status.PENDING_PASSWORD_SETUP,
    "Locked": User.Status.LOCKED,
    "locked": User.Status.LOCKED,
    "Archived": User.Status.ARCHIVED,
    "archived": User.Status.ARCHIVED,
}

ROLE_CODE_BY_LABEL = {
    "System Administrator": Role.Code.SYSTEM_ADMIN,
    "System Admin": Role.Code.SYSTEM_ADMIN,
    "Registrar": Role.Code.REGISTRAR,
    "Teacher": Role.Code.TEACHER,
    "Teacher / Staff": Role.Code.TEACHER,
    "Principal": Role.Code.PRINCIPAL,
    "High School Principal": Role.Code.PRINCIPAL,
    "Elementary Principal": Role.Code.ELEMENTARY_PRINCIPAL,
    "Assistant Principal": Role.Code.ASSISTANT_PRINCIPAL,
    "Academic Coordinator": Role.Code.ASSISTANT_PRINCIPAL,
    "Student": Role.Code.STUDENT,
    "Quality Assurance": Role.Code.QUALITY_AUDIT,
}

MISSING_BIRTH_PLACEHOLDER = date(2005, 1, 1)


def _apply_status_change(user, new_status):
    """Move an account to a new status, clearing lock residue when reactivated.

    The roster boards let an administrator flip the status dropdown back to
    "Active", which is how a lockout was cleared before the dedicated unlock
    action existed. Writing ``status`` on its own left ``failed_login_attempts``
    sitting at the lockout threshold and ``locked_at`` stamped, so the next
    failed sign-in re-locked the account instantly and the dashboard kept
    reporting a stale lock date on an account that was plainly active.
    """
    previous = user.status
    user.status = new_status
    if new_status == User.Status.ACTIVE and previous == User.Status.LOCKED:
        user.failed_login_attempts = 0
        user.locked_at = None
        user.must_change_password = False
        user.save(
            update_fields=[
                "status",
                "failed_login_attempts",
                "locked_at",
                "must_change_password",
                "is_active",
            ]
        )
    else:
        user.save(update_fields=["status", "is_active"])
    return previous

# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
# Every endpoint in this module used to be `@login_required` only, which meant
# any signed-in account -- a student included -- could POST to the
# administrative endpoints below. The worst case was /api/accounts/, which
# creates users and RoleAssignment rows; the RoleAssignment post_save signal
# then added the matching django Group, so one crafted POST was enough to
# promote an attacker to System Administrator and load the full admin dashboard.
#
# Each view is now annotated with the role group(s) allowed to call it. The
# decorator is applied *below* @login_required, matching apps/accounts/decorators.

from functools import wraps

from apps.accounts.campus import (
    ELEMENTARY,
    HIGH_SCHOOL,
    WHOLE_SCHOOL,
    campus_label_for,
    campus_scope_for,
    can_post_to_campus,
    role_codes_for,
    user_can_read_grade,
)
from apps.accounts.account_admin import (
    AccountActionError,
    generate_temporary_password,
    issue_temporary_password,
    locked_accounts_queryset,
    serialize_locked_account,
    unlock_user_account,
)
from apps.accounts.decorators import has_permission, has_role
from apps.accounts.ordering import order_students


def api_role_required(*roles):
    """Restrict a JSON write endpoint to the given role groups.

    Superusers always pass. Unknown groups simply never match, so a role code
    that is not yet a Group denies rather than allows.
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return _err("Authentication required.", 401)
            if user.is_superuser:
                return view_func(request, *args, **kwargs)
            if has_role(user, *roles):
                return view_func(request, *args, **kwargs)
            _log(user, f"api_denied:{view_func.__name__}")
            return _err(
                "You do not have permission to perform this action.", 403
            )

        return wrapper

    return decorator


ADMIN_ROLES = (
    "system_admin",
    "registrar",
    "principal",
    "elementary_principal",
    "assistant_principal",
)

# Audiences a school-wide announcement may be published to. Mirrors
# `SchoolAnnouncement.audience`; kept here so the API can reject a bad value
# with a 400 instead of failing a database write.
VALID_AUDIENCES = (WHOLE_SCHOOL, ELEMENTARY, HIGH_SCHOOL)
VALID_TARGET_ROLES = {value for value, _label in TARGET_ROLE_CHOICES}


def _visible_target_roles(user):
    """Role audiences the caller may read (mirrors communication.views)."""
    roles = [TARGET_ALL]
    if user.is_superuser or user.is_staff:
        return [value for value, _label in TARGET_ROLE_CHOICES]
    if has_role(user, "teacher"):
        roles.append("teachers")
    elif getattr(user, "student_profile", None) is not None:
        roles.append("students")
    else:
        roles.extend(["students", "teachers"])
    return roles


def _is_staff_admin(user):
    if user.is_superuser:
        return True
    return has_role(user, *ADMIN_ROLES)


def api_perm_required(*codenames, allow_teacher=False):
    """Restrict a JSON write endpoint to holders of any of the given permissions.

    Falls back to the same superuser / group-permission resolution the page
    views use, so the dashboard and the JSON bridge agree on who may write.
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return _err("Authentication required.", 401)
            is_teacher = allow_teacher and "teacher" in role_codes_for(user)
            if any(has_permission(user, c) for c in codenames) or is_teacher:
                return view_func(request, *args, **kwargs)
            _log(user, f"api_denied:{view_func.__name__}")
            return _err(
                "You do not have permission to perform this action.", 403
            )

        return wrapper

    return decorator


# Roles that own platform administration.
#
# NOTE: this name was previously bound twice in this module and the second
# binding silently won, because decorators are applied top-to-bottom and
# `@api_role_required(*ADMIN_ROLES)` is evaluated when the `def` line runs. The
# narrow binding therefore governed every view decorated after it, which had
# quietly made the accounts reconcile endpoint system-admin-only. There is now
# one binding only; `ACADEMIC_LEADERSHIP_ROLES` covers the leadership set.
ADMIN_ROLES = (
    "system_admin",
    "registrar",
    "principal",
    "elementary_principal",
    "assistant_principal",
)

# Every role that supervises academics across a campus.
ACADEMIC_LEADERSHIP_ROLES = (
    "system_admin",
    "principal",
    "elementary_principal",
    "assistant_principal",
)

# Roles that manage student records and academic structure.
REGISTRAR_ROLES = ("system_admin", "registrar")
# Roles that teach and grade.
STAFF_ROLES = (
    "system_admin",
    "registrar",
    "principal",
    "elementary_principal",
    "assistant_principal",
    "teacher",
)


def _ok(**extra):
    return JsonResponse({"ok": True, **extra}, status=200)


def _err(message, status=400):
    return JsonResponse({"ok": False, "message": str(message)}, status=status)


def _payload(request):
    ct = request.content_type or ""
    if "multipart/form-data" in ct or "application/x-www-form-urlencoded" in ct:
        # `QueryDict.dict()` keeps only the last value for a repeated key, which
        # silently collapsed list fields (e.g. `roles`, `revoked_roles`) sent by
        # the form-encoded dashboard JS into a single string. Build the dict by
        # hand so a repeated key arrives as a real list.
        data = {}
        for key in request.POST.keys():
            values = request.POST.getlist(key)
            data[key] = values if len(values) > 1 else values[0]
        return data
    try:
        return json.loads(request.body or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _active_year():
    return AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).order_by("-start_date").first()


def _share_flag(data):
    value = data.get("share", data.get("share_to_repository", data.get("is_shared")))
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _log(user, action, entity_type="", entity_id=None):
    try:
        ActivityLog.objects.create(
            user=user, action=action,
            related_entity_type=entity_type, related_entity_id=entity_id,
        )
    except Exception:
        pass


def _generate_credentials(email):
    """Create a brand-new PENDING_VERIFICATION user with a generated password.

    Returns the (user, temp_password) pair so callers can send the welcome/
    verification mail after the employee/student profile (and thus the correct
    account kind) has been attached.
    """
    from apps.accounts.forms import generate_password

    temp_password = generate_password()
    user = User.objects.create_user(email=email, password=temp_password)
    user.status = User.Status.PENDING_VERIFICATION
    user.password_changed_at = timezone.now()
    user.save(update_fields=["status", "password_changed_at"])
    PasswordHistory.objects.create(user=user, hashed_password=user.password)
    return user, temp_password


def _send_welcome_mail(user, request, temp_password):
    """Email the credentials + verification link for a freshly created account.

    Returns the result dict from _send_verification_email (or {"ok": True} if
    there is no temp password to email).
    """
    if not temp_password:
        return {"ok": True}
    from apps.accounts.views import _send_verification_email

    return _send_verification_email(user, request, password=temp_password)


def _employee(user):
    try:
        return user.employee_profile
    except (EmployeeProfile.DoesNotExist, AttributeError):
        return None


def _student(user):
    try:
        return user.student_profile
    except (StudentProfile.DoesNotExist, AttributeError):
        return None


def _grade_number(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    try:
        return max(1, min(12, int(digits) or 7))
    except (ValueError, TypeError):
        return 7


def _parse_birth(value):
    if not value:
        return MISSING_BIRTH_PLACEHOLDER
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            from datetime import datetime

            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return MISSING_BIRTH_PLACEHOLDER


_TITLES = {"ms", "mrs", "mr", "dr", "prof", "sir", "ma'am", "maam", "fr", "engr", "eng", "atty", "hon", "rev", "tass"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _name_tokens(name):
    return [t.rstrip(".") for t in str(name or "").replace(",", " ").split() if t.rstrip(".")]


def _resolve_teacher(name):
    if not name or not str(name).strip() or str(name).strip() in ("—", "SVI Faculty", "TASS Admin"):
        return None
    target = [t.lower() for t in _name_tokens(name) if t.lower() not in _TITLES]
    if not target:
        return None
    for user in User.objects.filter(employee_profile__isnull=False).select_related("employee_profile"):
        if user.get_full_name() == name:
            return user
        candidate = [t.lower() for t in _name_tokens(user.get_full_name())]
        if candidate and candidate == target:
            return user
    return None


def _parse_name(name, first="", surname=""):
    first = (first or "").strip()
    surname = (surname or "").strip()
    parts = _name_tokens(name)
    if not parts and (first or surname):
        return first, "", surname
    if first and surname:
        return first, "", surname
    while parts and parts[0].lower() in _TITLES:
        parts.pop(0)
    suffix = ""
    if parts and parts[-1].lower() in _SUFFIXES:
        suffix = parts.pop()
    if not parts:
        return first, "", surname
    if len(parts) == 1:
        # A single token cannot be both the first and the last name; treat it as
        # the surname so "Madonna" does not become first_name="Madonna",
        # last_name="Madonna".
        if not surname:
            return first, "", parts[0]
        return surname, "", parts[0]
    first = first or parts[0]
    surname = surname or parts[-1]
    middle = ""
    if len(parts) > 2:
        token = parts[1]
        if len(token) <= 2 and token.isalpha():
            middle = token[0]
        else:
            middle = parts[-2][0] if len(parts) == 3 else ""
    if suffix:
        surname = f"{surname} {suffix.title()}" if len(surname.split()) == 1 else surname
    return first, middle, surname


def _employee_by_name(incoming):
    """Map dashboard name fields to actual User objects once per request."""
    if not incoming:
        return {}
    for user in User.objects.filter(employee_profile__isnull=False).select_related("employee_profile"):
        full = user.get_full_name()
        if full in incoming or full.lower() in incoming:
            incoming[full] = user
            incoming[full.lower()] = user
    return incoming


def _active_vc(user):
    """Best-effort: the teacher's most recently created active classroom."""
    return VirtualClassroom.objects.filter(teacher=user, status=STATUS_ACTIVE).order_by("-created_at").first()


# ---------------------------------------------------------------------------
# GET /api/csrf/  — obtain a CSRF cookie (called once by the front-end if the
# cookie is absent, so subsequent POSTs can carry X-CSRFToken).
# ---------------------------------------------------------------------------
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# POST /api/accounts/  — system admin employee accounts reconcile
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required(*ADMIN_ROLES)
def accounts(request):
    data = _payload(request)
    incoming = data.get("accounts") or data.get("items") or []
    if not isinstance(incoming, list):
        return _err("Expected a list under 'accounts'.")

    seen, created, updated, email_failed = [], 0, 0, 0
    # The sweep below only has meaning when the board is showing real DB
    # accounts. When the demo data set (emails/employee numbers that don't
    # exist in PostgreSQL) is synced, "updated" rows are the demo's fabrications
    # and archiving every genuine employee account would be destructive.
    # So we only sweep when every row reconciled to an existing user+employee.
    real_mode = True
    for row in incoming:
        if not isinstance(row, dict):
            continue
        email = (row.get("email") or row.get("acctUsername") or "").strip().lower()
        if not email:
            continue
        seen.append(email)
        first, mi, last = _parse_name(
            row.get("name") or "",
            first=row.get("first") or "",
            surname=row.get("surname") or row.get("last_name") or "",
        )
        emp_no = (row.get("emp") or row.get("employee_number") or "").strip()
        if not last:
            continue

        user = User.objects.filter(email=email).first()
        temp_password = ""
        if user is None:
            user, temp_password = _generate_credentials(email)
            created += 1
        else:
            updated += 1

        employee = _employee(user)
        if employee is None:
            candidate_emp = emp_no or f"EMP-{user.pk:04d}"
            if EmployeeProfile.objects.filter(employee_number=candidate_emp).exists():
                candidate_emp = f"{candidate_emp}-{user.pk}"
            employee = EmployeeProfile.objects.create(
                user=user,
                employee_number=candidate_emp,
                first_name=first,
                last_name=last,
                middle_initial=mi,
            )
        else:
            dirty = (
                employee.first_name != first
                or employee.last_name != last
                or employee.middle_initial != mi
                or (emp_no and employee.employee_number != emp_no)
            )
            if dirty:
                employee.first_name, employee.last_name, employee.middle_initial = first, last, mi
                if emp_no:
                    employee.employee_number = emp_no
                employee.save()

        status = USER_STATUS_BY_LABEL.get(row.get("status"))
        if status and user.status != status:
            _apply_status_change(user, status)

        # Roles. A user may legitimately hold more than one (Teacher +
        # Principal), so the payload may carry a `roles` list of labels/codes.
        # `role` is still accepted for the single-role boards. Assignments are
        # additive: only roles explicitly revoked via `revoked_roles` are
        # removed, so saving a form that only renders the primary role cannot
        # silently strip a user's other roles.
        wanted_codes = []
        raw_roles = row.get("roles")
        if isinstance(raw_roles, list):
            for item in raw_roles:
                code = None
                if isinstance(item, str):
                    code = ROLE_CODE_BY_LABEL.get(item.strip()) or item.strip()
                elif isinstance(item, dict):
                    code = item.get("code") or ROLE_CODE_BY_LABEL.get(
                        (item.get("label") or "").strip()
                    )
                if code:
                    wanted_codes.append(code)
        if not wanted_codes:
            single = ROLE_CODE_BY_LABEL.get((row.get("role") or "").strip())
            if single:
                wanted_codes.append(single)

        for code in dict.fromkeys(c for c in wanted_codes if c):
            role = Role.objects.filter(code=code).first()
            if role:
                RoleAssignment.objects.get_or_create(
                    user=user, role=role, academic_year=None
                )

        revoked = row.get("revoked_roles")
        if isinstance(revoked, list):
            revoke_codes = []
            for item in revoked:
                if isinstance(item, str):
                    revoke_codes.append(
                        ROLE_CODE_BY_LABEL.get(item.strip()) or item.strip()
                    )
            for code in dict.fromkeys(c for c in revoke_codes if c):
                RoleAssignment.objects.filter(user=user, role__code=code).delete()

        _log(request.user, f"account_saved:{email}", entity_type="User", entity_id=user.pk)
        if temp_password:
            if not _send_welcome_mail(user, request, temp_password).get("ok"):
                email_failed += 1

        if created or _employee(user) is None:
            real_mode = False
        elif emp_no and employee.employee_number != emp_no:
            real_mode = False

    # Soft-archive employee accounts the board no longer lists (never self).
    # Only in real mode: in demo mode the visible roster may be unrelated to
    # the DB records, so archiving would silently disable demo accounts.
    if real_mode:
        managed = Role.objects.values_list("code", flat=True)
        for user in User.objects.filter(groups__name__in=managed).exclude(pk=request.user.pk):
            if _employee(user) is None:
                continue
            if user.email not in seen and user.status != User.Status.ARCHIVED:
                user.status = User.Status.ARCHIVED
                user.save(update_fields=["status", "is_active"])
                RoleAssignment.objects.filter(user=user).delete()
                _log(request.user, f"account_archived:{user.email}", entity_type="User", entity_id=user.pk)

    if email_failed:
        return _ok(
            created=created,
            updated=updated,
            email_failed=email_failed,
            message=f"{email_failed} new account(s) were created but their verification email failed to send.",
        )
    return _ok(created=created, updated=updated)


# ---------------------------------------------------------------------------
# POST /api/accounts/create/  — create ONE employee account, properly
# ---------------------------------------------------------------------------
# The bulk `accounts()` reconcile above is a mirror of a whole localStorage
# board, and it treats "a row I did not receive" as "archive this employee".
# That makes it the wrong endpoint for the "Add Employee" form: a single-row
# post would look like a mass deletion. The modal also used to write to
# localStorage only, so a new employee existed in the browser and nowhere else
# -- and the "activation email sent" toast was printed before anything was
# ever sent.

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def _field_errors(email, first, last, mi, emp_no, contact, suffix):
    """Validate one employee payload the way the database would.

    Django does not check field lengths or regex validators on ``.save()``, so
    an over-long employee number reached Postgres and raised DataError *after*
    the User row had already been committed, leaving a half-built account.
    Returns a {field: message} dict; empty means valid.
    """
    errors = {}
    if not email:
        errors["email"] = "Email address is required."
    elif not _EMAIL_RE.match(email):
        errors["email"] = "Enter a valid email address, e.g. name@school.edu."
    elif len(email) > 254:
        errors["email"] = "Email address is too long."

    if not first:
        errors["first_name"] = "First name is required."
    elif len(first) > 50:
        errors["first_name"] = "First name must be 50 characters or fewer."

    if not last:
        errors["last_name"] = "Last name is required."
    elif len(last) > 50:
        errors["last_name"] = "Last name must be 50 characters or fewer."

    if mi:
        if len(mi) > 2:
            errors["middle_initial"] = "Middle initial must be a single letter."
        elif not re.match(r"^[A-Za-z]\.?$", mi):
            errors["middle_initial"] = "Middle initial must be a single letter, e.g. M."

    if not emp_no:
        errors["employee_number"] = "Employee ID is required."
    elif len(emp_no) > 15:
        errors["employee_number"] = (
            f"Employee ID must be 15 characters or fewer (got {len(emp_no)})."
        )
    elif not re.match(r"^[A-Za-z0-9._/-]+$", emp_no):
        errors["employee_number"] = (
            "Employee ID may only contain letters, numbers, dot, dash, slash and underscore."
        )

    if contact:
        if not re.match(r"^09\d{9}$", contact):
            errors["contact"] = (
                "Contact number must be an 11-digit PH mobile, e.g. 09171234567."
            )
    if suffix and suffix not in dict(SuffixChoices.choices):
        errors["suffix"] = "Suffix is not a valid option."
    return errors


@login_required
@require_POST
@api_role_required(*ADMIN_ROLES)
def account_create(request):
    """Create a single employee account and email its activation link.

    Unlike the roster reconcile this never archives anybody: it only ever adds.
    The User, EmployeeProfile and RoleAssignment are written in one
    transaction so a rejected field cannot leave a user with no profile.
    """
    year = _active_year()

    data = _payload(request)
    email = (data.get("email") or data.get("acctUsername") or "").strip().lower()
    first, mi, last = _parse_name(
        data.get("name") or "",
        first=data.get("first") or data.get("first_name") or "",
        surname=data.get("surname") or data.get("last") or data.get("last_name") or "",
    )
    mi = (mi or data.get("middle_initial") or "").strip()
    if mi and "." not in mi and len(mi) == 1:
        mi = mi + "."
    emp_no = (data.get("emp") or data.get("employee_number") or "").strip()
    contact = re.sub(r"[\s()-]", "", str(data.get("contact") or ""))
    suffix = (data.get("suffix") or "").strip()

    # Optional demographics, stored on EmployeeProfile so the Staff
    # Demographics panel has something real to plot. Both stay optional: an
    # unfilled field is stored as blank and the panel draws an em dash.
    sex_raw = str(data.get("sex") or data.get("gender") or "").strip()
    sex = {"male": "M", "female": "F"}.get(sex_raw.lower(), sex_raw.upper())
    birth_raw = str(data.get("birthdate") or data.get("birth") or "").strip()
    birthdate = None

    errors = _field_errors(email, first, last, mi, emp_no, contact, suffix)
    if sex not in ("", "M", "F"):
        errors["sex"] = "Sex must be Male or Female."
    if birth_raw:
        try:
            birthdate = date.fromisoformat(birth_raw)
        except ValueError:
            errors["birthdate"] = "Birthdate must be a date, e.g. 1990-05-04."
        else:
            if birthdate > date.today():
                errors["birthdate"] = "Birthdate cannot be in the future."
    if not errors:
        if User.objects.filter(email=email).exists():
            errors["email"] = "An account with this email already exists."
        elif emp_no and EmployeeProfile.objects.filter(employee_number=emp_no).exists():
            errors["employee_number"] = "This Employee ID is already in use."

    role_codes = []
    raw_roles = data.get("roles")
    if isinstance(raw_roles, list):
        for item in raw_roles:
            code = None
            if isinstance(item, str):
                code = ROLE_CODE_BY_LABEL.get(item.strip()) or item.strip()
            elif isinstance(item, dict):
                code = item.get("code") or ROLE_CODE_BY_LABEL.get(
                    (item.get("label") or "").strip()
                )
            if code:
                role_codes.append(code)
    if not role_codes:
        single = ROLE_CODE_BY_LABEL.get((data.get("role") or "").strip())
        if single:
            role_codes.append(single)
    if not role_codes:
        errors["role"] = "Select at least one role."

    known = [r.code for r in Role.objects.all()]
    for code in role_codes:
        if code not in known:
            errors["role"] = f"Unknown role '{code}'."
            break

    # Optional teacher subjects. Accept IDs from the active year only: a stale
    # subject from an archived year must not silently grant repository access.
    subject_ids = []
    raw_subjects = data.get("subjects")
    if isinstance(raw_subjects, list):
        for item in raw_subjects:
            value = item.get("id") if isinstance(item, dict) else item
            try:
                sid = int(value)
            except (TypeError, ValueError):
                continue
            if sid not in subject_ids:
                subject_ids.append(sid)
    valid_subject_ids = set()
    if year:
        valid_subject_ids = set(
            Subject.objects.filter(pk__in=subject_ids, academic_year=year).values_list(
                "id", flat=True
            )
        )
    if subject_ids and len(valid_subject_ids) != len(subject_ids):
        errors["subjects"] = "Select subjects from the active academic year."

    if errors:
        return JsonResponse(
            {
                "ok": False,
                "message": "Please correct the highlighted fields.",
                "errors": errors,
            },
            status=400,
        )

    # One transaction: a User without its EmployeeProfile/RoleAssignment is an
    # account nobody can ever finish verifying.
    from django.db import transaction

    try:
        with transaction.atomic():
            user, temp_password = _generate_credentials(email)
            EmployeeProfile.objects.create(
                user=user,
                employee_number=emp_no,
                first_name=first,
                last_name=last,
                middle_initial=mi,
                suffix=suffix,
                contact_number=contact,
                sex=sex,
                birthdate=birthdate,
            )
            assigned = []
            for code in dict.fromkeys(role_codes):
                role = Role.objects.filter(code=code).first()
                if role:
                    RoleAssignment.objects.get_or_create(
                        user=user, role=role, academic_year=None
                    )
                    assigned.append(role.code)
            if "teacher" in assigned:
                for sid in valid_subject_ids:
                    TeacherSubject.objects.get_or_create(
                        user=user, subject_id=sid
                    )
            _log(
                request.user,
                f"account_created:{email}",
                entity_type="User",
                entity_id=user.pk,
            )
    except Exception as exc:
        _log(request.user, f"account_create_failed:{email}")
        return _err(f"The account could not be created: {exc}", 500)

    mail = _send_welcome_mail(user, request, temp_password)
    mail_ok = bool(mail.get("ok"))
    _log(
        request.user,
        f"account_activation_email:{'sent' if mail_ok else 'failed'}:{email}",
        entity_type="User",
        entity_id=user.pk,
    )

    payload = {
        "user_id": user.pk,
        "email": email,
        "employee_number": emp_no,
        "roles": assigned,
        "subjects": sorted(valid_subject_ids),
        # Only ever echoed back to the admin who just created the account, so
        # a broken mail transport does not strand the employee with a password
        # nobody knows. It is never stored in plaintext.
        "temp_password": temp_password,
        "email_sent": mail_ok,
        "message": (
            f"Account created. An activation email with the temporary password "
            f"and verification link was sent to {email}."
            if mail_ok
            else (
                f"Account saved to the database, but the verification email could "
                f"NOT be sent. Share the temporary password manually: {temp_password}"
            )
        ),
    }
    if not mail_ok:
        payload["email_error"] = mail.get("message") or "Unknown mail error."
        payload["errors"] = {"email": "Verification email failed to send."}
    return _ok(**payload)


# ---------------------------------------------------------------------------
# POST /api/students/create/  — create ONE student account
# ---------------------------------------------------------------------------
# Companion to account_create above. The System Administrator's "Add User"
# modal only ever produced employees, so a learner account could only be made
# by the registrar's bulk roster import. This endpoint creates a single User +
# StudentProfile + Student RoleAssignment so the same modal can add a student.
# Enrollment is intentionally NOT performed here: grade/section is the
# registrar's job from the Students page.
def _student_field_errors(email, first, last, mi, lrn, birth_raw, contact, suffix):
    """Validate one student payload the way the database would.

    Mirrors ``_field_errors`` but uses the StudentProfile column rules: LRN is
    exactly 12 digits, birthdate is NOT NULL, and contact_number is a looser
    20-char free field rather than the employee PH-mobile constraint.
    """
    errors = {}
    if not email:
        errors["email"] = "Email address is required."
    elif not _EMAIL_RE.match(email):
        errors["email"] = "Enter a valid email address, e.g. name@school.edu."
    elif len(email) > 254:
        errors["email"] = "Email address is too long."

    if not first:
        errors["first_name"] = "First name is required."
    elif len(first) > 50:
        errors["first_name"] = "First name must be 50 characters or fewer."

    if not last:
        errors["last_name"] = "Last name is required."
    elif len(last) > 50:
        errors["last_name"] = "Last name must be 50 characters or fewer."

    if mi:
        if len(mi) > 2:
            errors["middle_initial"] = "Middle initial must be a single letter."
        elif not re.match(r"^[A-Za-z]\.?$", mi):
            errors["middle_initial"] = "Middle initial must be a single letter, e.g. M."

    if not lrn:
        errors["lrn"] = "LRN is required."
    elif not re.match(r"^\d{12}$", lrn):
        errors["lrn"] = "LRN must be exactly 12 digits."

    if not birth_raw:
        errors["birthdate"] = "Birthdate is required for a student account."

    if contact and len(contact) > 20:
        errors["contact"] = "Contact number must be 20 characters or fewer."

    if suffix and suffix not in dict(SuffixChoices.choices):
        errors["suffix"] = "Suffix is not a valid option."
    return errors


@login_required
@require_POST
@api_role_required(*REGISTRAR_ROLES)
def student_create(request):
    """Create a single student account and email its activation link.

    The User, StudentProfile and Student RoleAssignment are written in one
    transaction so a rejected field cannot leave a user with no profile. The
    account starts PENDING_VERIFICATION and the generated temporary password
    is echoed back to the creating administrator, exactly like an employee.
    """
    data = _payload(request)
    email = (data.get("email") or data.get("acctUsername") or "").strip().lower()
    first, mi, last = _parse_name(
        data.get("name") or "",
        first=data.get("first") or data.get("first_name") or "",
        surname=data.get("surname") or data.get("last") or data.get("last_name") or "",
    )
    mi = (mi or data.get("middle_initial") or "").strip()
    if mi and "." not in mi and len(mi) == 1:
        mi = mi + "."
    lrn = re.sub(
        r"\s+",
        "",
        str(data.get("lrn") or data.get("emp") or data.get("employee_number") or ""),
    )
    contact = re.sub(r"[\s()-]", "", str(data.get("contact") or ""))[:20]
    suffix = (data.get("suffix") or "").strip()
    address = (data.get("address") or "").strip()[:255]
    guardian_name = (data.get("guardian_name") or data.get("guardian") or "").strip()[:100]
    guardian_email = (data.get("guardian_email") or "").strip().lower()[:254]
    guardian_contact = (
        data.get("guardian_contact")
        or data.get("guardian_contact_number")
        or ""
    ).strip()[:20]

    sex_raw = str(data.get("sex") or data.get("gender") or "").strip()
    sex = {"male": "M", "female": "F"}.get(sex_raw.lower(), sex_raw.upper())
    birth_raw = str(data.get("birthdate") or data.get("birth") or "").strip()
    birthdate = None

    errors = _student_field_errors(email, first, last, mi, lrn, birth_raw, contact, suffix)
    if sex not in ("", "M", "F"):
        errors["sex"] = "Sex must be Male or Female."
    if birth_raw and "birthdate" not in errors:
        try:
            birthdate = date.fromisoformat(birth_raw)
        except ValueError:
            errors["birthdate"] = "Birthdate must be a date, e.g. 2012-05-04."
        else:
            if birthdate > date.today():
                errors["birthdate"] = "Birthdate cannot be in the future."
    if guardian_email and not _EMAIL_RE.match(guardian_email):
        errors["guardian_email"] = "Enter a valid guardian email address."

    if not errors:
        if User.objects.filter(email=email).exists():
            errors["email"] = "An account with this email already exists."
        elif lrn and StudentProfile.objects.filter(lrn=lrn).exists():
            errors["lrn"] = "This LRN is already in use."

    if errors:
        return JsonResponse(
            {
                "ok": False,
                "message": "Please correct the highlighted fields.",
                "errors": errors,
            },
            status=400,
        )

    from django.db import transaction

    try:
        with transaction.atomic():
            user, temp_password = _generate_credentials(email)
            StudentProfile.objects.create(
                user=user,
                lrn=lrn,
                first_name=first,
                last_name=last,
                middle_initial=mi,
                suffix=suffix,
                birthdate=birthdate,
                sex=sex,
                contact_number=contact,
                address=address,
                guardian_name=guardian_name,
                guardian_email=guardian_email,
                guardian_contact_number=guardian_contact,
            )
            role = Role.objects.filter(code=Role.Code.STUDENT).first()
            if role:
                RoleAssignment.objects.get_or_create(
                    user=user, role=role, academic_year=None
                )
            _log(
                request.user,
                f"student_account_created:{email}",
                entity_type="User",
                entity_id=user.pk,
            )
    except Exception as exc:
        _log(request.user, f"student_account_create_failed:{email}")
        return _err(f"The student account could not be created: {exc}", 500)

    mail = _send_welcome_mail(user, request, temp_password)
    mail_ok = bool(mail.get("ok"))
    _log(
        request.user,
        f"student_account_activation_email:{'sent' if mail_ok else 'failed'}:{email}",
        entity_type="User",
        entity_id=user.pk,
    )

    payload = {
        "user_id": user.pk,
        "email": email,
        "lrn": lrn,
        "roles": ["student"],
        # Echoed only to the administrator who created the account, so a broken
        # mail transport does not strand the learner without a password.
        "temp_password": temp_password,
        "email_sent": mail_ok,
        "message": (
            f"Student account created. An activation email with the temporary "
            f"password and verification link was sent to {email}."
            if mail_ok
            else (
                f"Student account saved to the database, but the verification email "
                f"could NOT be sent. Share the temporary password manually: "
                f"{temp_password}"
            )
        ),
    }
    if not mail_ok:
        payload["email_error"] = mail.get("message") or "Unknown mail error."
        payload["errors"] = {"email": "Verification email failed to send."}
    return _ok(**payload)


# ---------------------------------------------------------------------------
# POST /api/accounts/demographics/  — record an employee's sex/birthdate
# ---------------------------------------------------------------------------
# The Staff demographics panel is empty because EmployeeProfile.sex/birthdate
# were never editable from the admin dashboard. The Add Account modal sends
# them at creation, but existing staff (and the Edit Account modal) had no
# path to set or change them. This endpoint is that path: one employee, sex
# and birthdate, validated like the create form.
@login_required
@require_POST
@api_role_required(*ADMIN_ROLES)
def account_demographics(request):
    data = _payload(request)
    email = (str(data.get("email") or "")).strip().lower()
    user_id = data.get("user_id")
    user = None
    if user_id:
        user = User.objects.filter(pk=user_id).first()
    if user is None and email:
        user = User.objects.filter(email__iexact=email).first()
    if user is None:
        return _err("No account matches that user.", 404)

    employee = _employee(user)
    if employee is None:
        return _err("Selected account has no employee profile.", 409)

    sex_raw = str(data.get("sex") or "").strip()
    sex = {"male": "M", "female": "F", "m": "M", "f": "F"}.get(sex_raw.lower(), sex_raw.upper())
    if sex not in ("", "M", "F"):
        return _err("Sex must be Male or Female.", 400)

    birth_raw = str(data.get("birthdate") or "").strip()
    birthdate = None
    if birth_raw:
        try:
            birthdate = date.fromisoformat(birth_raw)
        except ValueError:
            return _err("Birthdate must be a date, e.g. 1990-05-04.", 400)
        if birthdate > date.today():
            return _err("Birthdate cannot be in the future.", 400)

    changed = (
        (employee.sex or "") != sex
        or (employee.birthdate or None) != birthdate
    )
    if changed:
        employee.sex = sex
        employee.birthdate = birthdate
        employee.save(update_fields=["sex", "birthdate"])
        _log(
            request.user,
            f"account_demographics:{user.email}",
            entity_type="User",
            entity_id=user.pk,
        )

    return _ok(
        ok=True,
        user_id=user.pk,
        email=user.email,
        sex=sex,
        birthdate=birthdate.isoformat() if birthdate else "",
        message=(
            "Staff demographics saved. The demographics panel updates on your next page load."
            if changed
            else "No changes were made to that record."
        ),
    )


def _resend_verification_for(user, request):
    """Issue a fresh temporary password and re-send the activation email.

    Only the very first send ever has the temporary password in plaintext, so a
    resend has to mint a new one. Otherwise a user whose original mail bounced
    (or was blocked by the provider) stays stuck at Pending Verification with a
    password nobody knows.

    Returns ``(ok, payload)``; ``payload`` always carries ``temp_password`` so
    the admin can still hand the credential over if the mail transport is down.
    """
    from apps.accounts.forms import generate_password

    if user.status == User.Status.ARCHIVED:
        return False, {
            "message": "That account is archived. Restore it before re-sending.",
            "status": 409,
        }
    if user.status != User.Status.PENDING_VERIFICATION:
        return False, {
            "message": (
                f"{user.email} is already verified and active. Use 'Forgot "
                f"password' if they cannot sign in."
            ),
            "status": 409,
        }

    temp_password = generate_password()
    user.password = make_password(temp_password)
    user.password_changed_at = timezone.now()
    user.failed_login_attempts = 0
    user.locked_at = None
    user.save(
        update_fields=[
            "password",
            "password_changed_at",
            "failed_login_attempts",
            "locked_at",
        ]
    )
    PasswordHistory.objects.create(user=user, hashed_password=user.password)
    # Supersede any earlier, never-used links so only this one works.
    EmailVerificationToken.objects.filter(
        user=user, verified_at__isnull=True
    ).update(verified_at=timezone.now())

    mail = _send_welcome_mail(user, request, temp_password)
    if not mail.get("ok"):
        _log(request.user, f"account_activation_email_failed:{user.email}")
        return False, {
            "message": mail.get("message") or "The email could not be sent.",
            "temp_password": temp_password,
            "email_sent": False,
            "status": 502,
        }

    _log(
        request.user,
        f"account_activation_email_resent:{user.email}",
        entity_type="User",
        entity_id=user.pk,
    )
    return True, {
        "email": user.email,
        "user_id": user.pk,
        "email_sent": True,
        "message": (
            f"Activation email re-sent to {user.email} with a new temporary "
            f"password."
        ),
    }


@login_required
@require_POST
@api_role_required(*ADMIN_ROLES)
def account_resend_verification(request):
    """Re-send an activation email for an account still awaiting verification."""
    data = _payload(request)
    email = (data.get("email") or "").strip().lower()
    if not email:
        return _err("Email address is required.", 400)

    user = User.objects.filter(email=email).first()
    if user is None:
        return _err(f"No account exists for {email}.", 404)

    ok, payload = _resend_verification_for(user, request)
    if not ok:
        return JsonResponse({"ok": False, **payload}, status=payload.get("status", 400))
    return _ok(**payload)


# ---------------------------------------------------------------------------
# POST /api/accounts/subjects/  — set one teacher's subjects for the active year
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required("system_admin")
def account_subjects(request):
    """Replace the subjects recorded on a teacher's account profile.

    ``subject_ids`` is the complete desired set, so unchecking a subject in the
    Edit Account modal removes it. Teaching access to subject-scoped assessment
    features is determined by the teacher's active Virtual Classrooms.
    """
    from django.db import transaction

    data = _payload(request)
    user_pk = data.get("user_id") or data.get("id")
    target = None
    if user_pk:
        target = (
            User.objects.filter(pk=user_pk).first()
            if str(user_pk).isdigit()
            else None
        )
    if target is None:
        email = (data.get("email") or "").strip().lower()
        target = User.objects.filter(email=email).first()
    if target is None:
        return _err("That user could not be found.", 404)

    year = _active_year()
    if not year:
        return _err("No active academic year configured yet.", 409)

    raw = data.get("subject_ids")
    if raw is None:
        raw = data.get("subjects")
    if not isinstance(raw, list):
        return _err("Expected a list of subject IDs under 'subject_ids'.")

    subject_ids = []
    for item in raw:
        value = item.get("id") if isinstance(item, dict) else item
        try:
            sid = int(value)
        except (TypeError, ValueError):
            continue
        if sid not in subject_ids:
            subject_ids.append(sid)

    subjects = list(
        Subject.objects.filter(pk__in=subject_ids, academic_year=year).order_by(
            "grade_level", "name"
        )
    )
    if len(subjects) != len(subject_ids):
        return _err(
            "One or more selected subjects are not in the active academic year.",
            400,
        )

    with transaction.atomic():
        TeacherSubject.objects.filter(user=target).exclude(
            subject_id__in=subject_ids
        ).delete()
        for subject in subjects:
            TeacherSubject.objects.get_or_create(user=target, subject=subject)
    _log(
        request.user,
        f"teacher_subjects_updated:{target.email}",
        entity_type="User",
        entity_id=target.pk,
    )
    return _ok(
        user_id=target.pk,
        email=target.email,
        subjects=[
            {"id": subject.id, "name": subject.name, "gradeLevel": subject.grade_level}
            for subject in subjects
        ],
        message="Subjects updated.",
    )


# ---------------------------------------------------------------------------
# POST /api/user-roles/  — set one user's persistent role set
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required(*ADMIN_ROLES)
def user_roles(request):
    """Replace a single user's persistent role set and nothing else.

    The role manager in the dashboard edits one person at a time, so it posts
    here rather than resending the whole account roster. That matters: the
    roster reconcile in ``accounts()`` treats a missing row as "archive this
    user", which is the wrong semantic for a role edit.

    ``roles`` is the complete desired set of role codes. Rows not listed are
    revoked, so the admin always sees exactly what will be stored. The
    RoleAssignment post_save signal keeps the matching Django Groups in step,
    which is what the page views and API guards check.
    """
    data = _payload(request)
    user_pk = data.get("user_id") or data.get("id")
    target = None
    if user_pk:
        target = User.objects.filter(pk=user_pk).first() if str(user_pk).isdigit() else None
        if target is None:
            target = User.objects.filter(email=str(user_pk).strip().lower()).first()
    else:
        target = User.objects.filter(email=str(data.get("email") or "").strip().lower()).first()
    if target is None:
        return _err("That user could not be found.", 404)

    raw = data.get("roles")
    if raw is None:
        raw = data.get("role_codes")
    if not isinstance(raw, list):
        return _err("Expected a list of role codes under 'roles'.")

    valid_codes = set(Role.objects.values_list("code", flat=True))
    wanted = []
    for item in raw:
        code = item.get("code") if isinstance(item, dict) else item
        if not isinstance(code, str):
            continue
        code = ROLE_CODE_BY_LABEL.get(code.strip()) or code.strip()
        if code in valid_codes and code not in wanted:
            wanted.append(code)

    if not wanted:
        return _err("Select at least one role for this user.")

    # A user must keep at least one role, and cannot escalate themselves out of
    # the only role that grants access to this endpoint.
    if target.pk == request.user.pk and "system_admin" not in wanted:
        return _err("You cannot remove your own System Administrator role.", 400)

    before = sorted(
        target.role_assignments.all()
        .values_list("role__code", flat=True)
    )

    keep_ids = list(
        Role.objects.filter(code__in=wanted).values_list("id", flat=True)
    )
    RoleAssignment.objects.filter(user=target).exclude(
        role_id__in=keep_ids
    ).delete()
    for role in Role.objects.filter(code__in=wanted):
        RoleAssignment.objects.get_or_create(
            user=target, role=role, academic_year=None
        )

    # The signal syncs groups per-assignment; re-sync once for the removals so
    # a revoked role cannot survive in the Group membership.
    _sync_user_groups(target)

    after = sorted(
        target.role_assignments.all()
        .values_list("role__code", flat=True)
    )
    if before != after:
        _log(
            request.user,
            f"roles_changed:{target.email}",
            entity_type="User",
            entity_id=target.pk,
        )

    return _ok(
        user_id=target.pk,
        email=target.email,
        roles=[
            {"code": c, "label": Role.objects.get(code=c).get_code_display()}
            for c in after
        ],
    )


# ---------------------------------------------------------------------------
# POST /api/students/  — registrar student records reconcile
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required(*REGISTRAR_ROLES)
def students(request):
    data = _payload(request)
    incoming = data.get("students") or data.get("items") or []
    if not isinstance(incoming, list):
        return _err("Expected a list under 'students'.")

    seen, created, updated, archived = [], 0, 0, 0
    email_failed = 0
    for row in incoming:
        if not isinstance(row, dict):
            continue
        lrn = str(row.get("lrn") or "").strip()
        last = (row.get("last") or row.get("last_name") or "").strip()
        first = (row.get("first") or row.get("first_name") or "").strip()
        if not lrn or not last:
            continue
        seen.append(lrn)

        email = (row.get("email") or row.get("acctUsername") or "").strip().lower()
        profile = StudentProfile.objects.filter(lrn=lrn).first()

        if profile is None:
            temp_password = ""
            if not email:
                email = f"{first.lower()}.{last.lower()}@sanvicente.edu.ph"
            existing = User.objects.filter(email=email).first()
            if existing is None:
                user, temp_password = _generate_credentials(email)
            else:
                user = existing
            profile = StudentProfile.objects.create(
                user=user,
                lrn=lrn,
                first_name=first,
                last_name=last,
                middle_initial=(row.get("middle") or "").rstrip(".")[0:1],
                birthdate=_parse_birth(row.get("birth") or row.get("birthdate")),
                sex="M" if row.get("gender") in ("Male", "M") else ("F" if row.get("gender") in ("Female", "F") else ""),
                address=(row.get("address") or "").strip(),
                contact_number=(row.get("contact") or "").strip(),
                guardian_name=(row.get("guardian") or "").strip(),
            )
            created += 1
            if temp_password and not _send_welcome_mail(user, request, temp_password).get("ok"):
                email_failed += 1
        else:
            user = profile.user
            updated += 1
            dirty = False
            if profile.first_name != first or profile.last_name != last:
                profile.first_name, profile.last_name = first, last
                dirty = True
            for field, key in (("middle_initial", "middle"), ("contact_number", "contact"), ("address", "address")):
                value = str(row.get(key) or "").strip()
                if value and getattr(profile, field) != value:
                    setattr(profile, field, value)
                    dirty = True
            if dirty:
                profile.save()

        if email and user.email != email and not User.objects.filter(email=email).exclude(pk=user.pk).exists():
            user.email = email
            user.save(update_fields=["email"])

        status = USER_STATUS_BY_LABEL.get(row.get("status"))
        if status and user.status != status:
            _apply_status_change(user, status)

        section_name = (row.get("section") or "").strip()
        grade = _grade_number(row.get("grade"))
        archived_row = (row.get("status") or "") == "Archived" or status == User.Status.ARCHIVED

        if section_name and not archived_row:
            # Accept "Grade 7 - Faith", "Grade 11 – STEM-A", or a bare "Section A".
            sec_name = section_name
            if " - " in section_name or " – " in section_name:
                left, right = section_name.split(" - " if " - " in section_name else " – ", 1)
                if any(ch.isdigit() for ch in left):
                    grade = _grade_number(left)
                sec_name = right.strip()
            section = Section.objects.filter(
                academic_year=year, grade_level=grade, name=sec_name
            ).first()
            if section is None:
                section = Section.objects.create(academic_year=year, name=sec_name, grade_level=grade)
            if not StudentEnrollment.objects.filter(student=profile, academic_year=year).exists():
                StudentEnrollment.objects.create(student=profile, academic_year=year, section=section)
            else:
                StudentEnrollment.objects.filter(student=profile, academic_year=year).update(
                    section=section, status=StudentEnrollment.STATUS_ENROLLED, archived_at=None
                )
            from apps.virtual_classrooms.services import sync_section_vc_enrollments
            # Enroll in all active VCs of this section and drop enrollments in
            # VCs from the student's previous section/grade.
            sync_section_vc_enrollments(profile, year)

        if archived_row:
            archived += 1
            user.status = User.Status.ARCHIVED
            user.save(update_fields=["status", "is_active"])
            StudentEnrollment.objects.filter(student=profile, academic_year=year).update(
                status=StudentEnrollment.STATUS_ARCHIVED, archived_at=timezone.now()
            )
            VirtualClassroomEnrollment.objects.filter(student=profile, status=VirtualClassroomEnrollment.STATUS_ACTIVE).update(
                status=VirtualClassroomEnrollment.STATUS_REMOVED, removed_at=timezone.now()
            )

        _log(request.user, f"student_saved:{lrn}", entity_type="StudentProfile", entity_id=profile.pk)

    if email_failed:
        return _ok(
            created=created,
            updated=updated,
            archived=archived,
            email_failed=email_failed,
            message=f"{email_failed} new student account(s) were created but their verification email failed to send.",
        )
    return _ok(created=created, updated=updated, archived=archived)


# ---------------------------------------------------------------------------
# POST /api/academic-structure/  — sections / subjects / virtual classrooms
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required(*REGISTRAR_ROLES)
def academic_structure(request):
    year = _active_year()
    if not year:
        return _err("No active academic year configured yet.", 409)
    data = _payload(request)
    result = {"sections": 0, "subjects": 0, "topics": 0, "topics_skipped": 0, "vcs": 0, "vcs_skipped": 0, "vcs_errors": []}

    for row in data.get("sections") or []:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip()
        grade = _grade_number(row.get("grade"))
        if not name:
            continue
        active = (row.get("status") or "Active") in ("active", "Active")
        sec = Section.objects.filter(academic_year=year, grade_level=grade, name=name).first()
        if sec is None:
            sec = Section.objects.create(academic_year=year, name=name, grade_level=grade,
                                         status=Section.STATUS_ACTIVE if active else Section.STATUS_ARCHIVED)
        else:
            sec.status = Section.STATUS_ACTIVE if active else Section.STATUS_ARCHIVED
            adviser = _resolve_teacher(row.get("adviser"))
            if adviser:
                sec.adviser = adviser
            sec.save()
        result["sections"] += 1

    for row in data.get("subjects") or []:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip()
        grade = _grade_number(row.get("grade"))
        if not name:
            continue
        period = normalize_grading_period(
            row.get("period") or row.get("grading_period") or ""
        )
        active = (row.get("status") or "Active") in ("active", "Active")
        subj = Subject.objects.filter(academic_year=year, name=name, grade_level=grade).first()
        if subj is None:
            subj = Subject.objects.create(
                academic_year=year, name=name, grade_level=grade, grading_period=period,
                status=Subject.STATUS_ACTIVE if active else Subject.STATUS_ARCHIVED,
            )
        else:
            subj.status = Subject.STATUS_ACTIVE if active else Subject.STATUS_ARCHIVED
            subj.subject_type = Subject.TYPE_CORE if (row.get("type") or "Applied") == "Applied" else Subject.TYPE_ELECTIVE
            subj.save()
        result["subjects"] += 1

    topics_payload = data.get("topics") or {}
    if isinstance(topics_payload, dict):
        for subject_name, topic_list in topics_payload.items():
            subj = Subject.objects.filter(academic_year=year, name=subject_name).first()
            if subj is None:
                result["topics_skipped"] += len(topic_list or [])
                continue
            for topic_name in topic_list or []:
                if not topic_name:
                    continue
                m = TopicTemplate.objects.filter(subject=subj).aggregate(m=Max("order"))["m"]
                order = 0 if m is None else m + 1
                if TopicTemplate.objects.filter(subject=subj, name=topic_name).exists():
                    continue
                TopicTemplate.objects.create(subject=subj, name=topic_name, order=order)
                result["topics"] += 1

    vc_rows = data.get("vcs") or data.get("virtualClassrooms") or []
    for row in vc_rows:
        if not isinstance(row, dict):
            continue
        subject_name = (row.get("subject") or "").strip()
        section_name = (row.get("section") or "").strip()
        raw_grade = row.get("grade")
        grade = _grade_number(raw_grade) if raw_grade not in (None, "") else None
        if not subject_name or not section_name:
            result["vcs_skipped"] += 1
            result["vcs_errors"].append(
                "A virtual classroom needs both a subject and a section."
            )
            continue
        subj = resolve_subject(
            year,
            subject_id=row.get("subject_id") or row.get("subjectId"),
            name=subject_name,
            grade=grade,
        )
        # Resolve the section by id when supplied, otherwise by name (and the
        # grade the dashboard sent). Matching on name alone could pick a
        # same-named section from another grade; the grade check below then
        # reported a mismatch and the classroom was skipped.
        sec = resolve_section(
            year,
            section_id=row.get("section_id") or row.get("sectionId"),
            name=section_name,
            grade=grade,
        )
        if subj is None:
            result["vcs_skipped"] += 1
            result["vcs_errors"].append(
                "Subject '{}' does not exist for {}.".format(
                    subject_name, year.label
                )
            )
            continue
        if sec is None:
            result["vcs_skipped"] += 1
            result["vcs_errors"].append(
                "Section '{}' does not exist for {}.".format(section_name, year.label)
            )
            continue
        if sec.grade_level != subj.grade_level:
            result["vcs_skipped"] += 1
            result["vcs_errors"].append(
                "Section '{}' is Grade {} but the subject is Grade {}.".format(
                    sec.name, sec.grade_level, subj.grade_level
                )
            )
            continue
        period = normalize_grading_period(
            row.get("gradingPeriod") or row.get("grading_period") or ""
        )
        vc = VirtualClassroom.objects.filter(subject=subj, section=sec, academic_year=year, grading_period=period).first()
        teacher = _resolve_teacher(row.get("teacher"))
        # One teacher per virtual classroom. A bulk import shouldn't abort on a
        # clash, so fall back to an available teacher for that section.
        teacher = pick_section_teacher(sec, preferred=teacher, exclude_classroom=vc)
        if vc is None:
            vc = VirtualClassroom.objects.create(
                subject=subj, section=sec, academic_year=year, grading_period=period, teacher=teacher,
                theme_color=(row.get("color") or row.get("themeColor") or "#4F46E5"),
                schedule=(row.get("schedule") or row.get("teacherSchedule") or row.get("classSchedule") or "").strip(),
            )
        else:
            if teacher and vc.teacher_id != teacher.pk:
                vc.teacher = teacher
            color = row.get("color") or row.get("themeColor")
            if color:
                vc.theme_color = color
            sched = row.get("schedule") or row.get("teacherSchedule") or row.get("classSchedule")
            if sched is not None:
                vc.schedule = str(sched).strip()
            active = (row.get("status") or "active") in ("active", "Active")
            vc.status = STATUS_ACTIVE if active else STATUS_ARCHIVED
            vc.save()
        # Bring a brand-new classroom up to spec: pre-populate its Topic
        # outline from the subject's TopicTemplate(s) and auto-enroll the
        # section's students for core subjects. Idempotent, so it is safe on
        # the update path too.
        from apps.virtual_classrooms.services import populate_classroom_defaults
        populate_classroom_defaults(vc, auto_enroll=True)
        result["vcs"] += 1

    return _ok(**result)


# ---------------------------------------------------------------------------
# POST /api/virtual-classroom/create/  — create one Virtual Classroom
# ---------------------------------------------------------------------------
def _vc_serialize(vc):
    return {
        "id": vc.id,
        "subject": vc.subject.name if vc.subject_id else "",
        "subjectId": vc.subject_id,
        "section": vc.section.name if vc.section_id else "",
        "sectionId": vc.section_id,
        "grade": "Grade {}".format(vc.section.grade_level) if vc.section_id else "",
        "teacher": vc.teacher.get_full_name() if vc.teacher_id else "",
        "teacherId": vc.teacher_id,
        "gradingPeriod": vc.grading_period,
        "schedule": vc.schedule,
        "themeColor": vc.theme_color,
        "year": vc.academic_year.label if vc.academic_year_id else "",
        "status": vc.get_status_display(),
    }


@login_required
@require_POST
def virtual_classroom_create(request):
    """Create (or idempotently update) a single Virtual Classroom.

    Unlike ``/api/academic-structure/`` -- which reconciles bulk dashboards --
    this endpoint validates every required field and reports *why* a creation
    was rejected, so a dashboard can show a real error instead of silently
    dropping the classroom. It is open to anyone holding the
    ``create_virtual_classroom`` permission (plus the roles whose dashboards
    own academic structure), which is what lets an authorized teacher create
    a classroom, not just a registrar.
    """
    user = request.user
    allowed = (
        user.is_superuser
        or has_permission(user, "virtual_classrooms.create_virtual_classroom")
        or user.groups.filter(
            name__in=("system_admin", "registrar", "assistant_principal")
        ).exists()
    )
    if not allowed:
        _log(user, "api_denied:virtual_classroom_create")
        return _err("You do not have permission to perform this action.", 403)

    data = _payload(request)

    # Academic year: explicit id/label wins, otherwise the active year.
    year = None
    raw_year = (
        data.get("academic_year_id")
        or data.get("academicYearId")
        or data.get("academic_year")
        or data.get("ay")
        or data.get("year")
    )
    if raw_year not in (None, ""):
        if str(raw_year).isdigit():
            year = AcademicYear.objects.filter(pk=int(raw_year)).first()
        else:
            label = (
                str(raw_year)
                .replace("S.Y.", "")
                .replace("SY", "")
                .replace("–", "-")
                .replace("—", "-")
            )
            label = "".join(label.split())
            year = AcademicYear.objects.filter(label=label).first()
    if year is None:
        year = _active_year()
    if year is None:
        return _err("No active academic year is configured yet.", 409)

    grade = data.get("grade")
    if grade in (None, ""):
        grade = None

    section = resolve_section(
        year,
        section_id=data.get("section_id") or data.get("sectionId"),
        name=data.get("section") or data.get("sectionName") or "",
        grade=grade,
    )
    subject = resolve_subject(
        year,
        subject_id=data.get("subject_id") or data.get("subjectId"),
        name=data.get("subject") or data.get("subjectName") or "",
        grade=grade,
    )
    if section is None:
        return _err(
            "Select a valid section for {}.".format(year.label)
        )
    if subject is None:
        return _err(
            "Select a valid subject for {}.".format(year.label)
        )

    teacher = None
    teacher_id = data.get("teacher_id") or data.get("teacherId")
    if teacher_id not in (None, ""):
        try:
            teacher = User.objects.filter(
                pk=int(teacher_id), groups__name="teacher", is_active=True
            ).first()
        except (TypeError, ValueError):
            teacher = None
        if teacher is None:
            return _err("The selected teacher could not be found.")
    raw_teacher = str(data.get("teacher") or "").strip()
    if teacher is None and raw_teacher:
        teacher = _resolve_teacher(raw_teacher)
        if teacher is None:
            return _err(
                "The teacher '{}' could not be found.".format(raw_teacher)
            )

    existing_classroom = VirtualClassroom.objects.filter(
        academic_year=year,
        section=section,
        subject=subject,
        grading_period=normalize_grading_period(
            data.get("gradingPeriod") or data.get("grading_period") or data.get("period") or ""
        ),
    ).first()
    old_teacher_id = existing_classroom.teacher_id if existing_classroom else None
    try:
        vc, created = create_virtual_classroom(
            academic_year=year,
            section=section,
            subject=subject,
            teacher=teacher,
            grading_period=(
                data.get("gradingPeriod")
                or data.get("grading_period")
                or data.get("period")
                or ""
            ),
            schedule=(
                data.get("schedule")
                or data.get("teacherSchedule")
                or data.get("classSchedule")
                or ""
            ),
            theme_color=data.get("color") or data.get("themeColor"),
        )
    except ValidationError as exc:
        return _err("; ".join(exc.messages))

    if created or (vc.teacher_id and vc.teacher_id != old_teacher_id):
        try:
            from apps.communication.notifications import notify_teacher_classroom_assignment

            notify_teacher_classroom_assignment(vc, assigned_by=user)
        except Exception:
            pass

    _log(
        user,
        "virtual_classroom_created" if created else "virtual_classroom_updated",
        entity_type="VirtualClassroom",
        entity_id=vc.pk,
    )
    return _ok(
        created=created,
        virtualClassroom=_vc_serialize(vc),
        message=(
            "Virtual classroom created successfully."
            if created
            else "Virtual classroom updated successfully."
        ),
    )


# ---------------------------------------------------------------------------
# GET/POST /api/academic-year/  — manage school years (System Admin / Registrar)
# ---------------------------------------------------------------------------
@login_required
@api_role_required("system_admin", "registrar", "quality_audit")
def academic_year(request):
    def _grading(year):
        v = getattr(year, "structure_version", None)
        if v is not None and getattr(v, "grading_period_type", None):
            return {
                "code": v.grading_period_type,
                "label": v.get_grading_period_type_display(),
            }
        return None

    def _years_json():
        years = (
            AcademicYear.objects.select_related("structure_version")
            .annotate(
                sect_count=Count("sections", distinct=True),
                subj_count=Count("subjects", distinct=True),
                enroll_count=Count("student_enrollments", distinct=True),
                vc_count=Count("virtual_classrooms", distinct=True),
            )
            .order_by("-start_date")
        )
        return [
            {
                "id": y.id,
                "label": y.label,
                "start": y.start_date.isoformat(),
                "end": y.end_date.isoformat(),
                "status": "Active" if y.status == AcademicYear.STATUS_ACTIVE else "Archived",
                "sections": y.sect_count,
                "subjects": y.subj_count,
                "enrolled": y.enroll_count,
                "classrooms": y.vc_count,
                "gradingPeriod": _grading(y),
            }
            for y in years
        ]

    def _normalize_grading(value):
        code = str(value or "").upper().strip()[:12]
        if code not in dict(AcademicStructureVersion.GRADING_PERIOD_TYPE_CHOICES):
            code = AcademicStructureVersion.QUARTER
        return code

    current = _active_year()

    if request.method == "GET":
        return _ok(years=_years_json(), active=(current.label if current else None))

    data = _payload(request)
    action = (data.get("action") or "create").strip().lower()

    if action == "create":
        label = (data.get("label") or "").strip()
        start_raw = (data.get("start") or "").strip()
        end_raw = (data.get("end") or "").strip()
        if not label or not start_raw or not end_raw:
            return _err("Label, start date, and end date are all required.")
        if AcademicYear.objects.filter(label=label).exists():
            return _err(f"School Year {label} already exists.")
        try:
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
        except ValueError:
            return _err("Dates must use the YYYY-MM-DD format.")
        if end <= start:
            return _err("End date must be after the start date.")
        # Activating the new year archives the previous active one, keeping the
        # model's "only one active AcademicYear" invariant intact.
        AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).update(
            status=AcademicYear.STATUS_ARCHIVED
        )
        grading = _normalize_grading(data.get("gradingPeriodType") or data.get("grading_period_type"))
        try:
            year = AcademicYear.objects.create(
                label=label, start_date=start, end_date=end,
                status=AcademicYear.STATUS_ACTIVE,
            )
            AcademicStructureVersion.objects.create(
                academic_year=year, grading_period_type=grading,
            )
        except ValidationError as exc:
            return _err("; ".join(exc.messages))
        _log(request.user, "academic_year_created", entity_type="AcademicYear", entity_id=year.pk)
        return _ok(years=_years_json(), active=label)

    if action == "rollover":
        from apps.academic_structure.services import rollover_year

        source = AcademicYear.objects.filter(
            pk=data.get("sourceId") or data.get("source_id")
        ).first()
        if source is None:
            return _err("Choose the school year to copy data from.", 400)
        target = AcademicYear.objects.filter(
            pk=data.get("targetId") or data.get("target_id")
        ).first() or current
        if target is None:
            return _err("No active school year to roll the data into. Create one first.", 409)
        try:
            report = rollover_year(source, target)
        except ValidationError as exc:
            return _err("; ".join(exc.messages))
        _log(
            request.user, "academic_year_rolled_over",
            entity_type="AcademicYear", entity_id=target.pk,
        )
        return _ok(
            years=_years_json(),
            active=(current.label if current else None),
            rollover={
                "source": source.label,
                "target": target.label,
                "sections": report.sections,
                "subjects": report.subjects,
                "classes": report.classes,
                "skipped": report.skipped,
            },
        )

    year = AcademicYear.objects.filter(pk=data.get("id")).first()
    if year is None:
        return _err("That school year no longer exists.", 404)

    if action == "archive":
        if year.status == AcademicYear.STATUS_ACTIVE:
            return _err("Cannot archive the active school year. Create a new S.Y. or activate another one first.")
        year.status = AcademicYear.STATUS_ARCHIVED
        year.save(update_fields=["status"])
        _log(request.user, "academic_year_archived", entity_type="AcademicYear", entity_id=year.pk)
        return _ok(years=_years_json(), active=(current.label if current else None))

    if action == "activate":
        AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).exclude(pk=year.pk).update(
            status=AcademicYear.STATUS_ARCHIVED
        )
        year.status = AcademicYear.STATUS_ACTIVE
        year.save(update_fields=["status"])
        _log(request.user, "academic_year_activated", entity_type="AcademicYear", entity_id=year.pk)
        return _ok(years=_years_json(), active=year.label)

    if action == "grading":
        grading = _normalize_grading(data.get("gradingPeriodType") or data.get("grading_period_type"))
        version, _ = AcademicStructureVersion.objects.get_or_create(
            academic_year=year,
            defaults={"grading_period_type": grading},
        )
        version.grading_period_type = grading
        version.save(update_fields=["grading_period_type"])
        _log(request.user, "academic_year_grading_set", entity_type="AcademicYear", entity_id=year.pk)
        return _ok(years=_years_json(), active=year.label)

    return _err("Unknown action. Use create, activate, archive, grading, or rollover.")


# ---------------------------------------------------------------------------
# POST /api/structure/  — add one Section or Subject to the active school year
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required(*REGISTRAR_ROLES)
def structure(request):
    year = _active_year()
    if not year:
        return _err("No active academic year configured yet.", 409)
    data = _payload(request)
    kind = (data.get("kind") or data.get("type") or "").strip().lower()

    if kind == "section":
        name = (data.get("name") or "").strip()
        grade = _grade_number(data.get("grade"))
        if not name:
            return _err("Section name is required.")
        if Section.objects.filter(academic_year=year, grade_level=grade, name=name).exists():
            return _err(f"Section '{name}' already exists in Grade {grade} for {year.label}.")
        adviser = _resolve_teacher(data.get("adviser"))
        try:
            sec = Section.objects.create(
                academic_year=year, name=name, grade_level=grade, adviser=adviser,
                status=Section.STATUS_ACTIVE,
            )
        except ValidationError as exc:
            return _err("; ".join(exc.messages))
        _log(request.user, "section_created", entity_type="Section", entity_id=sec.pk)
        return _ok(section={
            "id": sec.id, "name": sec.name, "gradeLevel": sec.grade_level, "year": year.label,
            "status": "Active", "adviser": (adviser.get_full_name() if adviser else None),
            "classrooms": 0, "students": 0,
        })

    if kind == "subject":
        name = (data.get("name") or "").strip()
        grade = _grade_number(data.get("grade"))
        if not name:
            return _err("Subject name is required.")
        period = (data.get("period") or data.get("grading_period") or "").strip()
        if period in ("Whole Academic Year", "Whole Year", "Quarter", "Semester", "1st Quarter"):
            period = ""
        s_type = Subject.TYPE_ELECTIVE if (data.get("type") or "") == "ELECTIVE" else Subject.TYPE_CORE
        if Subject.objects.filter(academic_year=year, name=name, grade_level=grade, grading_period=period).exists():
            return _err(f"Subject '{name}' already exists in Grade {grade} for {year.label}.")
        try:
            subj = Subject.objects.create(
                academic_year=year, name=name, grade_level=grade, grading_period=period,
                subject_type=s_type, status=Subject.STATUS_ACTIVE,
            )
        except ValidationError as exc:
            return _err("; ".join(exc.messages))
        _log(request.user, "subject_created", entity_type="Subject", entity_id=subj.pk)
        return _ok(subject={
            "id": subj.id, "name": subj.name, "gradeLevel": subj.grade_level, "year": year.label,
            "subjectType": "Applied" if s_type == Subject.TYPE_CORE else "Elective",
            "gradingPeriod": period, "status": "Active", "classrooms": 0, "assessments": 0,
        })

    return _err("Unsupported creation kind. Expected 'section' or 'subject'.")


# ---------------------------------------------------------------------------
# POST /api/school-announcements/ — school-wide feed (principal)
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required(*ACADEMIC_LEADERSHIP_ROLES)
def school_announcements(request):
    """Post school-wide announcements, scoped to the author's campus.

    The school runs two campuses. An announcement carries an ``audience``:

    ``whole_school``
        Both campuses and the office see it. Any principal may post one.
    ``elementary`` / ``high_school``
        Only that campus. An Elementary Principal posting an elementary notice
        therefore never reaches a High School dashboard, and vice versa.

    A request may name its own audience, but only one the author is actually
    allowed to post to. An Elementary Principal asking for ``high_school`` gets
    a 403 rather than a silent fallback to the whole school -- a quietly
    re-scoped notice would reach people the author never intended to address.
    The author's resolved scope is stamped on the row so an audit reader can
    tell which campus it was published for without inferring it from the
    author's current role.
    """
    data = _payload(request)
    rows = data.get("announcements")
    if rows is None:
        rows = [data]
    if not isinstance(rows, list):
        return _err("Expected a list under 'announcements'.")
    count = 0
    author_campus = campus_scope_for(request.user)
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = (row.get("title") or "").strip()
        content = (row.get("content") or row.get("body") or "").strip() if title else ""
        if not title or not content:
            continue
        audience = (row.get("audience") or row.get("campus") or "").strip()
        if audience and audience not in VALID_AUDIENCES:
            return _err(
                "Unknown audience '{}'. Expected one of: {}.".format(
                    audience, ", ".join(sorted(VALID_AUDIENCES))
                )
            )
        if audience and not can_post_to_campus(request.user, audience):
            return _err(
                "You cannot post an announcement to '{}'. Your account is "
                "scoped to the {} campus.".format(
                    audience, campus_label_for(request.user)
                ),
                403,
            )
        archived = bool(row.get("archived"))
        target_role = (row.get("target_role") or row.get("targetRole") or TARGET_ALL).strip()
        if target_role not in VALID_TARGET_ROLES:
            return _err(
                "Unknown target role '{}'. Expected one of: {}.".format(
                    target_role, ", ".join(sorted(VALID_TARGET_ROLES))
                )
            )
        # Look the row up within the audience it is being saved as, so a
        # same-titled notice on the other campus is never edited by accident.
        ann = (
            SchoolAnnouncement.objects.filter(title=title, audience=audience or "whole_school")
            .order_by("-posted_at")
            .first()
        )
        if ann is None:
            ann = SchoolAnnouncement.objects.create(
                title=title, content=content, posted_by=request.user,
                status="archived" if archived else "active",
                audience=audience or "whole_school",
                audience_at_posted_at=author_campus,
                target_role=target_role,
            )
        else:
            ann.content = content
            ann.status = "archived" if archived else "active"
            ann.audience = audience or "whole_school"
            ann.audience_at_posted_at = author_campus
            ann.target_role = target_role
            ann.save()
        count += 1
        _log(request.user, f"school_announcement_saved:{title}:{ann.audience}", entity_type="SchoolAnnouncement", entity_id=ann.pk)
        try:
            from apps.communication import notifications as _notif
            _notif.notify_school_announcement(ann)
        except Exception:  # a notification must never break the post
            pass
    try:
        from apps.communication.realtime import notify_school
        notify_school(kind=["school_announcements"])
    except Exception:
        pass
    return _ok(count=count)


# ---------------------------------------------------------------------------
# GET /api/classroom/<int:vc_id>/ — read-only classroom detail for admins
#
# The System Administrator (and other leadership roles) can open any Virtual
# Classroom and see everything inside it: roster, topics & resources,
# assessments with submission/grading progress, announcements and attendance.
# This is a read-only aggregate; all writes happen through the teacher's own
# endpoints.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# GET /api/my/classrooms/<int:vc_id>/ — the signed-in student's own view of
# one virtual classroom they are enrolled in.
#
# This is the refresh target behind the live "something changed" socket: when a
# teacher uploads a material, publishes an assessment, grades a submission or
# posts an announcement, the student's browser is told to re-fetch this and
# repaints the affected tabs. Access is checked against the student's active
# enrollment, so one student can never read another classroom's work.
# ---------------------------------------------------------------------------
@login_required
def my_classroom(request, vc_id):
    profile = getattr(request.user, "student_profile", None)
    if profile is None:
        return _err("Only students have a classroom view.", 403)

    enrolled = VirtualClassroomEnrollment.objects.filter(
        virtual_classroom_id=vc_id,
        student=profile,
        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).select_related(
        "virtual_classroom__subject", "virtual_classroom__section", "virtual_classroom__teacher"
    ).first()
    if enrolled is None:
        return _err("You are not enrolled in this class.", 403)

    from apps.dashboards.student_live import student_classroom_payload

    return _ok(**student_classroom_payload(profile, enrolled.virtual_classroom, user=request.user))


@login_required
def classroom_detail(request, vc_id):
    vc = (
        VirtualClassroom.objects.filter(pk=vc_id)
        .select_related(
            "subject", "section", "teacher",
            "academic_year", "academic_year__structure_version",
        )
        .first()
    )
    if vc is None:
        return _err("Virtual classroom not found.", 404)
    # The assigned teacher may read their own classroom; this is what powers the
    # My Class tabs (Teaching & Learning Activities and Submissions). School
    # leaders keep the campus scope check, because a drill-down cannot
    # out-scope the list it came from.
    if vc.teacher_id != request.user.pk:
        may_read_grade = (
            request.user.groups.filter(name="principal").exists()
            or user_can_read_grade(request.user, vc.section.grade_level)
        )
        if not _is_staff_admin(request.user) or not may_read_grade:
            return _err("You do not have access to this classroom.", 403)

    def _full_name(user):
        if not user or user.pk is None:
            return ""
        return user.get_full_name() or user.email or "Unassigned"

    def _student_name(sp):
        name = "{}, {}".format(sp.last_name, sp.first_name)
        if sp.middle_initial:
            name += " {}.".format(sp.middle_initial)
        if sp.suffix:
            name += " {}".format(sp.suffix)
        return name

    meta = {
        "id": vc.pk,
        "name": vc.subject.name,
        "gradeLevel": vc.subject.grade_level,
        "section": vc.section.name,
        "teacher": _full_name(vc.teacher),
        "year": vc.academic_year.label,
        "period": vc.grading_period,
        "schedule": vc.schedule or "",
        "status": vc.status.title(),
        "themeColor": vc.theme_color or "#4F46E5",
        "createdAt": vc.created_at.strftime("%b %d, %Y") if vc.created_at else "",
    }

    # --- students (active enrollments) --------------------------------------
    enrollments = order_students(
        vc.enrollments.filter(status=VirtualClassroomEnrollment.STATUS_ACTIVE)
        .select_related("student", "student__user"),
        prefix="student__",
    )

    # --- attendance ---------------------------------------------------------
    sessions = list(vc.attendance_sessions.select_related("created_by").order_by("-session_date"))
    session_ids = [s.pk for s in sessions]
    present_codes = {
        AttendanceRecord.PRESENT_ON_TIME,
        AttendanceRecord.PRESENT_LATE,
    }
    per_session = defaultdict(lambda: {"present": 0, "total": 0})
    per_student = defaultdict(lambda: {"present": 0, "late": 0, "absent": 0})
    if session_ids:
        for record in (
            AttendanceRecord.objects.filter(session_id__in=session_ids)
            .select_related("session", "student")
        ):
            ps = per_session[record.session_id]
            ps["total"] += 1
            ss = per_student[record.student_id]
            if record.status in present_codes:
                ps["present"] += 1
                ss["present"] += 1
                if record.status == AttendanceRecord.PRESENT_LATE:
                    ss["late"] += 1
            else:
                ss["absent"] += 1

    total_present = sum(b["present"] for b in per_student.values())
    total_absent = sum(b["absent"] for b in per_student.values())
    attendance = {
        "sessions": len(sessions),
        "present": total_present,
        "late": sum(b["late"] for b in per_student.values()),
        "absent": total_absent,
        "records": total_present + total_absent,
        "recent": [
            {
                "date": s.session_date.isoformat(),
                "recordedBy": _full_name(s.created_by),
                "present": per_session[s.pk]["present"],
                "total": per_session[s.pk]["total"],
            }
            for s in sessions[:20]
        ],
    }

    students = []
    for enrollment in enrollments:
        sp = enrollment.student
        att = per_student.get(sp.pk, {"present": 0, "late": 0, "absent": 0})
        students.append({
            "id": sp.pk,
            "lrn": sp.lrn,
            "name": _student_name(sp),
            "sex": (sp.sex or "").upper(),
            "email": sp.user.email if sp.user_id else "",
            "enrolledAt": enrollment.enrolled_at.strftime("%b %d, %Y") if enrollment.enrolled_at else "",
            "present": att["present"],
            "late": att["late"],
            "absent": att["absent"],
        })

    # --- topics & resources --------------------------------------------------
    topic_res_counts = dict(
        vc.resource_placements.values("topic_id")
        .annotate(n=Count("id"))
        .values_list("topic_id", "n")
    )
    topics = [
        {
            "id": t.pk,
            "name": t.name,
            "order": t.order,
            "supplementary": t.is_supplementary,
            "resources": topic_res_counts.get(t.pk, 0),
            "createdAt": t.created_at.strftime("%b %d, %Y") if t.created_at else "",
        }
        for t in vc.topics.all()
    ]

    placements = vc.resource_placements.select_related(
        "resource", "resource__uploaded_by", "topic"
    ).order_by("-placed_at")
    resources = [
        {
            "placement_id": placement.pk,
            "id": placement.resource.pk,
            "title": placement.resource.title,
            "typeLabel": placement.resource.get_resource_type_display(),
            "externalLink": placement.resource.external_link or "",
            "fileUrl": (
                reverse("teaching:resource_download", args=[placement.resource.pk])
                if placement.resource.file
                else ""
            ),
            "topic": placement.topic.name,
            "size": resource_size_bytes(placement.resource),
            "sizeLabel": resource_size_label(placement.resource),
            "uploadedBy": _full_name(placement.resource.uploaded_by),
            "sharedToRepository": placement.resource.share_to_repository,
            "placedAt": placement.placed_at.strftime("%b %d, %Y") if placement.placed_at else "",
        }
        for placement in placements
    ]

    # --- assessments & submission progress ----------------------------------
    assignments = (
        vc.assessment_assignments
        .select_related("assessment", "assessment__created_by")
        .annotate(
            parts_count=Count("assessment__parts", distinct=True),
            submitted_count=Count("submissions", distinct=True),
            graded_count=Count(
                "submissions",
                filter=Q(submissions__grading_status=Submission.GRADING_COMPLETE),
                distinct=True,
            ),
        )
        .order_by("-assigned_at")
    )
    # Per-student submission rows for the monitor-submissions view, plus the
    # first attached file on each assessment (if any), so the teacher's
    # "uploaded assessment file" is visible in the classroom payload.
    sub_by_assignment = defaultdict(dict)
    for sub in Submission.objects.filter(
        assessment_assignment__virtual_classroom=vc
    ).prefetch_related("responses"):
        sub_by_assignment[sub.assessment_assignment_id][sub.student_id] = sub

    def _assessment_attachment(assessment):
        part = (
            assessment.parts.exclude(attached_file="")
            .exclude(attached_file__isnull=True)
            .first()
        )
        if part is None:
            return "", ""
        try:
            return (
                reverse("assessments:assessment_attachment", args=[assessment.pk, part.pk]),
                os.path.basename(part.attached_file.name),
            )
        except Exception:
            return "", ""

    def _submission_file(sub):
        """Return (download_url, filename) for a student's uploaded work.

        The file lives on `attached_file` for part-less assessments and on the
        first `SubmissionResponse.response_file` when the assessment has parts.
        """
        field = sub.attached_file
        if not field:
            response = next(
                (r for r in sub.responses.all() if r.response_file), None
            )
            field = response.response_file if response is not None else None
        if not field:
            return "", ""
        try:
            return (
                "/api/submission-file/{}/".format(sub.pk),
                os.path.basename(field.name),
            )
        except Exception:
            return "", ""

    assessments = []
    for assignment in assignments:
        assessment = assignment.assessment
        att_url, att_name = _assessment_attachment(assessment)
        sub_map = sub_by_assignment.get(assignment.pk, {})
        submission_rows = []
        for enrollment in enrollments:
            sp = enrollment.student
            sub = sub_map.get(sp.pk)
            if sub is None:
                submission_rows.append(
                    {
                        "studentId": sp.pk,
                        "submissionId": None,
                        "name": _student_name(sp),
                        "status": "not-submitted",
                        "score": "—",
                        "time": "—",
                        "fileUrl": "",
                        "fileName": "",
                        "viewUrl": "",
                    }
                )
            else:
                file_url, file_name = _submission_file(sub)
                submission_rows.append(
                    {
                        "studentId": sp.pk,
                        "submissionId": sub.pk,
                        "name": _student_name(sp),
                        "status": "late" if sub.is_late else "submitted",
                        "score": (
                            "%g" % float(sub.total_score)
                            if sub.total_score is not None
                            else "—"
                        ),
                        "time": timezone.localtime(sub.submitted_at).strftime(
                            "%b %d, %Y %I:%M %p"
                        )
                        if sub.submitted_at
                        else "—",
                        "fileUrl": file_url,
                        "fileName": file_name,
                        "viewUrl": reverse(
                            "assessments:submission_review",
                            args=[assessment.pk, sub.pk],
                        ),
                    }
                )
        assessments.append(
            {
                "id": assignment.pk,
                "title": assessment.title,
                "method": assessment.get_assessment_method_display(),
                "category": assessment.get_assessment_type_display(),
                "stage": assessment.lifecycle_stage.replace("_", " ").title(),
                "status": assessment.status.title(),
                "deadline": assessment.submission_deadline.strftime("%b %d, %Y")
                if assessment.submission_deadline else "",
                "parts": assignment.parts_count,
                "submitted": assignment.submitted_count,
                "graded": assignment.graded_count,
                "students": len(students),
                "createdBy": _full_name(assessment.created_by),
                "assignedAt": assignment.assigned_at.strftime("%b %d, %Y")
                if assignment.assigned_at else "",
                "attachmentUrl": att_url,
                "attachmentName": att_name,
                "submissions": submission_rows,
            }
        )

    # --- announcements --------------------------------------------------------
    _role_filter = _visible_target_roles(request.user)
    announcements = [
        {
            "id": announcement.pk,
            "title": announcement.title or (announcement.content[:90] if announcement.content else "Class announcement"),
            "content": announcement.content,
            "postedBy": _full_name(announcement.posted_by),
            "postedAt": announcement.posted_at.strftime("%b %d, %Y · %I:%M %p")
            if announcement.posted_at else "",
            "status": announcement.status.title(),
            "targetRole": announcement.target_role or TARGET_ALL,
            "attachmentUrl": (announcement.attachment.url if announcement.attachment else ""),
        }
        for announcement in vc.announcements.select_related("posted_by")
        .filter(Q(target_role__in=_role_filter) | Q(target_role=""))
        .order_by("-posted_at")
    ]

    _log(request.user, f"viewed_classroom:{vc.pk}", entity_type="VirtualClassroom", entity_id=vc.pk)
    return _ok(
        virtualClassroom=meta,
        students=students,
        topics=topics,
        resources=resources,
        assessments=assessments,
        announcements=announcements,
        attendance=attendance,
    )


# ---------------------------------------------------------------------------
# POST /api/class-announcements/ — classroom feed (teacher)
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required(*STAFF_ROLES)
def class_announcements(request):
    data = _payload(request)
    title = (data.get("title") or "").strip()[:200]
    content = (data.get("content") or data.get("body") or "").strip()
    if not content:
        return _err("Announcement content is required.")
    vc_id = data.get("virtual_classroom_id")
    if vc_id in (None, ""):
        return _err("virtual_classroom_id is required; class announcements are classroom-scoped.")
    vc = VirtualClassroom.objects.filter(pk=vc_id).first()
    if vc is None:
        return _err("Virtual classroom not found.")
    if not (vc.teacher_id == request.user.pk or _is_staff_admin(request.user)):
        return _err("You may only post announcements in your own virtual classroom.")
    archived = bool(data.get("archived"))
    target_role = (data.get("target_role") or data.get("targetRole") or TARGET_ALL).strip()
    if target_role not in VALID_TARGET_ROLES:
        return _err(
            "Unknown target role '{}'. Expected one of: {}.".format(
                target_role, ", ".join(sorted(VALID_TARGET_ROLES))
            )
        )
    ann = ClassAnnouncement.objects.filter(virtual_classroom=vc, title=title, content=content).first()
    if ann is None:
        ann = ClassAnnouncement.objects.create(
            virtual_classroom=vc, posted_by=request.user, title=title, content=content,
            status="archived" if archived else "active",
            target_role=target_role,
        )
    else:
        ann.status = "archived" if archived else "active"
        ann.target_role = target_role
        ann.save()
    _log(request.user, f"class_announcement_saved:{ann.pk}", entity_type="ClassAnnouncement", entity_id=ann.pk)
    try:
        notif_service.notify_class_announcement(ann)
    except Exception:
        pass
    try:
        _notify_classroom_change(vc, request.user, ["announcements"])
    except Exception:
        pass
    return _ok(count=1, virtual_classroom_id=vc.pk, announcement={
        "id": ann.pk, "announcement_id": ann.pk, "classroomId": vc.pk,
        "title": ann.title or (ann.content[:90] if ann.content else "Class announcement"),
        "body": ann.content, "createdAt": ann.posted_at.isoformat(),
        "archived": ann.status == "archived",
    })


# ---------------------------------------------------------------------------
# POST /api/profile/ — current user profile updates
# ---------------------------------------------------------------------------
@login_required
@require_POST
def profile(request):
    data = _payload(request)
    name = (data.get("name") or "").strip()
    contact = (data.get("contact") or "").strip()
    email = (data.get("email") or "").strip().lower()
    changed = False

    if email and email != request.user.email and User.objects.filter(email=email).exclude(pk=request.user.pk).exists():
        return _err("That email address is already in use by another account.", status=200)

    employee = _employee(request.user)
    if employee is not None:
        if name:
            first, mi, last = _parse_name(name)
            if first and (employee.first_name != first or employee.last_name != last):
                employee.first_name, employee.middle_initial, employee.last_name = first, mi, last
                changed = True
        if "contact" in data:
            cleaned = contact.replace(" ", "")
            if employee.contact_number != cleaned:
                employee.contact_number = cleaned
                changed = True
        if changed:
            employee.save()
    else:
        student = _student(request.user)
        if student is not None:
            if name:
                first, mi, last = _parse_name(name)
                if first:
                    student.first_name, student.middle_initial, student.last_name = first, mi, last
                    changed = True
            if "contact" in data and student.contact_number != contact:
                student.contact_number = contact
                changed = True
            if changed:
                student.save()

    if email and email != request.user.email:
        request.user.email = email
        request.user.save(update_fields=["email"])
        changed = True

    target = employee if employee is not None else _student(request.user)
    avatar = request.FILES.get("avatar")
    if avatar is not None:
        if target is None:
            return _err("This account has no profile record available for a profile photo.", status=409)
        target.avatar = avatar
        target.save(update_fields=["avatar"])
        changed = True

    # Return the stable /accounts/avatar/<key>/ path, not FieldFile.url: with a
    # private R2 bucket that is a presigned URL, and the front-end keeps it in
    # the DOM (and in localStorage) long after the signature expires.
    from apps.accounts.context_processors import avatar_url as stable_avatar_url

    avatar = ""
    if target is not None and target.avatar:
        avatar = stable_avatar_url(target.avatar) or ""

    _log(request.user, "profile_updated", entity_type="User", entity_id=request.user.pk)
    return _ok(changed=changed, avatar=avatar)


# ---------------------------------------------------------------------------
# POST /api/submissions/ — student submits work (optional file) / quiz score
# ---------------------------------------------------------------------------
@login_required
@require_POST
def submissions(request):
    from apps.assessments.models import (
        AssessmentAssignment,
        Submission,
        SubmissionResponse,
    )

    student = _student(request.user)
    if student is None:
        return _err("Only students can submit assessments.", 403)

    data = _payload(request)
    title = (data.get("title") or "").strip()
    note = (data.get("note") or "").strip()
    score = data.get("score")
    try:
        violations = int(data.get("violations") or 0)
    except (TypeError, ValueError):
        violations = 0

    enrolled = VirtualClassroomEnrollment.objects.filter(
        student=student, status=STATUS_ACTIVE
    ).values_list("virtual_classroom_id", flat=True)

    assignment = None
    aid = data.get("assessment_id")
    assignment_id = data.get("assignment_id")
    if assignment_id and str(assignment_id).isdigit():
        assignment = AssessmentAssignment.objects.filter(
            pk=int(assignment_id), virtual_classroom_id__in=enrolled
        ).select_related("assessment").first()
        if assignment and aid and str(assignment.assessment_id) != str(aid):
            return _err("The assignment does not belong to this assessment.", 400)
    elif aid and str(aid).isdigit():
        matching = AssessmentAssignment.objects.filter(
            assessment_id=int(aid), virtual_classroom_id__in=enrolled
        ).select_related("assessment")
        if matching.count() > 1:
            return _err("assignment_id is required when this assessment is assigned to multiple classes.", 400)
        assignment = matching.first()
    if assignment is None and title and not assignment_id:
        assignment = (
            AssessmentAssignment.objects.filter(
                virtual_classroom_id__in=enrolled,
                assessment__title__iexact=title,
            )
            .select_related("assessment")
            .first()
        )
    if assignment is None:
        return _err("No matching assessment was found for this submission.", 404)

    assessment = assignment.assessment
    from apps.assessments.scheduling import assessment_window

    state = assessment_window(assessment)
    if state == "upcoming":
        return _err("This assessment is not yet available for submission.", 403)
    if state == "closed":
        return _err("This assessment has closed and is no longer accepting submissions.", 403)

    upload = request.FILES.get("file")
    if upload is None:
        return _err("Attach a file or submit answers through the active assessment attempt.", 400)

    submission, created = Submission.objects.get_or_create(
        assessment_assignment=assignment, student=student
    )
    if not created:
        # If the student already submitted (responses/files attached) block
        # re-submission from the SPA.
        if submission.responses.exists() or submission.attached_file:
            return _err("You have already submitted this assessment.", 409)

    # A student's client-side score is never authoritative. Objective scoring
    # runs from saved responses on the server; uploaded work stays ungraded.

    parts = list(assignment.assessment.parts.order_by("order"))
    if parts:
        response, _ = SubmissionResponse.objects.get_or_create(
            submission=submission, part=parts[0]
        )
        response.response_file = upload
        if note:
            response.response_data = {"note": note}
        response.save()
    else:
        submission.attached_file = upload
        submission.save(update_fields=["attached_file"])

    _log(
        request.user,
        "assessment_submitted",
        entity_type="Submission",
        entity_id=submission.pk,
    )
    if violations > 0:
        _log(
            request.user,
            f"assessment_proctor_violation:{violations}",
            entity_type="Submission",
            entity_id=submission.pk,
        )
    return _ok(submission_id=submission.pk, created=created, violations=violations)


@login_required
@require_POST
def submission_autosave(request):
    """Save a versioned draft to its authenticated, server-timed attempt."""
    from django.db import transaction

    from apps.assessments.forms import SubmitForm
    from apps.assessments.models import AssessmentAttempt, Submission
    from apps.assessments.scheduling import assessment_window

    student = _student(request.user)
    if student is None:
        return _err("Only students can save drafts.", 403)

    data = _payload(request)
    now = timezone.now()
    required_ids = ("assessment_id", "assignment_id", "attempt_id", "revision")
    if any(not str(data.get(key, "")).isdigit() for key in required_ids):
        return _err("assessment_id, assignment_id, attempt_id and revision are required.")
    attempt = AssessmentAttempt.objects.select_related(
        "assignment__assessment", "student__user"
    ).filter(
        pk=int(data["attempt_id"]),
        student=student,
        assignment_id=int(data["assignment_id"]),
        assignment__assessment_id=int(data["assessment_id"]),
    ).first()
    if attempt is None:
        return _err("Assessment attempt not found for this student.", 404)
    assessment = attempt.assignment.assessment
    state = assessment_window(assessment, now)
    if state == "upcoming":
        return _err("This assessment is not yet available.", 403)
    if state == "closed":
        return _err("This assessment is closed.", 403)
    if attempt.submitted_at:
        return _err("This assessment attempt has already been submitted.", 409)
    if Submission.objects.filter(
        assessment_assignment=attempt.assignment, student=student
    ).exists():
        return _err("This assessment has already been submitted.", 409)
    if now >= attempt.expires_at:
        return _err("The assessment timer has expired.", 409)

    answers = data.get("answers")
    if not isinstance(answers, dict):
        return _err("answers must be an object keyed by assessment question.")
    parts = list(assessment.parts.order_by("order"))
    answer_form = SubmitForm(parts=parts)
    unknown = set(answers) - set(answer_form.fields)
    if unknown:
        return _err("One or more question IDs do not belong to this assessment.", 400)
    normalized = {}
    for field_name, value in answers.items():
        field = answer_form.fields[field_name]
        if not isinstance(value, (str, int, float, list, type(None))):
            return _err("Answer format is invalid.", 400)
        try:
            if value not in (None, "", []):
                field.clean(value)
            normalized[field_name] = value
        except Exception:
            return _err("An answer is not valid for its question.", 400)

    try:
        revision = int(data["revision"])
    except (TypeError, ValueError):
        return _err("revision must be an integer.")
    with transaction.atomic():
        locked = AssessmentAttempt.objects.select_for_update().filter(
            pk=attempt.pk, student=student
        ).first()
        if locked is None:
            return _err("Assessment attempt not found.", 404)
        if revision <= locked.revision:
            return _ok(saved=True, stale=True, revision=locked.revision, answers=locked.answers,
                       last_saved_at=locked.last_saved_at.isoformat() if locked.last_saved_at else None)
        now = timezone.now()
        state = assessment_window(assessment, now)
        if state != "available" or now >= locked.expires_at or locked.submitted_at:
            return _err("The assessment attempt is no longer accepting answers.", 409)
        locked.answers = normalized
        locked.revision = revision
        locked.last_saved_at = now
        locked.save(update_fields=["answers", "revision", "last_saved_at"])
    return _ok(saved=True, stale=False, revision=revision, last_saved_at=now.isoformat())


# ---------------------------------------------------------------------------
# POST /api/remind/ — a teacher nudges students who have not submitted.
#
# Body: {assignment_id} (or {title}) plus optional {student_ids: [...]}.
# Without student_ids every enrolled student with no Submission is reminded;
# with student_ids only those students are. One bell Notification per student,
# keyed on ("assessment_reminder", <assignment pk>): each nudge replaces that
# student's previous reminder for the same assessment with a fresh unread row,
# so the red dot lights again while the assessment's own ("assessment", <pk>)
# row is never touched.
# ---------------------------------------------------------------------------
@login_required
@require_POST
def remind(request):
    from apps.assessments.models import AssessmentAssignment, Submission

    data = _payload(request)
    aid = data.get("assignment_id") or data.get("assessment_id")
    title = (data.get("title") or "").strip()

    qs = AssessmentAssignment.objects.select_related(
        "assessment", "virtual_classroom", "virtual_classroom__section"
    )

    assignment = None
    if aid is not None and str(aid).isdigit():
        assignment = qs.filter(pk=int(aid)).first()
    if assignment is None and title:
        assignment = qs.filter(assessment__title__iexact=title).first()
    if assignment is None:
        return _err("No assessment you teach matches that reminder.", 404)

    vc = assignment.virtual_classroom
    # Same access rule the classroom page itself uses: the assigned teacher may
    # nudge their own class; a school leader may nudge a class they can read.
    # Checking is_staff here instead 404'd every leader whose account carries a
    # role group but not Django's staff flag, so the button always failed.
    if vc.teacher_id != request.user.pk:
        if not _is_staff_admin(request.user) or not user_can_read_grade(
            request.user, vc.section.grade_level
        ):
            return _err("No assessment you teach matches that reminder.", 404)
    enrollments = list(
        VirtualClassroomEnrollment.objects.filter(
            virtual_classroom=vc,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).select_related("student__user")
    )

    raw_ids = data.get("student_ids")
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    if raw_ids:
        wanted = {str(x) for x in raw_ids}
        targets = [
            e
            for e in enrollments
            if str(e.student_id) in wanted
            or str(getattr(e.student, "user_id", "")) in wanted
        ]
    else:
        submitted = set(
            Submission.objects.filter(
                assessment_assignment=assignment,
                student_id__in=[e.student_id for e in enrollments],
            ).values_list("student_id", flat=True)
        )
        targets = [e for e in enrollments if e.student_id not in submitted]

    recipient_ids = [
        e.student.user_id for e in targets if e.student and e.student.user_id
    ]
    if not recipient_ids:
        return _ok(sent=0, created=0)

    assess_title = assignment.assessment.title or "your assessment"
    message = 'Your teacher is reminding you to submit "{}".'.format(assess_title)
    deadline = assignment.assessment.submission_deadline
    if deadline:
        message += " Due {}.".format(
            timezone.localtime(deadline).strftime("%b %d, %Y %I:%M %p")
        )
    url = reverse("classrooms:classroom_detail", args=[vc.pk])

    created = 0
    for uid in recipient_ids:
        # A nudge is a fresh event each time: drop the previous reminder for
        # this student+assessment and create a new unread row. Otherwise the
        # second reminder reuses the already-read row (source_id was NULL for
        # every assessment, so they all collided) and the student's bell never
        # lit again.
        Notification.objects.filter(
            recipient_id=uid,
            notification_type=Notification.TYPE_ASSESSMENT,
            source_type="assessment_reminder",
            source_id=assignment.pk,
        ).delete()
        Notification.objects.create(
            recipient_id=uid,
            notification_type=Notification.TYPE_ASSESSMENT,
            source_type="assessment_reminder",
            source_id=assignment.pk,
            virtual_classroom=vc,
            title="Reminder: {}".format(assess_title)[:200],
            message=message[:200],
            url=url,
        )
        created += 1
    _log(
        request.user,
        "assessment_reminder_sent:{}".format(assess_title),
        entity_type="AssessmentAssignment",
        entity_id=assignment.pk,
    )
    return _ok(sent=len(recipient_ids), created=created)


# ---------------------------------------------------------------------------
# GET /api/submission-file/<pk>/ — download a student's uploaded work.
# Accessible to the student who owns it, the assigned teacher, or an
# authorised school leader for the classroom's grade level.
# ---------------------------------------------------------------------------
@login_required
@require_GET
def submission_file(request, pk):
    submission = (
        Submission.objects.filter(pk=pk)
        .select_related(
            "student",
            "student__user",
            "assessment_assignment",
            "assessment_assignment__virtual_classroom",
            "assessment_assignment__virtual_classroom__section",
        )
        .first()
    )
    if submission is None:
        return _err("Submission not found.", 404)

    user = request.user
    vc = submission.assessment_assignment.virtual_classroom
    is_owner = submission.student.user_id == user.pk
    is_teacher = vc.teacher_id == user.pk
    is_leader = _is_staff_admin(user) and user_can_read_grade(
        user, vc.section.grade_level
    )
    if not (is_owner or is_teacher or is_leader):
        return _err("You do not have access to this submission.", 403)

    field = submission.attached_file
    if not field:
        response = next(
            (
                r
                for r in submission.responses.all()
                if r.response_file
            ),
            None,
        )
        field = response.response_file if response is not None else None
    if not field:
        return _err("This submission has no uploaded file.", 404)

    try:
        handle = field.open("rb")
        return FileResponse(
            handle,
            as_attachment=True,
            filename=os.path.basename(field.name),
        )
    except Exception:
        try:
            return HttpResponseRedirect(field.url)
        except Exception:
            return _err("The submitted file could not be found.", 404)


# ---------------------------------------------------------------------------
# POST /api/manual-grade/ — teacher scores a subjective submission
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_perm_required("manually_grade_subjective_assessment")
def manual_grade(request):
    from apps.assessments.models import Submission, SubmissionResponse

    data = _payload(request)
    title = (data.get("assessment") or "").strip()
    feedback = (data.get("feedback") or "").strip()
    lrn = (data.get("lrn") or "").strip()
    student_name = (data.get("student") or "").strip()
    score = data.get("score")
    if not title or score in (None, ""):
        return _err("Assessment and score are required.")

    vcs = VirtualClassroom.objects.filter(teacher=request.user).values_list("id", flat=True)
    assignment = (
        AssessmentAssignment.objects.filter(
            virtual_classroom_id__in=vcs, assessment__title__iexact=title
        )
        .select_related("assessment")
        .first()
    )
    if assignment is None:
        return _err("No matching assessment was found for your classes.", 404)

    submissions = Submission.objects.filter(assessment_assignment=assignment).select_related("student")
    submission = submissions.filter(student__lrn=lrn).first() if lrn else None
    if submission is None and student_name:
        parts = student_name.split()
        if parts:
            submission = submissions.filter(
                student__first_name__icontains=parts[0],
                student__last_name__icontains=parts[-1],
            ).first()
    if submission is None:
        return _err("No submission was found for this student.", 404)

    try:
        value = Decimal(str(score))
    except (InvalidOperation, TypeError, ValueError):
        return _err("Invalid score.")

    part = assignment.assessment.parts.order_by("order").first()
    if part is None:
        return _err("This assessment has no parts to grade.", 400)

    response, _ = SubmissionResponse.objects.get_or_create(submission=submission, part=part)
    response.score = value
    response.item_scores = {"manual": {"score": float(value)}}
    response.grading_status = SubmissionResponse.GRADED
    response.graded_by = request.user
    response.graded_at = timezone.now()
    response.save()

    if feedback:
        submission.teacher_remarks = feedback
        submission.save(update_fields=["teacher_remarks"])
    submission.recompute_total_score()

    _log(
        request.user,
        "manual_grade_saved",
        entity_type="Submission",
        entity_id=submission.pk,
    )
    return _ok(
        submission_id=submission.pk,
        total_score=float(submission.total_score or 0),
        grading_status=submission.grading_status,
    )


# ---------------------------------------------------------------------------
# POST /api/virtual-classroom/theme/ — teacher personalizes an assigned VC
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_perm_required("update_virtual_classroom", "update_class_announcement")
def vc_theme(request):
    data = _payload(request)

    vc = None
    vid = data.get("virtual_classroom_id")
    if vid and str(vid).isdigit():
        vc = VirtualClassroom.objects.filter(pk=int(vid), teacher=request.user).first()

    if vc is None:
        title = (data.get("title") or "").strip().lower()
        section = (data.get("section") or "").strip().lower()
        candidates = VirtualClassroom.objects.filter(
            teacher=request.user, status=STATUS_ACTIVE
        ).select_related("subject", "section")
        for cand in candidates:
            grade = getattr(cand.subject, "grade_level", None)
            label = (
                ("Grade " + str(grade) + " – " + cand.subject.name) if grade else cand.subject.name
            )
            sec_name = (cand.section.name if cand.section else "")
            if section and sec_name.lower() != section:
                continue
            if title and title not in (label.lower(), cand.subject.name.lower()):
                continue
            vc = cand
            break

    if vc is None:
        return _err("Virtual Classroom not found for this teacher.", 404)

    color = (data.get("color") or "").strip()
    if color:
        vc.theme_color = color[:7]

    upload = request.FILES.get("background")
    if upload is not None:
        if not (upload.content_type or "").startswith("image/"):
            return _err("Background must be an image file.")
        vc.background_image = upload

    vc.save()
    _log(request.user, "vc_theme_updated", entity_type="VirtualClassroom", entity_id=vc.id)
    # Everyone enrolled in this classroom repaints its cover/theme without a
    # reload, exactly like a material or announcement change.
    try:
        _notify_classroom_change(vc, request.user, ["theme", "classroom"])
    except Exception:
        pass
    return _ok(
        virtual_classroom_id=vc.id,
        color=vc.theme_color,
        background=(vc.background_image.url if vc.background_image else ""),
    )


# ---------------------------------------------------------------------------
# POST /api/virtual-classroom/enroll/ — add / remove students in a classroom
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required(*REGISTRAR_ROLES)
def vc_enroll(request):
    data = _payload(request)
    vc = VirtualClassroom.objects.filter(
        pk=data.get("virtual_classroom_id") or 0, status=STATUS_ACTIVE
    ).first()
    if vc is None:
        return _err("Virtual Classroom not found.", 404)

    lrns = data.get("students") or data.get("lrns") or []
    if not isinstance(lrns, list):
        return _err("Expected a list of LRNs under 'students'.")
    lrns = [str(x).strip() for x in lrns if str(x).strip()]
    if not lrns:
        return _ok(added=0, removed=0, virtual_classroom_id=vc.pk)

    profiles = StudentProfile.objects.filter(lrn__in=lrns)
    remove = bool(data.get("remove"))
    added = removed = 0
    if remove:
        removed = (
            VirtualClassroomEnrollment.objects.filter(
                virtual_classroom=vc,
                student__in=profiles,
                status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            )
            .update(
                status=VirtualClassroomEnrollment.STATUS_REMOVED,
                removed_at=timezone.now(),
            )
        )
    else:
        existing = set(
            VirtualClassroomEnrollment.objects.filter(
                virtual_classroom=vc,
                student__in=profiles,
                status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            ).values_list("student_id", flat=True)
        )
        fresh = [
            VirtualClassroomEnrollment(virtual_classroom=vc, student=sp)
            for sp in profiles
            if sp.pk not in existing
        ]
        VirtualClassroomEnrollment.objects.bulk_create(fresh)
        added = len(fresh)

    _log(
        request.user,
        "vc_enroll" if not remove else "vc_unenroll",
        entity_type="VirtualClassroom",
        entity_id=vc.pk,
    )
    return _ok(added=added, removed=removed, virtual_classroom_id=vc.pk)


# ---------------------------------------------------------------------------
# POST /api/notifications/read/
# ---------------------------------------------------------------------------
@login_required
@require_POST
def notifications_read(request):
    data = _payload(request)
    from apps.assessments.models import AssessmentNotification

    profile = _student(request.user)
    # Staff accounts have no student profile. Without this guard `qs` stayed
    # unfiltered, so any signed-in staff user could mark every notification in
    # the system as read with one POST.
    if profile is None:
        return _ok(count=0)
    qs = AssessmentNotification.objects.filter(student=profile)
    if isinstance(data.get("ids"), list) and data["ids"]:
        qs = qs.filter(pk__in=[int(i) for i in data["ids"] if str(i).isdigit()])
    count = qs.filter(is_read=False).update(is_read=True)
    return _ok(count=count)


# ---------------------------------------------------------------------------
# GET /api/notifications/ — the persistent header-bell feed for the SPA
# ---------------------------------------------------------------------------
@login_required
@require_GET
def notifications(request):
    """Unread count plus the newest notifications, scoped to the caller.

    The SPA dashboards render their bell from this; the server-rendered pages
    use the matching ``communication`` view. Both read the same model, so the
    two UIs cannot disagree.
    """
    try:
        limit = int(request.GET.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))
    # There is no scheduler process in the pilot. The student's own bell poll
    # (every 30s) delivers both the two-minute opening reminder and the
    # availability notice as soon as its window opens. Both sweeps are
    # idempotent and scoped to active assessments/classroom enrollments.
    if getattr(request.user, "student_profile", None) is not None:
        try:
            from apps.assessments.services import (
                trigger_availability_notifications,
                trigger_opening_soon_notifications,
            )

            trigger_opening_soon_notifications()
            trigger_availability_notifications()
        except Exception:  # a notification sweep must never break the feed
            pass
    qs = Notification.objects.filter(recipient=request.user).select_related("virtual_classroom")
    unread_count = qs.filter(is_read=False).count()
    from apps.communication import notifications as notif_service

    items = [
        {
            "id": n.pk,
            "type": n.notification_type,
            "title": n.title,
            "message": n.message,
            "url": notif_service.notification_target(n),
            "is_read": n.is_read,
            "classroom_id": n.virtual_classroom_id,
            "source_type": n.source_type,
            "source_id": n.source_id,
            "created_at": n.created_at.isoformat(),
            "open_url": reverse("communication:notification_open", args=[n.pk]),
        }
        for n in qs[:limit]
    ]
    return JsonResponse({"ok": True, "unread_count": unread_count, "total": qs.count(), "items": items})


# ---------------------------------------------------------------------------
# POST /api/notifications/mark-all-read/ — clear the bell for the caller
# ---------------------------------------------------------------------------
@login_required
@require_POST
def notifications_mark_all_read(request):
    count = Notification.objects.filter(recipient=request.user, is_read=False).update(
        is_read=True, read_at=timezone.now()
    )
    return _ok(count=count, unread_count=0)


# ---------------------------------------------------------------------------
# POST /api/teacher-content/ — topics + learning resources for a classroom
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_perm_required("create_assessment_custom", "upload_learning_resource", "manage_learning_resources", allow_teacher=True)
def teacher_content(request):
    data = _payload(request)
    vc = None
    if data.get("virtual_classroom_id"):
        try:
            cid = int(data.get("virtual_classroom_id"))
        except (TypeError, ValueError):
            return _err("Invalid virtual_classroom_id.")
        vc = VirtualClassroom.objects.filter(pk=cid, teacher=request.user, status=STATUS_ACTIVE).first()
    if vc is None:
        return _err("Open a Virtual Classroom before adding or copying learning resources.", status=409)
    requested_topic = None
    if data.get("topic_id") and data.get("action") in ("resource", "link", "file", "upload"):
        requested_topic = Topic.objects.filter(
            pk=data["topic_id"], virtual_classroom=vc
        ).first()
        if requested_topic is None:
            return _err("The selected topic was not found in this classroom.", 404)

    saved = {}
    topic = None
    if data.get("action") == "topic":
        title = (data.get("name") or data.get("title") or "").strip()
        if not title:
            return _err("Topic title is required.")
        topic = Topic.objects.filter(virtual_classroom=vc, name=title).first()
        if topic is None:
            m = Topic.objects.filter(virtual_classroom=vc).aggregate(m=Max("order"))["m"]
            order = 0 if m is None else m + 1
            topic = Topic.objects.create(virtual_classroom=vc, name=title, order=order)
        saved["topic_id"] = topic.pk
    elif data.get("action") in ("resource", "link"):
        title = (data.get("title") or data.get("name") or "").strip()
        link = (data.get("url") or data.get("link") or "").strip()
        rtype = data.get("type") or ("link" if link else "document")
        if not title or not link:
            return _ok(count=0, skipped="no_external_link")
        if not link.startswith("http"):
            link = "https://" + link
        if rtype not in dict(LearningResource.RESOURCE_TYPE_CHOICES):
            rtype = "link"
        share = _share_flag(data)
        resource = LearningResource.objects.filter(
            title=title,
            uploaded_by=request.user,
            external_link=link,
            share_to_repository=share,
        ).first()
        if resource is None:
            resource = LearningResource.objects.create(
                subject=vc.subject, academic_year=vc.academic_year, uploaded_by=request.user,
                title=title, description=(data.get("description") or ""),
                resource_type=rtype, external_link=link, share_to_repository=share,
            )
        elif share:
            resource.share_to_repository = True
            resource.save()
        saved["resource_id"] = resource.pk
        if data.get("topic_id"):
            topic = requested_topic
        topic_name = (data.get("topic") or data.get("topic_name") or "").strip()
        if topic is None and topic_name:
            topic = Topic.objects.filter(virtual_classroom=vc, name=topic_name).first()
        if topic is None:
            topic, _ = Topic.objects.get_or_create(
                virtual_classroom=vc, name="General Resources", defaults={"order": 0}
            )
        placement, _ = LearningResourcePlacement.objects.get_or_create(
            resource=resource, virtual_classroom=vc, topic=topic,
            defaults={"placed_by": request.user},
        )
        try:
            notif_service.notify_resource_placement(placement)
        except Exception:
            pass

    elif data.get("action") in ("file", "upload"):
        uploads = request.FILES.getlist("file")
        if not uploads:
            upload = request.FILES.get("file")
            uploads = [upload] if upload else []
        if not uploads:
            return _err("No file was received. Please choose a file to upload.")
        title = (data.get("title") or data.get("name") or "").strip()
        if not title:
            title = uploads[0].name or "Untitled resource"
        rtype = data.get("type") or "document"
        _RTYPE_ALIAS = {"doc": "document", "ppt": "presentation", "img": "image", "vid": "video", "aud": "audio", "lnk": "link", "scorm": "document"}
        rtype = _RTYPE_ALIAS.get(rtype, rtype)
        if rtype not in dict(LearningResource.RESOURCE_TYPE_CHOICES):
            rtype = "document"
        share = _share_flag(data)
        resources = []
        for upload in uploads:
            try:
                resource = LearningResource.objects.create(
                    subject=vc.subject,
                    academic_year=vc.academic_year or _active_year(),
                    uploaded_by=request.user,
                    title=(data.get("title") or "").strip() or upload.name or "Untitled resource",
                    description=(data.get("description") or ""),
                    resource_type=rtype,
                    file=upload,
                    share_to_repository=share,
                )
            except Exception as exc:
                return _err(str(exc))
            resources.append(resource)
        resource = resources[0]
        saved["resource_id"] = resource.pk
        saved["resource_ids"] = [r.pk for r in resources]
        if resource.file:
            try:
                saved["resource_url"] = resource.file.url
            except Exception:
                pass
        if data.get("topic_id"):
            topic = requested_topic
        topic_name = (data.get("topic") or data.get("topic_name") or "").strip()
        if topic is None and topic_name:
            topic = Topic.objects.filter(virtual_classroom=vc, name=topic_name).first()
        if topic is None:
            topic, _ = Topic.objects.get_or_create(
                virtual_classroom=vc, name="General Resources", defaults={"order": 0}
            )
        for _resource in resources:
            placement, _ = LearningResourcePlacement.objects.get_or_create(
                resource=_resource, virtual_classroom=vc, topic=topic, placed_by=request.user
            )
            try:
                notif_service.notify_resource_placement(placement)
            except Exception:
                pass
            _log(request.user, "resource_uploaded", entity_type="LearningResource", entity_id=_resource.pk)

    # Tell the students in this classroom their Materials tab changed, so it
    # repaints without a reload.
    try:
        _notify_material_change(vc, request.user, action=data.get("action"))
    except Exception:
        pass
    return _ok(**saved)


def _notify_material_change(vc, actor, action=None):
    """Push a 'materials changed' signal to everyone in the classroom."""
    kinds = ["materials", "topics"]
    if action in ("topic",):
        kinds = ["topics"]
    _notify_classroom_change(vc, actor, kinds)


def _notify_classroom_change(vc, actor, kinds):
    """Tell the classroom (and each enrolled student directly) that it changed.

    A student who has not reloaded since enrolling has not joined the
    ``class-<id>`` socket room yet, so they are notified individually too.
    """
    from apps.communication.realtime import notify_enrolled_students

    try:
        notify_enrolled_students(vc.pk, kind=list(kinds), actor=actor, exclude=actor)
    except Exception:  # realtime must never break the save
        pass


# ---------------------------------------------------------------------------
# GET /api/learning-resources/ — the current teacher's learning resources
# ---------------------------------------------------------------------------
def _human_size(num):
    """Alias of the shared formatter so every resource payload agrees."""
    return human_size(num)


@login_required
def learning_resources(request):
    requested_vc = request.GET.get("vc_id") or request.GET.get("virtual_classroom_id")
    requested_year_id = request.GET.get("academic_year_id")
    qs = (
        LearningResource.objects.filter(status=LearningResource.STATUS_ACTIVE)
        .select_related("subject", "uploaded_by")
        .prefetch_related("placements__topic")
        .order_by("subject__grade_level", "subject__name", "title", "pk")
    )
    is_teacher = (
        request.user.groups.filter(name="teacher").exists()
        and not request.user.is_superuser
    )
    if is_teacher:
        from apps.accounts.models import teacher_subject_ids
        teacher_subject_ids_for_repo = teacher_subject_ids(request.user)
        if requested_vc:
            # A classroom materials view stays limited to the caller's class.
            qs = qs.filter(subject_id__in=teacher_subject_ids_for_repo).distinct()
        else:
            # The shared resource repository spans all subjects in the active
            # school year. Assessment repository subject scoping is separate.
            selected_year = None
            if requested_year_id:
                selected_year = AcademicYear.objects.filter(pk=requested_year_id).first()
            if selected_year is None:
                selected_year = AcademicYear.objects.filter(
                    status=AcademicYear.STATUS_ACTIVE
                ).order_by("-start_date", "-pk").first()
            if selected_year:
                qs = qs.filter(subject__academic_year=selected_year)
            qs = qs.filter(share_to_repository=True).distinct()
    else:
        teacher_subject_ids_for_repo = set()

    # Roles that see every classroom (as opposed to only their own). The System
    # Administrator holds every permission, so the shared repository must offer
    # the full classroom list and let the admin copy resources into any class.
    is_platform_admin = request.user.is_superuser or request.user.groups.filter(name__in=["system_admin"]).exists()
    # Leadership (campus-wide) roles browse a true school-wide Material
    # Repository: every learning resource, including ones teachers only placed
    # in a specific Virtual Classroom. They are not students, so per-class
    # materials are not a leak for them.
    is_school_wide_viewer = is_platform_admin or request.user.groups.filter(
        name__in=["principal", "elementary_principal", "assistant_principal", "quality_audit"]
    ).exists()
    if requested_vc:
        if is_teacher:
            cid = VirtualClassroom.objects.filter(
                pk=requested_vc, teacher=request.user
            ).values_list("pk", flat=True).first()
            if cid is None:
                return _ok(resources=[], virtual_classrooms=[], repository_subjects=[])
        else:
            cid = requested_vc
        qs = qs.filter(placements__virtual_classroom_id=cid).distinct()
    else:
        # With no classroom filter this is the browse-all list.
        #   - Leadership/admin: every resource in the school.
        #   - Teachers: the shared archive across years plus their own uploads.
        #   - Everyone else (students etc.): shared resources only.
        if is_school_wide_viewer:
            pass
        elif is_teacher:
            qs = qs.filter(share_to_repository=True).distinct()
        else:
            qs = qs.filter(share_to_repository=True).distinct()

    alias_map = {
        "document": "doc", "presentation": "ppt", "image": "img",
        "video": "vid", "audio": "aud", "pdf": "pdf", "link": "lnk",
    }
    icon_map = {
        "doc": "fa-solid fa-file-word", "ppt": "fa-solid fa-file-powerpoint",
        "img": "fa-solid fa-image", "vid": "fa-solid fa-video",
        "aud": "fa-solid fa-headphones", "pdf": "fa-solid fa-file-pdf",
        "lnk": "fa-solid fa-link",
    }
    color_map = {
        "doc": "#3a86d4", "ppt": "#e99b2a", "img": "#8b5cf6",
        "vid": "#f97316", "aud": "#0ea5e9", "pdf": "#d94f4f", "lnk": "#64748b",
    }

    items = []
    for r in qs:
        alias = alias_map.get(r.resource_type, "doc")
        placement = next(iter(r.placements.all()), None)
        topic = placement.topic.name if placement and placement.topic_id else "General Resources"
        size = ""
        if r.file:
            try:
                size = _human_size(r.file.size)
            except Exception:
                size = ""
        meta = r.get_resource_type_display()
        if size:
            meta += " · " + size
        url = ""
        if r.file:
            # Route through the authenticated download view rather than the raw
            # storage URL: it re-checks classroom/subject access and returns the
            # original bytes with the right Content-Type and attachment header,
            # so a Word/PDF file is not rendered as text in the browser.
            url = reverse("teaching:resource_download", args=[r.pk])
        elif r.external_link:
            url = r.external_link
        items.append({
            "id": r.id,
            "title": r.title,
            "subject_id": r.subject_id,
            "subject": r.subject.name if r.subject_id else "",
            "grade_level": r.subject.grade_level if r.subject_id else None,
            "topic": topic,
            "type": alias,
            "icon": icon_map.get(alias, "fa-solid fa-file"),
            "color": color_map.get(alias, "#3a86d4"),
            "meta": meta,
            "url": url,
            "uploaded_by": r.uploaded_by.email if r.uploaded_by_id else "",
            "created_at": r.created_at.strftime("%b %d, %Y"),
        })
    vcs = VirtualClassroom.objects.filter(
        status="active", academic_year__status=AcademicYear.STATUS_ACTIVE
    )
    if not request.user.is_superuser and not request.user.groups.filter(name="system_admin").exists():
        vcs = vcs.filter(teacher=request.user)
    vcs = vcs.select_related(
        "subject",
        "subject__academic_year",
        "section",
        "section__academic_year",
        "academic_year",
    ).order_by("subject__name", "section__name")
    classrooms = [{
        "id": vc.id,
        "name": str(vc),
        "subject": vc.subject.name,
        "subject_id": vc.subject_id,
        "grade_level": vc.subject.grade_level,
        "academic_year": vc.academic_year.label if vc.academic_year_id else "",
    } for vc in vcs]
    from apps.academic_structure.models import Subject
    repository_subjects = []
    if is_teacher:
        repository_subject_ids = set(qs.values_list("subject_id", flat=True))
        if not requested_vc:
            selected_year = None
            if requested_year_id:
                selected_year = AcademicYear.objects.filter(pk=requested_year_id).first()
            if selected_year is None:
                selected_year = AcademicYear.objects.filter(
                    status=AcademicYear.STATUS_ACTIVE
                ).order_by("-start_date", "-pk").first()
            if selected_year:
                repository_subject_ids.update(
                    Subject.objects.filter(academic_year=selected_year).values_list("pk", flat=True)
                )
        repository_subjects = [
            {
                "id": subject.id,
                "name": subject.name,
                "grade_level": subject.grade_level,
                "grading_period": subject.grading_period,
            }
            for subject in Subject.objects.filter(
                pk__in=repository_subject_ids,
            ).select_related("academic_year").order_by("grade_level", "name", "grading_period", "pk")
        ]
    return _ok(resources=items, virtual_classrooms=classrooms, repository_subjects=repository_subjects)


# ---------------------------------------------------------------------------
# POST /api/learning-resources/attach/ — copy a resource into a classroom
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_perm_required("manage_learning_resources", "upload_learning_resource", allow_teacher=True)
def attach_learning_resource(request):
    data = _payload(request)
    resource = LearningResource.objects.filter(
        pk=data.get("resource_id"), status=LearningResource.STATUS_ACTIVE
    ).select_related("subject").first()
    if resource is None:
        return _err("Learning resource not found.")
    is_platform_admin = request.user.is_superuser or request.user.groups.filter(name="system_admin").exists()
    if not is_platform_admin and not (
        resource.share_to_repository or resource.uploaded_by_id == request.user.pk
    ):
        return _err("This resource is not available in your repository.", 403)
    vcs = VirtualClassroom.objects.filter(
        pk=data.get("virtual_classroom_id"),
        status="active",
        academic_year__status=AcademicYear.STATUS_ACTIVE,
    )
    if not request.user.is_superuser and not request.user.groups.filter(name="system_admin").exists():
        vcs = vcs.filter(teacher=request.user)
    vc = vcs.first()
    if vc is None:
        return _err("Virtual classroom not found.")
    same_subject = (
        resource.subject_id == vc.subject_id
        or (
            resource.subject.grade_level == vc.subject.grade_level
            and resource.subject.name.strip().casefold() == vc.subject.name.strip().casefold()
        )
    )
    if not same_subject:
        return _err("Only resources for the same subject and grade can be selected.", 403)
    topic = Topic.objects.filter(virtual_classroom=vc).order_by("order").first()
    if topic is None:
        topic = Topic.objects.create(
            virtual_classroom=vc, name="General Resources", order=0
        )
    placement, created = LearningResourcePlacement.objects.get_or_create(
        resource=resource,
        virtual_classroom=vc,
        topic=topic,
        defaults={"placed_by": request.user},
    )
    if created:
        try:
            notif_service.notify_resource_placement(placement)
        except Exception:
            pass
    _log(
        request.user,
        "resource_linked",
        entity_type="LearningResource",
        entity_id=resource.pk,
    )
    return _ok(
        placement_id=placement.pk,
        created=created,
        virtual_classroom=str(vc),
    )


@login_required
@require_POST
@api_perm_required("create_assessment_custom", allow_teacher=True)
@transaction.atomic
def assessment_repository_copy(request):
    """Copy a shared archive assessment into this year's assessment builder."""
    data = _payload(request)
    try:
        source_id = int(data.get("assessment_id"))
        raw_ids = data.get("virtual_classroom_ids")
        if isinstance(raw_ids, str):
            raw_ids = json.loads(raw_ids)
        target_ids = sorted({int(value) for value in (raw_ids or [])})
    except (TypeError, ValueError, json.JSONDecodeError):
        return _err("Select an assessment and at least one destination classroom.", 400)
    if not target_ids:
        return _err("Select at least one destination classroom.", 400)

    source = Assessment.objects.filter(
        pk=source_id, share_to_repository=True
    ).select_related("subject").prefetch_related("parts").first()
    if source is None:
        return _err("This archived assessment is no longer available.", 404)
    classrooms = list(
        VirtualClassroom.objects.filter(
            pk__in=target_ids,
            teacher=request.user,
            status=VirtualClassroom.STATUS_ACTIVE,
            academic_year__status=AcademicYear.STATUS_ACTIVE,
        ).select_related("subject", "academic_year")
    )
    if len(classrooms) != len(target_ids):
        return _err("One or more destination classrooms are unavailable.", 403)
    compatible = [
        vc for vc in classrooms
        if vc.subject.grade_level == source.subject.grade_level
        and vc.subject.name.strip().casefold() == source.subject.name.strip().casefold()
    ]
    if len(compatible) != len(classrooms):
        return _err("Choose current-year classrooms for the assessment's subject and grade.", 400)

    clones_by_subject = {}
    cloned_ids = []
    for vc in compatible:
        clone = clones_by_subject.get(vc.subject_id)
        if clone is None:
            clone = Assessment.objects.create(
                subject=vc.subject,
                academic_year=vc.academic_year,
                created_by=request.user,
                title=source.title,
                assessment_method=source.assessment_method,
                assessment_type=source.assessment_type,
                instructions=source.instructions,
                lifecycle_stage=Assessment.STAGE_CREATED,
                available_at=None,
                submission_deadline=timezone.now() + timedelta(days=7),
                time_limit_minutes=source.time_limit_minutes,
                passing_score_percentage=source.passing_score_percentage,
                accepted_file_format=source.accepted_file_format,
                allow_submission_attachments=source.allow_submission_attachments,
                allow_submission_links=source.allow_submission_links,
                reveal_correct_answers=source.reveal_correct_answers,
                share_to_repository=False,
                status=Assessment.STATUS_ACTIVE,
                notification_settings=source.notification_settings,
            )
            for part in source.parts.all():
                AssessmentPart.objects.create(
                    assessment=clone,
                    question_type=part.question_type,
                    question_text=part.question_text,
                    order=part.order,
                    items=part.items,
                    attached_file=part.attached_file.name if part.attached_file else "",
                )
            clones_by_subject[vc.subject_id] = clone
            cloned_ids.append(clone.pk)
        # The assessment builder and classroom views discover assessments
        # through these assignments, including when sections share a subject.
        AssessmentAssignment.objects.get_or_create(
            assessment=clone,
            virtual_classroom=vc,
            defaults={"assigned_by": request.user},
        )

    return _ok(
        assessment_ids=cloned_ids,
        assessment_url=reverse("assessments:assessment_detail", args=[cloned_ids[0]]),
        copied_classrooms=len(compatible),
    )


# ---------------------------------------------------------------------------
# GET /api/attendance/ — saved attendance for a date range, per learner
# ---------------------------------------------------------------------------
@login_required
@require_GET
@api_perm_required("view_attendance_records", "record_class_attendance")
def attendance_calendar(request):
    """The caller's saved attendance, keyed by date and learner.

    The teacher dashboard used to paint its attendance calendar from the
    browser's localStorage only: a record saved on the office machine was
    invisible on the classroom laptop, and clearing site data wiped the view of
    records that were safely in PostgreSQL. This endpoint is the read side of
    ``POST /api/attendance/`` so the calendar is rebuilt from the database.

    The response mirrors the shape the front end already stores::

        {
          "2026-09-01": {"<student id>": "P" | "L" | "A"},
          "2026-09-01.sub": {"<student id>": {"sub": "late", "remark": "..."}},
        }

    Only the caller's own classrooms are read, and the range is capped at 365
    days so the complete academic-year attendance view can be rebuilt.
    """
    from apps.attendance.models import AttendanceRecord, AttendanceSession

    today = timezone.localdate()

    def parse(value, fallback):
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return fallback

    start = parse(request.GET.get("from"), today.replace(day=1))
    end = parse(request.GET.get("to"), start + timedelta(days=31))
    if end < start:
        start, end = end, start
    if (end - start).days > 365:
        end = start + timedelta(days=365)

    status_code = {
        AttendanceRecord.PRESENT_ON_TIME: "P",
        AttendanceRecord.PRESENT_LATE: "L",
        AttendanceRecord.ABSENT_EXCUSED: "A",
        AttendanceRecord.ABSENT_UNEXCUSED: "A",
    }
    sub_code = {
        AttendanceRecord.PRESENT_ON_TIME: "ontime",
        AttendanceRecord.PRESENT_LATE: "late",
        AttendanceRecord.ABSENT_EXCUSED: "excused",
        AttendanceRecord.ABSENT_UNEXCUSED: "unexcused",
    }

    sessions = AttendanceSession.objects.filter(
        virtual_classroom__teacher=request.user,
        virtual_classroom__status=STATUS_ACTIVE,
        session_date__gte=start,
        session_date__lte=end,
    )
    calendar = {}
    records = (
        AttendanceRecord.objects.filter(session__in=sessions)
        .select_related("session")
        .order_by("session__session_date", "pk")
    )
    for record in records:
        code = status_code.get(record.status)
        if code is None:
            continue
        day = record.session.session_date.isoformat()
        calendar.setdefault(day, {})[str(record.student_id)] = code
        calendar.setdefault(day + ".sub", {})[str(record.student_id)] = {
            "sub": sub_code.get(record.status, "ontime"),
            "remark": record.remark or "",
        }
    return _ok(from_date=start.isoformat(), to_date=end.isoformat(), calendar=calendar)


# ---------------------------------------------------------------------------
# POST /api/attendance/ — record a session + per-student statuses
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_perm_required("update_attendance_record_custom", "record_class_attendance")
def attendance(request):
    data = _payload(request)
    vc = None
    if data.get("virtual_classroom_id"):
        try:
            cid = int(data.get("virtual_classroom_id"))
        except (TypeError, ValueError):
            return _err("Invalid virtual_classroom_id.")
        vc = VirtualClassroom.objects.filter(pk=cid, teacher=request.user, status=STATUS_ACTIVE).first()
    if vc is None:
        return _ok(count=0, skipped="no_virtual_classroom")

    day = data.get("date") or data.get("session_date")
    if day:
        try:
            session_date = date.fromisoformat(str(day)[:10])
        except ValueError:
            session_date = timezone.localdate()
    else:
        session_date = timezone.localdate()

    session, _ = AttendanceSession.objects.get_or_create(
        virtual_classroom=vc, session_date=session_date, defaults={"created_by": request.user}
    )

    status_map = {
        "P": AttendanceRecord.PRESENT_ON_TIME,
        "O": AttendanceRecord.PRESENT_ON_TIME,
        "present": AttendanceRecord.PRESENT_ON_TIME,
        "present_on_time": AttendanceRecord.PRESENT_ON_TIME,
        "present-ontime": AttendanceRecord.PRESENT_ON_TIME,
        "L": AttendanceRecord.PRESENT_LATE,
        "late": AttendanceRecord.PRESENT_LATE,
        "present_late": AttendanceRecord.PRESENT_LATE,
        "present-late": AttendanceRecord.PRESENT_LATE,
        "E": AttendanceRecord.ABSENT_EXCUSED,
        "excused": AttendanceRecord.ABSENT_EXCUSED,
        "absent_excused": AttendanceRecord.ABSENT_EXCUSED,
        "absent-excused": AttendanceRecord.ABSENT_EXCUSED,
        "A": AttendanceRecord.ABSENT_UNEXCUSED,
        "absent": AttendanceRecord.ABSENT_UNEXCUSED,
        "absent_unexcused": AttendanceRecord.ABSENT_UNEXCUSED,
        "absent-unexcused": AttendanceRecord.ABSENT_UNEXCUSED,
    }

    records = data.get("records") or data.get("attendance") or []
    count = skipped = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        lrn = str(rec.get("lrn") or "").strip()
        name = str(rec.get("name") or "").strip()
        status_raw = str(rec.get("status") or "P")
        status = (
            status_map.get(status_raw.lower())
            or status_map.get(status_raw)
            or AttendanceRecord.PRESENT_ON_TIME
        )
        profile = None
        if lrn.isdigit() and len(lrn) == 12:
            profile = StudentProfile.objects.filter(lrn=lrn).first()
        if profile is None and name:
            parts = name.split()
            profile = StudentProfile.objects.filter(last_name=parts[-1], first_name=parts[0]).first()
        if profile is None:
            skipped += 1
            continue
        try:
            AttendanceRecord.objects.update_or_create(
                session=session, student=profile,
                defaults={"status": status, "remark": (rec.get("remark") or "")[:500]},
            )
            count += 1
        except ValidationError:
            # e.g. an excused absence without the mandatory note/attachment.
            skipped += 1
            continue
        # Tell the student which attendance status was recorded, including
        # present and late. A notification failure must never block the save.
        label = {
            AttendanceRecord.PRESENT_ON_TIME: "present",
            AttendanceRecord.PRESENT_LATE: "late",
            AttendanceRecord.ABSENT_EXCUSED: "absent (excused)",
            AttendanceRecord.ABSENT_UNEXCUSED: "absent",
        }.get(status, "updated")
        try:
            notif_service.sync_attendance_notification(
                user_id=profile.user_id,
                virtual_classroom=vc,
                session=session,
                status_label=label,
            )
        except Exception:  # pragma: no cover - notification is an enhancement
            pass
    _log(request.user, f"attendance_saved:{session_date}", entity_type="AttendanceSession", entity_id=session.pk)
    return _ok(count=count, skipped=skipped, virtual_classroom_id=vc.pk, session_date=str(session_date))


# ---------------------------------------------------------------------------
# POST /api/assessments/ — teacher assessment builder publish
# ---------------------------------------------------------------------------
_ASSESSMENT_IMAGE_MAX_BYTES = 350 * 1024


def _assessment_image_data(value):
    """Validate and retain a small raster image data URL from the builder."""
    if isinstance(value, dict):
        if value.get("kind") != "image":
            return ""
        value = value.get("data") or ""
    if not isinstance(value, str) or not value.startswith("data:image/"):
        # Older builder versions sent the filename only; it cannot be rendered.
        return ""
    match = re.fullmatch(
        r"data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/]*={0,2})",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValidationError("Assessment images must be valid PNG, JPEG, WEBP, or GIF files.")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error):
        raise ValidationError("An assessment image could not be decoded.")
    if len(raw) > _ASSESSMENT_IMAGE_MAX_BYTES:
        raise ValidationError("Each assessment image must be 350 KB or smaller after compression.")
    return value


def _compile_assessment_parts(data):
    """Validate the builder payload and preserve its part/question identities."""
    type_code = {
        "mc": AssessmentPart.TYPE_MULTIPLE_CHOICE,
        "multiple_choice": AssessmentPart.TYPE_MULTIPLE_CHOICE,
        "tf": AssessmentPart.TYPE_TRUE_FALSE,
        "true_false": AssessmentPart.TYPE_TRUE_FALSE,
        "mt": AssessmentPart.TYPE_MATCHING,
        "matching": AssessmentPart.TYPE_MATCHING,
        "id": AssessmentPart.TYPE_IDENTIFICATION,
        "identification": AssessmentPart.TYPE_IDENTIFICATION,
        "sa": AssessmentPart.TYPE_SHORT_ANSWER,
        "short": AssessmentPart.TYPE_SHORT_ANSWER,
        "numeric": AssessmentPart.TYPE_NUMERIC,
        "es": AssessmentPart.TYPE_ESSAY,
        "essay": AssessmentPart.TYPE_ESSAY,
        "pp": AssessmentPart.TYPE_POSITION_PAPER,
        "cs": AssessmentPart.TYPE_CASE_STUDY,
        "nr": AssessmentPart.TYPE_NARRATIVE_REPORT,
        "pt": AssessmentPart.TYPE_ESSAY,
    }
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValidationError("Add at least one valid question before saving.")
    part_specs = data.get("parts")
    if part_specs is not None and not isinstance(part_specs, list):
        raise ValidationError("Assessment parts must be a list.")
    specs_by_id = {}
    ordered_ids = []
    for spec in part_specs or []:
        if not isinstance(spec, dict) or not spec.get("id"):
            raise ValidationError("Each assessment part must have an ID.")
        token = str(spec["id"])
        if token in specs_by_id:
            raise ValidationError("Assessment part IDs must be unique.")
        specs_by_id[token] = spec
        ordered_ids.append(token)

    grouped = {}
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            raise ValidationError("Question {} is invalid.".format(index))
        question_text = str(question.get("text") or question.get("question") or "").strip()
        if not question_text:
            raise ValidationError("Question {} needs question text.".format(index))
        raw_type = str(question.get("type") or "mc").lower()
        question_type = type_code.get(raw_type)
        if question_type is None:
            raise ValidationError("Question {} has an unsupported question type.".format(index))
        raw_points = question.get("points", question.get("point", 1))
        try:
            point_value = Decimal(str(raw_points))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError("Question {} needs a whole-number point value.".format(index))
        if (
            isinstance(raw_points, bool)
            or not point_value.is_finite()
            or point_value != point_value.to_integral_value()
        ):
            raise ValidationError("Question {} needs a whole-number point value.".format(index))
        points = int(point_value)
        if points < 1 or points > 100:
            raise ValidationError("Question {} points must be between 1 and 100.".format(index))
        token = str(question.get("partId") or "legacy:" + question_type)
        part_spec = specs_by_id.get(token)
        if part_spec and part_spec.get("qtype"):
            spec_type = type_code.get(str(part_spec["qtype"]).lower())
            if spec_type != question_type:
                raise ValidationError("Every question must match its assessment part type.")
        group = grouped.setdefault(
            token,
            {
                "token": token,
                "label": str((specs_by_id.get(token) or {}).get("label") or ""),
                "question_type": question_type,
                "items": [],
            },
        )
        if group["question_type"] != question_type:
            raise ValidationError("Every question in a part must use the same question type.")

        item = {"question": question_text, "point": points}
        try:
            image = _assessment_image_data(question.get("media"))
            if image:
                item["image"] = image
            opt_images = question.get("optImgs") if isinstance(question.get("optImgs"), dict) else {}
            if question_type == AssessmentPart.TYPE_MULTIPLE_CHOICE:
                options = question.get("options")
                if not isinstance(options, list):
                    raise ValidationError("Question {} needs at least two choices.".format(index))
                choices, choice_images, source_indexes = [], [], []
                for source_index, raw_option in enumerate(options[:4]):
                    letter = chr(65 + source_index)
                    choice_image = _assessment_image_data(opt_images.get(letter, ""))
                    option_text = str(raw_option or "").strip()
                    if not option_text and not choice_image:
                        continue
                    choices.append(option_text or "Choice {}".format(letter))
                    choice_images.append(choice_image)
                    source_indexes.append(source_index)
                if len(choices) < 2:
                    raise ValidationError("Question {} needs at least two choices.".format(index))
                try:
                    correct_value = Decimal(str(question.get("correct", 0)))
                except (InvalidOperation, TypeError, ValueError):
                    raise ValidationError("Question {} has an invalid correct choice.".format(index))
                if (
                    not correct_value.is_finite()
                    or correct_value != correct_value.to_integral_value()
                ):
                    raise ValidationError("Question {} has an invalid correct choice.".format(index))
                correct_source = int(correct_value)
                if correct_source not in source_indexes:
                    raise ValidationError("Question {} must select one of its choices as correct.".format(index))
                correct_index = source_indexes.index(correct_source)
                item.update(
                    choices=choices,
                    choice_images=choice_images,
                    correct_answer=choices[correct_index],
                )
            elif question_type == AssessmentPart.TYPE_TRUE_FALSE:
                item["choices"] = ["True", "False"]
                item["correct_answer"] = "False" if question.get("tf") is False else "True"
            elif question_type in (
                AssessmentPart.TYPE_IDENTIFICATION,
                AssessmentPart.TYPE_SHORT_ANSWER,
            ):
                answers = question.get("answers") or question.get("accepted_answers")
                if not answers:
                    answers = [question.get("idAns") or question.get("saAns") or ""]
                if isinstance(answers, str):
                    answers = [answer.strip() for answer in answers.splitlines() if answer.strip()]
                elif isinstance(answers, list):
                    answers = [str(answer).strip() for answer in answers if str(answer).strip()]
                else:
                    raise ValidationError("Question {} accepted answers must be a list or text.".format(index))
                if not answers:
                    raise ValidationError("Question {} needs at least one accepted answer.".format(index))
                item["accepted_answers"] = answers
            elif question_type == AssessmentPart.TYPE_NUMERIC:
                answer = question.get("answer", question.get("correct_answer"))
                if answer in (None, ""):
                    raise ValidationError("Question {} needs the correct numeric answer.".format(index))
                try:
                    numeric_answer = Decimal(str(answer))
                    tolerance = Decimal(str(question.get("tolerance", 0)))
                except (InvalidOperation, TypeError, ValueError):
                    raise ValidationError("Question {} has an invalid numeric answer or tolerance.".format(index))
                if not numeric_answer.is_finite() or not tolerance.is_finite():
                    raise ValidationError("Question {} numeric values must be finite.".format(index))
                if tolerance < 0:
                    raise ValidationError("Question {} tolerance cannot be negative.".format(index))
                item["correct_answer"] = str(answer)
                item["tolerance"] = float(tolerance)
            elif question_type == AssessmentPart.TYPE_MATCHING:
                raw_pairs = question.get("matches") or question.get("match") or ""
                if isinstance(raw_pairs, dict):
                    pairs = list(raw_pairs.items())
                else:
                    pairs = []
                    for line in str(raw_pairs).replace(";", "\n").splitlines():
                        if not line.strip():
                            continue
                        left, separator, right = line.partition("=")
                        if not separator or not left.strip() or not right.strip():
                            raise ValidationError(
                                "Matching question {} must use one 'Left = Right' pair per line.".format(index)
                            )
                        pairs.append((left.strip(), right.strip()))
                if not pairs:
                    raise ValidationError("Matching question {} needs at least one pair.".format(index))
                premises = [str(left).strip() for left, _right in pairs]
                matches = {str(left).strip(): str(right).strip() for left, right in pairs}
                if len(matches) != len(premises) or any(not left or not right for left, right in matches.items()):
                    raise ValidationError("Matching question {} has duplicate or empty pairs.".format(index))
                item.update(
                    premises=premises,
                    targets=[str(right).strip() for _left, right in pairs],
                    correct_matches=matches,
                )
            else:
                item["grading_rubric"] = question.get("rubric") or []
        except ValidationError as exc:
            raise ValidationError("Question {}: {}".format(index, "; ".join(exc.messages)))

        item_key = str(question.get("itemKey") or "").strip()
        group["items"].append((item_key, item))

    if ordered_ids:
        missing_groups = set(grouped) - set(ordered_ids)
        if missing_groups:
            raise ValidationError("A question refers to an unknown assessment part.")
        ordered_groups = []
        for token in ordered_ids:
            group = grouped.get(token)
            if group is None:
                continue
            ordered_groups.append(group)
    else:
        ordered_groups = list(grouped.values())

    for group in ordered_groups:
        used_keys = {key for key, _item in group["items"] if key}
        if len(used_keys) != sum(bool(key) for key, _item in group["items"]):
            raise ValidationError("Question IDs must be unique within each part.")
        next_number = 1
        compiled_items = {}
        for key, item in group["items"]:
            if not key:
                while "item_{}".format(next_number) in used_keys:
                    next_number += 1
                key = "item_{}".format(next_number)
                used_keys.add(key)
                next_number += 1
            compiled_items[key] = item
        group["items"] = compiled_items
    return ordered_groups


def _assessment_parts_changed(assessment, groups):
    existing_parts = {part.pk: part for part in assessment.parts.all()}
    wanted_ids = set()
    for order, group in enumerate(groups):
        token = group["token"]
        part = None
        if token.startswith("p") and token[1:].isdigit():
            part = existing_parts.get(int(token[1:]))
        if part is None:
            return True
        wanted_ids.add(part.pk)
        if (
            part.order != order
            or part.question_type != group["question_type"]
            or part.question_text != group["label"]
            or (part.items or {}) != group["items"]
        ):
            return True
    return wanted_ids != set(existing_parts)


@login_required
@require_POST
@api_perm_required("update_assessment_custom", "archive_assessment_custom", "create_assessment_custom", allow_teacher=True)
@transaction.atomic
def assessments(request):
    data = _payload(request)
    if not isinstance(data, dict):
        return _err("Assessment data must be an object.", 400)
    title = (data.get("title") or "").strip()
    if not title:
        return _err("Assessment title is required.")
    vc = None
    if data.get("virtual_classroom_id"):
        try:
            cid = int(data.get("virtual_classroom_id"))
        except (TypeError, ValueError):
            return _err("Invalid virtual_classroom_id.")
        vc = VirtualClassroom.objects.filter(pk=cid, teacher=request.user, status=STATUS_ACTIVE).first()
    if vc is None:
        return _err("Open an active virtual classroom that you teach before publishing an assessment.")

    method_map = {
        "Quiz": Assessment.METHOD_QUIZ,
        "Quizzes": Assessment.METHOD_QUIZ,
        "Seatwork": Assessment.METHOD_SEATWORK,
        "Seatworks": Assessment.METHOD_SEATWORK,
        "Assignment": Assessment.METHOD_ASSIGNMENT,
        "Assignments": Assessment.METHOD_ASSIGNMENT,
        "Examination": Assessment.METHOD_EXAMINATION,
        "Examinations": Assessment.METHOD_EXAMINATION,
        "Project": Assessment.METHOD_PROJECT,
        "Projects": Assessment.METHOD_PROJECT,
    }
    type_map = {
        "Quiz": Assessment.TYPE_WRITTEN_ORAL,
        "Quizzes": Assessment.TYPE_WRITTEN_ORAL,
        "Seatwork": Assessment.TYPE_WRITTEN_ORAL,
        "Seatworks": Assessment.TYPE_WRITTEN_ORAL,
        "Assignment": Assessment.TYPE_PRODUCT_PERFORMANCE,
        "Assignments": Assessment.TYPE_PRODUCT_PERFORMANCE,
        "Examination": Assessment.TYPE_SUMMATIVE_TEST,
        "Examinations": Assessment.TYPE_SUMMATIVE_TEST,
        "Project": Assessment.TYPE_PRODUCT_PERFORMANCE,
        "Projects": Assessment.TYPE_PRODUCT_PERFORMANCE,
    }
    a_label = str(data.get("type") or "Quiz")
    method = method_map.get(a_label, method_map["Quiz"])
    a_type = type_map.get(a_label, Assessment.TYPE_WRITTEN_ORAL)

    def _parse_dt(value):
        value = (value or "").strip() if isinstance(value, str) else ""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed

    available_at = _parse_dt(data.get("openDate"))
    submission_deadline = _parse_dt(data.get("deadline"))
    reopen_assessment = data.get("reopen") is True or str(data.get("reopen") or "").lower() in {"1", "true", "yes", "on"}
    if reopen_assessment and (
        submission_deadline is None or submission_deadline <= timezone.now()
    ):
        return _err("Choose a future closing date to reopen this assessment.", 400)
    time_limit = None
    if "timeLimit" in data:
        try:
            time_limit_value = Decimal(str(data["timeLimit"]))
        except (InvalidOperation, TypeError, ValueError):
            return _err("Time limit must be a whole number between 1 and 300 minutes.", 400)
        if (
            isinstance(data["timeLimit"], bool)
            or not time_limit_value.is_finite()
            or time_limit_value != time_limit_value.to_integral_value()
        ):
            return _err("Time limit must be a whole number between 1 and 300 minutes.", 400)
        time_limit = int(time_limit_value)
        if not 1 <= time_limit <= 300:
            return _err("Time limit must be between 1 and 300 minutes.", 400)
    passing_score = None
    if "passingScore" in data:
        try:
            passing_score_value = Decimal(str(data["passingScore"]))
        except (InvalidOperation, TypeError, ValueError):
            return _err("Passing score must be a whole number between 1 and 100.", 400)
        if (
            isinstance(data["passingScore"], bool)
            or not passing_score_value.is_finite()
            or passing_score_value != passing_score_value.to_integral_value()
        ):
            return _err("Passing score must be a whole number between 1 and 100.", 400)
        passing_score = int(passing_score_value)
        if not 1 <= passing_score <= 100:
            return _err("Passing score must be between 1 and 100 percent.", 400)
    assessment_options = {
        "allowSubmissionAttachments": "allow_submission_attachments",
        "allowSubmissionLinks": "allow_submission_links",
        "revealCorrectAnswers": "reveal_correct_answers",
        "shareToRepository": "share_to_repository",
    }
    for key in assessment_options:
        if key in data and not isinstance(data[key], bool):
            return _err("{} must be true or false.".format(key), 400)
    accepted_file_format = None
    if "acceptedFileFormat" in data:
        if not isinstance(data["acceptedFileFormat"], str) or len(data["acceptedFileFormat"]) > 100:
            return _err("Accepted file formats must be text up to 100 characters.", 400)
        accepted_file_format = data["acceptedFileFormat"].strip()
    groups = None
    if "questions" in data:
        try:
            groups = _compile_assessment_parts(data)
        except ValidationError as exc:
            return _err("; ".join(exc.messages), 400)

    requested_assessment_id = data.get("assessment_id")
    if requested_assessment_id:
        try:
            requested_assessment_id = int(requested_assessment_id)
        except (TypeError, ValueError):
            return _err("Invalid assessment_id.", 400)
        assessment = Assessment.objects.filter(
            pk=requested_assessment_id,
            subject=vc.subject,
            created_by=request.user,
            assignments__virtual_classroom=vc,
        ).first()
        if assessment is None:
            return _err("Assessment not found or you cannot edit it.", 403)
    else:
        # A title is display text, not an edit identifier. The teacher UI sends
        # assessment_id for edits; without it this request is a new assessment,
        # even when another assessment has the same title.
        assessment = None
    if assessment is None and groups is None:
        return _err("Add at least one question before publishing an assessment.", 400)
    if (
        assessment is not None
        and groups is not None
        and _assessment_parts_changed(assessment, groups)
        and (
            Submission.objects.filter(
                assessment_assignment__assessment=assessment
            ).exists()
            or AssessmentAttempt.objects.filter(
                assignment__assessment=assessment
            ).exists()
        )
    ):
        return _err(
            "Question content cannot be changed after students have started an attempt. "
            "You can still update assessment settings without changing questions.",
            409,
        )
    if (
        assessment is not None
        and Submission.objects.filter(assessment_assignment__assessment=assessment).exists()
        and (
            (passing_score is not None and passing_score != assessment.passing_score_percentage)
            or method != assessment.assessment_method
            or a_type != assessment.assessment_type
        )
    ):
        return _err(
            "Assessment type and passing score cannot be changed after a student submits.",
            409,
        )
    if (
        assessment is not None
        and assessment.submission_deadline <= timezone.now()
        and submission_deadline is not None
        and submission_deadline > timezone.now()
        and not reopen_assessment
    ):
        return _err("Select the reopen option to extend a closed assessment.", 400)
    if assessment is None:
        assessment = Assessment.objects.create(
            subject=vc.subject, academic_year=vc.academic_year, created_by=request.user, title=title,
            assessment_method=method, assessment_type=a_type,
            instructions=(data.get("instructions") or ""),
            available_at=available_at,
            submission_deadline=submission_deadline or (timezone.now() + timedelta(days=1)),
            time_limit_minutes=time_limit or 60,
            passing_score_percentage=passing_score or 60,
            lifecycle_stage=Assessment.STAGE_CREATED,
            **{
                field: data.get(key, field == "reveal_correct_answers")
                for key, field in assessment_options.items()
            },
            accepted_file_format=accepted_file_format or "",
        )
    else:
        update_fields = []
        if assessment.title != title:
            assessment.title = title
            update_fields.append("title")
        if "instructions" in data and assessment.instructions != str(data.get("instructions") or ""):
            assessment.instructions = str(data.get("instructions") or "")
            update_fields.append("instructions")
        for key, field in assessment_options.items():
            if key in data and getattr(assessment, field) != data[key]:
                setattr(assessment, field, data[key])
                update_fields.append(field)
        if accepted_file_format is not None and assessment.accepted_file_format != accepted_file_format:
            assessment.accepted_file_format = accepted_file_format
            update_fields.append("accepted_file_format")
        if assessment.assessment_method != method:
            assessment.assessment_method = method
            update_fields.append("assessment_method")
        if assessment.assessment_type != a_type:
            assessment.assessment_type = a_type
            update_fields.append("assessment_type")
        if time_limit is not None and assessment.time_limit_minutes != time_limit:
            assessment.time_limit_minutes = time_limit
            update_fields.append("time_limit_minutes")
        if passing_score is not None and assessment.passing_score_percentage != passing_score:
            assessment.passing_score_percentage = passing_score
            update_fields.append("passing_score_percentage")
        if available_at is not None:
            assessment.available_at = available_at
            update_fields.append("available_at")
        if submission_deadline is not None:
            assessment.submission_deadline = submission_deadline
            update_fields.append("submission_deadline")
        if reopen_assessment and assessment.lifecycle_stage == Assessment.STAGE_COMPLETED:
            assessment.lifecycle_stage = Assessment.STAGE_AVAILABLE
            update_fields.append("lifecycle_stage")
        if update_fields:
            assessment.save(update_fields=update_fields)

    # Publishing from the teacher builder makes this assessment visible to
    # students. A title match above can find an older archived record, and the
    # student dashboard intentionally filters archived assessments out.
    if assessment.status != Assessment.STATUS_ACTIVE:
        assessment.status = Assessment.STATUS_ACTIVE
        assessment.save(update_fields=["status"])

    assignment, _ = AssessmentAssignment.objects.get_or_create(
        assessment=assessment,
        virtual_classroom=vc,
        defaults={"assigned_by": request.user},
    )
    try:
        notif_service.notify_assessment_assignment(assignment)
    except Exception:
        pass

    if groups is not None:
        existing_parts = {part.pk: part for part in assessment.parts.all()}
        kept_ids = set()
        for order, group in enumerate(groups):
            part = None
            token = group["token"]
            if token.startswith("p") and token[1:].isdigit():
                part = existing_parts.get(int(token[1:]))
            if part is None:
                part = AssessmentPart(assessment=assessment)
            else:
                kept_ids.add(part.pk)
            part.question_type = group["question_type"]
            part.order = order
            part.items = group["items"]
            if not part.question_text:
                part.question_text = group["label"]
            part.save()
            kept_ids.add(part.pk)
        AssessmentPart.objects.filter(assessment=assessment).exclude(pk__in=kept_ids).delete()

    _log(request.user, f"assessment_saved:{title}", entity_type="Assessment", entity_id=assessment.pk)
    try:
        notify_enrolled_students(vc.pk, kind=["assessments"])
    except Exception:
        pass
    return _ok(assessment_id=assessment.pk)


# ---------------------------------------------------------------------------
# POST /api/system-settings/ — timeouts, registration, maintenance, backup prefs
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required(*ADMIN_ROLES)
def system_settings(request):
    data = _payload(request)
    cfg = SystemConfiguration.get_solo()
    changed = False
    invalid = []

    def _int(key, lo, hi):
        nonlocal changed
        if data.get(key) is not None:
            try:
                value = int(data[key])
            except (TypeError, ValueError):
                invalid.append(f"{key} must be a whole number.")
                return
            if not lo <= value <= hi:
                invalid.append(f"{key} must be between {lo} and {hi}.")
                return
            setattr(cfg, {
                "sessionTimeout": "session_timeout_minutes",
                "lockoutThreshold": "account_lockout_threshold",
            }[key], value)
            changed = True

    _int("sessionTimeout", 1, 120)
    _int("lockoutThreshold", 1, 20)

    if invalid:
        return _err(" ".join(invalid), 400)
    if "registration" in data and data["registration"] not in dict(SystemConfiguration.REGISTRATION_CHOICES):
        return _err("Select a valid user registration policy.", 400)
    if data.get("registration") in dict(SystemConfiguration.REGISTRATION_CHOICES):
        cfg.user_registration_policy = data["registration"]
        changed = True
    if data.get("maintenance") is not None:
        cfg.maintenance_mode = bool(data["maintenance"])
        changed = True
    if data.get("backupSettings") and isinstance(data["backupSettings"], dict):
        settings = dict(cfg.backup_settings or {})
        settings.update(data["backupSettings"])
        cfg.backup_settings = settings
        changed = True
    if changed:
        cfg.updated_by = request.user
        cfg.save()
    return _ok(changed=changed)


@login_required
@require_POST
@api_role_required(*ADMIN_ROLES)
def password_policy(request):
    data = _payload(request)
    policy = PasswordPolicy.get_solo()
    changed = False
    for field, key, lo, hi in (("minimum_length", "minLength", 1, 64),
                               ("expiration_days", "expiration", 0, 365),
                               ("reuse_limit", "reuse", 0, 50)):
        if data.get(key) is not None:
            try:
                value = int(data[key])
            except (TypeError, ValueError):
                continue
            if lo <= value <= hi and getattr(policy, field) != value:
                setattr(policy, field, value)
                changed = True
    for field, key in (("require_uppercase", "upper"), ("require_lowercase", "lower"),
                       ("require_number", "number"), ("require_symbol", "special")):
        if data.get(key) is not None and getattr(policy, field) != bool(data[key]):
            setattr(policy, field, bool(data[key]))
            changed = True
    if changed:
        policy.updated_by = request.user
        policy.save()
    return _ok(changed=changed)


# ---------------------------------------------------------------------------
# POST /api/account-actions/ — unlock a locked account, issue a temp password
# ---------------------------------------------------------------------------
def _resolve_action_target(data):
    """Find the User an account action is aimed at, by id or by email."""
    raw_id = data.get("user_id", data.get("id"))
    if raw_id not in (None, ""):
        try:
            return User.objects.filter(pk=int(raw_id)).first()
        except (TypeError, ValueError):
            return None
    email = (data.get("email") or "").strip().lower()
    if email:
        return User.objects.filter(email=email).first()
    return None


@csrf_protect
def account_actions(request):
    """Account recovery actions for the System Admin dashboard.

    ``GET``
        Returns the current locked-account list so the card can refresh after an
        action without a full page reload.

    ``{"action": "unlock", "user_id": 12}``
        Clear the lockout and the failed-attempt counter, leaving the password
        alone. Requires ``accounts.unlock_account``.

    ``{"action": "temp_password", "user_id": 12}``
        Set a password the administrator hands over, and unlock the account.
        Requires ``accounts.reset_account_password``. Omit ``password`` (or send
        an empty one) to have a compliant password generated.

    Permission is checked per action rather than for the whole endpoint: the
    registrar may unlock a locked student but must not be able to hand out a
    password from the JSON bridge.

    ``@login_required`` is deliberately not used here. It answers an
    unauthenticated call with a 302 to the HTML login page, which the
    dashboard's JSON caller cannot read -- it surfaced as a generic parse
    failure instead of "your session expired".
    """
    if not request.user.is_authenticated:
        return _err("Authentication required.", 401)

    if request.method == "GET":
        return _ok(
            locked=[
                serialize_locked_account(
                    locked_user,
                    can_unlock=has_permission(request.user, "unlock_account"),
                    can_reset=has_permission(request.user, "reset_account_password"),
                )
                for locked_user in locked_accounts_queryset(limit=20)
            ],
            locked_total=User.objects.filter(status=User.Status.LOCKED).count(),
        )

    # Everything past this point changes state, so only POST may reach it.
    if request.method != "POST":
        return _err("Method not allowed.", 405)

    data = _payload(request)
    action = (data.get("action") or "").strip().lower()

    if action == "unlock":
        required = "unlock_account"
    elif action in ("temp_password", "temporary_password", "reset_password"):
        required = "reset_account_password"
    elif action == "resend_verification":
        required = "resend_verification_email"
    elif action == "delete_pending":
        required = "archive_account"
    elif action == "generate_password":
        # No target user: just hand back a compliant password for the form.
        if not has_permission(request.user, "reset_account_password"):
            _log(request.user, "api_denied:account_actions")
            return _err("You do not have permission to perform this action.", 403)
        return _ok(password=generate_temporary_password())
    else:
        return _err(
            "Unknown action {!r}. Expected 'unlock', 'temp_password', "
            "'resend_verification' or 'generate_password'.".format(action),
            400,
        )

    if not has_permission(request.user, required):
        _log(request.user, "api_denied:account_actions")
        return _err("You do not have permission to perform this action.", 403)

    user = _resolve_action_target(data)
    if user is None:
        return _err("No matching account was found.", 404)

    if action == "delete_pending":
        if user.status not in (
            User.Status.PENDING_VERIFICATION,
            User.Status.PENDING_PASSWORD_SETUP,
        ):
            return _err("Only pending accounts can be removed.", 409)
        user_id, email = user.pk, user.email
        _log(
            request.user,
            f"pending_account_removed:{email}",
            entity_type="User",
            entity_id=user_id,
        )
        user.delete()
        return _ok(user_id=user_id, email=email)

    # An administrator locking or unlocking themselves would either lock
    # themselves out mid-session or hand their own account a password only they
    # know, neither of which is a recovery.
    if user.pk == request.user.pk:
        return _err("You cannot perform this action on your own account.", 400)

    if action == "resend_verification":
        ok, payload = _resend_verification_for(user, request)
        if not ok:
            return JsonResponse(
                {"ok": False, **payload}, status=payload.get("status", 400)
            )
        return _ok(**payload)

    try:
        if action == "unlock":
            cleared = unlock_user_account(user, actor=request.user)
            return _ok(
                user_id=user.pk,
                email=user.email,
                cleared_attempts=cleared,
                status=user.status,
            )

        result = issue_temporary_password(
            user,
            new_password=(data.get("password") or "").strip() or None,
            actor=request.user,
        )
    except AccountActionError as exc:
        return _err(str(exc), 400)

    return _ok(
        user_id=user.pk,
        email=user.email,
        password=result["password"],
        was_locked=result["was_locked"],
        status=user.status,
        must_change_password=result["must_change_password"],
        warnings=result["warnings"],
    )


def _backup_dir():
    path = Path(settings.BASE_DIR) / "media" / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backup_history(limit=10):
    """The same rows the System Administrator panel renders."""
    out = []
    for b in (
        BackupRecord.objects.select_related("initiated_by")
        .order_by("-created_at")[:limit]
    ):
        out.append({
            "id": b.id,
            "date": timezone.localtime(b.created_at).strftime("%b %d, %Y · %I:%M %p"),
            "type": "{} · {}".format(
                b.get_backup_type_display() if hasattr(b, "get_backup_type_display") else b.backup_type,
                b.get_status_display() if hasattr(b, "get_status_display") else b.status,
            ),
            "size": None,
            "file": b.file_reference or "—",
            "status": b.get_status_display() if hasattr(b, "get_status_display") else b.status,
            "by": (b.initiated_by.get_full_name() if b.initiated_by else "System"),
        })
    return out


def _pg_dump_path():
    """Locate pg_dump without assuming it is on PATH (Windows install layout)."""
    found = shutil.which("pg_dump")
    if found:
        return found
    for parent in ("C:/Program Files/PostgreSQL", "/usr/lib/postgresql", "/usr/pgsql"):
        base = Path(parent)
        if not base.is_dir():
            continue
        for version in sorted(base.iterdir(), reverse=True):
            for name in ("pg_dump.exe", "pg_dump"):
                candidate = version / "bin" / name
                if candidate.exists():
                    return str(candidate)
    return None


def _run_pg_dump(target):
    """Stream a real dump to `target`. Returns (ok, detail)."""
    exe = _pg_dump_path()
    if not exe:
        return False, "pg_dump is not installed on this host"
    db = settings.DATABASES["default"]
    env = dict(os.environ)
    if db.get("PASSWORD"):
        env["PGPASSWORD"] = db["PASSWORD"]
    cmd = [
        exe,
        "-h", str(db.get("HOST") or "localhost"),
        "-p", str(db.get("PORT") or "5432"),
        "-U", str(db.get("USER") or "postgres"),
        "-d", str(db["NAME"]),
        "-F", "custom",
        "-f", str(target),
    ]
    try:
        done = subprocess.run(
            cmd, env=env, capture_output=True, timeout=60 * 30, check=False,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the administrator
        return False, "{}: {}".format(type(exc).__name__, exc)
    if done.returncode != 0:
        detail = (done.stderr or b"").decode("utf-8", "replace").strip()
        return False, detail or "pg_dump exited with code {}".format(done.returncode)
    return True, None


@login_required
@require_POST
@api_role_required(*ADMIN_ROLES)
def backup(request):
    data = _payload(request)
    if data.get("action") == "run":
        # A backup is only recorded as completed when a dump file was actually
        # written. The previous version created a "completed" row with a
        # synthetic auto-<timestamp> reference and no file behind it.
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        target = _backup_dir() / f"tass-{stamp}.dump"
        ok, detail = _run_pg_dump(target)
        if ok and target.exists() and target.stat().st_size > 0:
            record = BackupRecord.objects.create(
                backup_type=BackupRecord.TYPE_MANUAL,
                status=BackupRecord.STATUS_COMPLETED,
                initiated_by=request.user,
                file_reference=target.name,
            )
            _log(request.user, "manual_backup_completed", entity_type="BackupRecord", entity_id=record.pk)
            return _ok(
                backup_id=record.pk,
                file=target.name,
                bytes=target.stat().st_size,
                history=_backup_history(),
            )
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        failed = BackupRecord.objects.create(
            backup_type=BackupRecord.TYPE_MANUAL,
            status=BackupRecord.STATUS_FAILED,
            initiated_by=request.user,
            file_reference=detail or "dump failed",
        )
        _log(request.user, "manual_backup_failed", entity_type="BackupRecord", entity_id=failed.pk)
        return _err("Backup failed: {}".format(detail or "no dump produced"), 500)
    if data.get("action") == "restore":
        record = BackupRecord.objects.filter(status=BackupRecord.STATUS_COMPLETED).order_by("-created_at").first()
        if record:
            record.status = BackupRecord.STATUS_RESTORED
            record.restored_at = timezone.now()
            record.save()
        return _ok(restored=bool(record))
    if data.get("action") == "schedule" and data.get("frequency"):
        cfg = SystemConfiguration.get_solo()
        settings = dict(cfg.backup_settings or {})
        settings.update({
            "frequency": data["frequency"],
            "retention": data.get("retention") or settings.get("retention", 30),
            "auto": bool(data.get("auto", True)),
        })
        cfg.backup_settings = settings
        cfg.save()
        return _ok(changed=True)
    return _ok(changed=False)


# ---------------------------------------------------------------------------
# POST /api/role-access-review/ — annual role/access review snapshot
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_perm_required("conduct_annual_role_access_review")
def role_access_review(request):
    year = _active_year()
    data = _payload(request)
    if not year:
        return _err("No active academic year configured yet.", 409)
    roles = data.get("roles") or []
    snapshot = {}
    for item in roles:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            snapshot[str(item[0])] = item[1]
        elif isinstance(item, dict):
            snapshot[str(item.get("role") or item.get("name") or "")] = item.get("count", 0)
    users = data.get("users")
    review, _ = RoleAccessReview.objects.get_or_create(
        academic_year=year, defaults={"conducted_by": request.user}
    )
    review.conducted_by = request.user
    if users is not None:
        review.role_snapshot = snapshot or review.role_snapshot
    review.completed_at = timezone.now()
    review.save()
    _log(request.user, "annual_role_access_review_completed", entity_type="RoleAccessReview", entity_id=review.pk)
    return _ok(review_id=review.pk)


# ---------------------------------------------------------------------------
# POST /api/role-permissions/ — grant or revoke one permission on a role Group
#
# This is the write path behind the editable permission matrix on the System
# Administrator dashboard. Each checkbox toggle posts {role, codename, grant}
# and the matching Grant/Revoke lands on the role's Django Group immediately,
# which is exactly what the view guards and (via perms) the dashboards read.
# ---------------------------------------------------------------------------
@login_required
@require_POST
@api_role_required(*ADMIN_ROLES)
def role_permissions(request):
    from django.contrib.auth.models import Group

    from apps.accounts import rbac

    data = _payload(request)
    role_code = str(data.get("role") or "").strip()
    codename = str(data.get("codename") or "").strip()
    grant = bool(data.get("grant"))

    valid_codes = set(Role.Code.values)
    if role_code not in valid_codes:
        return _err("Unknown role: '{}'.".format(role_code), 400)
    if role_code == Role.Code.SYSTEM_ADMIN:
        return _err("System Administrator permissions are fixed and cannot be edited here.", 400)

    permission = rbac.permission_for(codename)
    if permission is None:
        return _err("Unknown permission: '{}'.".format(codename), 400)

    group, _ = Group.objects.get_or_create(name=role_code)
    if grant:
        if not group.permissions.filter(pk=permission.pk).exists():
            group.permissions.add(permission)
    else:
        group.permissions.remove(permission)

    _log(
        request.user,
        "role_permission_{}:" .format("grant" if grant else "revoke") + codename,
        entity_type="Group",
        entity_id=group.pk,
    )
    return _ok(role=role_code, codename=codename, grant=grant)


# ---------------------------------------------------------------------------
# /api/messages/ — one-on-one (private) and group chat for the dashboards
#
# GET  /api/messages/              -> threads (group + private) and contacts
# GET  /api/messages/?peer=ID      -> the private thread with that peer
# GET  /api/messages/?thread=ID    -> the group thread for that classroom
# POST /api/messages/ {recipient_id, body} -> send a private message
# POST /api/messages/ {classroom_id, body} -> send a group message
# ---------------------------------------------------------------------------
def _msg_display(user):
    if hasattr(user, "employee_profile"):
        p = user.employee_profile
        return f"{p.first_name} {p.last_name}".strip() or user.email
    if hasattr(user, "student_profile"):
        p = user.student_profile
        return f"{p.first_name} {p.last_name}".strip() or user.email
    return user.email


def _msg_initials(display):
    parts = display.split()
    return "".join(part[0] for part in parts[:2] if part[0].isalpha() or part[0].isdigit()).upper()[:2] or "?"


def _msg_gradient(seed):
    palette = [
        "linear-gradient(135deg,#1e5faf,#3a86d4)",
        "linear-gradient(135deg,#e11d48,#f43f5e)",
        "linear-gradient(135deg,#7c3aed,#a78bfa)",
        "linear-gradient(135deg,#16a34a,#4ade80)",
        "linear-gradient(135deg,#d97706,#fbbf24)",
        "linear-gradient(135deg,#0891b2,#22d3ee)",
        "linear-gradient(135deg,#dc2626,#f87171)",
        "linear-gradient(135deg,#4f46e5,#818cf8)",
    ]
    return palette[hash(str(seed)) % len(palette)]


def _coerce_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _msg_payload(msg, current_user):
    sender = msg.sender
    display = _msg_display(sender)
    return {
        "id": msg.pk,
        "sent": sender.pk == current_user.pk,
        "sender": display,
        "sender_id": sender.pk,
        "text": msg.content,
        "time": msg.sent_at.strftime("%Y-%m-%d %H:%M"),
        "iso": msg.sent_at.isoformat(),
    }


def _msg_ws_payload(msg, kind, thread, peer_id=None, classroom_id=None):
    sender = msg.sender
    return {
        "type": "chat",
        "id": msg.pk,
        "kind": kind,
        "thread": thread,
        "peer_id": peer_id,
        "classroom_id": classroom_id,
        "sender_id": sender.pk,
        "sender": _msg_display(sender),
        "text": msg.content,
        "time": msg.sent_at.strftime("%Y-%m-%d %H:%M"),
    }


def _broadcast(rooms, payload):
    """Push a realtime payload to chat rooms, ignoring channel-layer outages."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync

        layer = get_channel_layer()
        if layer is None:
            return
        for room in rooms:
            async_to_sync(layer.group_send)(room, {"type": "chat.message", "payload": payload})
    except Exception:
        pass


def _shares_classroom(user, peer):
    from apps.communication.access import can_message
    if can_message(user, peer):
        return True
    # Try to be permissive - check if they share any classroom relationship
    try:
        from apps.communication.views import _classrooms_for_user
        from apps.virtual_classrooms.models import VirtualClassroomEnrollment
        uc = _classrooms_for_user(user)
        pc = _classrooms_for_user(peer)
        if uc.filter(pk__in=pc).exists():
            return True
        # teacher-student
        if hasattr(peer, 'student_profile'):
            if uc.filter(enrollments__student=peer.student_profile, enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE).exists():
                return True
        if hasattr(user, 'student_profile'):
            if uc.filter(teacher=peer).exists() or pc.filter(teacher=user).exists():
                return True
    except Exception:
        pass
    # If they both have send_private_message permission, allow
    try:
        from apps.accounts.decorators import has_permission
        if has_permission(user, 'send_private_message') and has_permission(peer, 'send_private_message'):
            return True
    except Exception:
        pass
    return False


@login_required
def messages(request):
    from apps.communication.views import _classrooms_for_user

    user = request.user

    if request.method == "GET":
        peer_id = request.GET.get("peer")
        group_id = request.GET.get("thread")

        if peer_id:
            # Match the picker in messageable_contacts: a peer is any learner
            # enrolled in a class the caller may reach, regardless of account
            # status (only archived accounts are rejected).
            peer = (
                User.objects.filter(pk=_coerce_int(peer_id))
                .exclude(status=User.Status.ARCHIVED)
                .first()
            )
            if peer is None or peer.pk == user.pk:
                return _err("Recipient not found.", 404)
            if not _shares_classroom(user, peer):
                return _err("You do not share a class with this user.", 403)
            PrivateMessage.objects.filter(
                sender=peer, recipient=user, read_at__isnull=True
            ).update(read_at=timezone.now())
            qs = (
                PrivateMessage.objects.filter(Q(sender=user, recipient=peer) | Q(sender=peer, recipient=user))
                .select_related("sender", "recipient")
                .order_by("sent_at")
            )
            return _ok(
                peer={"id": peer.pk, "name": _msg_display(peer)},
                messages=[_msg_payload(m, user) for m in qs],
            )

        if group_id:
            classroom = VirtualClassroom.objects.filter(pk=_coerce_int(group_id)).first()
            if classroom is None or classroom not in _classrooms_for_user(user):
                return _err("You do not access this class chat.", 403)
            qs = (
                GroupMessage.objects.filter(virtual_classroom=classroom)
                .select_related("sender")
                .order_by("sent_at")
            )
            return _ok(
                classroom={"id": classroom.pk, "name": f"{classroom.subject.name} - {classroom.section.name}"},
                messages=[_msg_payload(m, user) for m in qs],
            )

        classrooms = list(_classrooms_for_user(user))

        group_map = {}
        for m in (
            GroupMessage.objects.filter(virtual_classroom__in=classrooms)
            .select_related("virtual_classroom", "sender", "virtual_classroom__subject", "virtual_classroom__section")
        ):
            entry = group_map.get(m.virtual_classroom_id)
            if entry is None or m.sent_at > entry.sent_at:
                group_map[m.virtual_classroom_id] = m

        group_threads = []
        for vc in classrooms:
            last = group_map.get(vc.pk)
            name = f"{vc.subject.name} - {vc.section.name}"
            group_threads.append({
                "id": f"c-{vc.pk}",
                "classroom_id": vc.pk,
                "name": name,
                "sub": (f"{_msg_display(last.sender)}: {last.content}" if last else "Class group chat"),
                "unread": 0,
                "ts": last.sent_at.isoformat() if last else "",
            })
        # Most-recent-activity first (messenger order); threads with no
        # messages yet keep the old alphabetical fallback at the bottom.
        active = sorted((t for t in group_threads if t["ts"]), key=lambda t: t["ts"], reverse=True)
        idle = sorted((t for t in group_threads if not t["ts"]), key=lambda t: t["name"])
        group_threads = active + idle

        peer_ids = set(
            PrivateMessage.objects.filter(sender=user).values_list("recipient_id", flat=True)
        ) | set(
            PrivateMessage.objects.filter(recipient=user).values_list("sender_id", flat=True)
        )

        private_threads = []
        for peer in User.objects.filter(pk__in=peer_ids).order_by("email"):
            unread = PrivateMessage.objects.filter(
                sender=peer, recipient=user, read_at__isnull=True
            ).count()
            last = (
                PrivateMessage.objects.filter(Q(sender=user, recipient=peer) | Q(sender=peer, recipient=user))
                .order_by("-sent_at")
                .first()
            )
            display = _msg_display(peer)
            private_threads.append({
                "id": f"p-{peer.pk}",
                "peer_id": peer.pk,
                "name": display,
                "sub": (f"{_msg_display(last.sender)}: {last.content}" if last else "No messages yet"),
                "unread": unread,
                "time": last.sent_at.strftime("%Y-%m-%d %H:%M") if last else "",
                "ts": last.sent_at.isoformat() if last else "",
            })
        active = sorted((t for t in private_threads if t["ts"]), key=lambda t: t["ts"], reverse=True)
        idle = sorted((t for t in private_threads if not t["ts"]), key=lambda t: t["name"])
        private_threads = active + idle

        # `contacts` is a flat, de-duplicated picker list. `by_classroom` keeps
        # the same people grouped per virtual classroom so a dashboard opened
        # on one classroom can offer only that classroom's counterpart (a
        # student sees that class's teacher, a teacher sees that class's
        # students). Both are needed: de-duplication is what the global picker
        # wants, grouping is what per-class scoping requires.
        #
        # The rule (and the directional buckets — a student only ever gets that
        # class's teacher, a teacher only ever gets that class's students, staff
        # keep both sides) lives in apps.communication.access so this endpoint
        # and the /communication/messages/ page cannot drift apart, and so the
        # list can never offer someone can_message() would then reject.
        from apps.communication.access import messageable_contacts

        contacts, by_classroom = messageable_contacts(user)

        return _ok(
            group=group_threads,
            private=private_threads,
            contacts=contacts,
            contacts_by_classroom=by_classroom,
            school_name=getattr(settings, "SCHOOL_NAME", "San Vicente Institute"),
        )

    # POST — send message
    data = _payload(request)
    body = (data.get("body") or "").strip()
    if not body:
        return _err("Message text is required.")

    if data.get("recipient_id"):
        peer = (
            User.objects.filter(pk=_coerce_int(data["recipient_id"]))
            .exclude(status=User.Status.ARCHIVED)
            .first()
        )
        if peer is None or peer.pk == user.pk:
            return _err("Recipient not found.", 404)
        if not _shares_classroom(user, peer):
            return _err("You do not share a class with this user.", 403)
        msg = PrivateMessage.objects.create(sender=user, recipient=peer, content=body)
        notif_service.notify_private_message(msg)
        _log(user, "private_message_sent", entity_type="PrivateMessage", entity_id=msg.pk)
        _broadcast(
            [f"user-{peer.pk}", f"user-{user.pk}"],
            _msg_ws_payload(msg, kind="dm", thread=f"p-{peer.pk}", peer_id=peer.pk),
        )
        return _ok(message=_msg_payload(msg, user))

    if data.get("classroom_id"):
        classroom = VirtualClassroom.objects.filter(pk=_coerce_int(data["classroom_id"])).first()
        if classroom is None or classroom not in _classrooms_for_user(user):
            return _err("You do not have access to this class chat.", 403)
        msg = GroupMessage.objects.create(sender=user, virtual_classroom=classroom, content=body)
        notif_service.notify_group_message(msg)
        _log(user, "group_message_sent", entity_type="GroupMessage", entity_id=msg.pk)
        _broadcast(
            [f"class-{classroom.pk}"],
            _msg_ws_payload(msg, kind="group", thread=f"c-{classroom.pk}", classroom_id=classroom.pk),
        )
        return _ok(message=_msg_payload(msg, user))

    return _err("Missing recipient or classroom.")


# ── SCHOOL ACTIVITY CALENDAR ────────────────────────────────────────────────
# Every signed-in role may read the calendar; the System Administrator, the
# Principals and the Registrar may create, edit or delete an activity. Reading
# is deliberately open to all roles because the calendar holds school-wide
# information.
CALENDAR_WRITE_ROLES = ("system_admin", "principal", "elementary_principal", "registrar")


def _can_edit_calendar(user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name__in=CALENDAR_WRITE_ROLES).exists()


def _ve_messages(exc):
    """Flatten a Django ValidationError into one readable line.

    ``message_dict`` maps a field name to a *list* of messages, so joining its
    values directly raises TypeError.
    """
    if hasattr(exc, "message_dict"):
        parts = []
        for field, messages in exc.message_dict.items():
            for msg in messages:
                parts.append("{}: {}".format(field, msg) if field else str(msg))
        return "; ".join(parts)
    return "; ".join(str(m) for m in exc.messages)


def _parse_iso_date(value, field):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValidationError({field: "Expected a YYYY-MM-DD date, got {!r}.".format(value)})


def _parse_iso_time(value, field):
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValidationError({field: "Expected a HH:MM time, got {!r}.".format(value)})


def _event_json(ev):
    return {
        "id": ev.id,
        "title": ev.title,
        "description": ev.description or "",
        "category": ev.category,
        "categoryLabel": ev.get_category_display(),
        "startDate": ev.start_date.isoformat(),
        # A single-day activity reports no endDate so the client can draw it on
        # one cell instead of stretching it across a range.
        "endDate": ev.end_date.isoformat() if ev.spans_multiple_days else None,
        "allDay": ev.all_day,
        "startTime": ev.start_time.strftime("%H:%M") if ev.start_time else None,
        "endTime": ev.end_time.strftime("%H:%M") if ev.end_time else None,
        "location": ev.location or "",
        "status": ev.status,
        "statusLabel": ev.get_status_display(),
        "audience": getattr(ev, "audience", "all"),
        "audienceLabel": ev.get_audience_display() if hasattr(ev, "get_audience_display") else "Everyone",
        "academicYear": ev.academic_year.label if ev.academic_year else None,
        "createdBy": (ev.created_by.get_full_name() if ev.created_by else None),
        "updatedBy": (ev.updated_by.get_full_name() if ev.updated_by else None),
        "updatedAt": timezone.localtime(ev.updated_at).isoformat() if ev.updated_at else None,
    }


def _apply_event_fields(ev, data, user):
    """Copy an inbound payload onto a SchoolEvent, validating as we go."""
    old_start, old_end = ev.start_date, ev.end_date
    if "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            raise ValidationError({"title": "A title is required."})
        ev.title = title[:200]
    if "description" in data:
        ev.description = str(data.get("description") or "")
    if "category" in data:
        category = str(data.get("category") or "event")
        if category not in {key for key, _ in SchoolEvent.CATEGORY_CHOICES}:
            raise ValidationError({"category": "Unknown category {!r}.".format(category)})
        ev.category = category
    if "startDate" in data or "start_date" in data:
        start = _parse_iso_date(data.get("startDate", data.get("start_date")), "startDate")
        if not start:
            raise ValidationError({"startDate": "A start date is required."})
        ev.start_date = start
    if "endDate" in data or "end_date" in data:
        ev.end_date = _parse_iso_date(data.get("endDate", data.get("end_date")), "endDate")
    if "allDay" in data or "all_day" in data:
        value = data.get("allDay", data.get("all_day"))
        ev.all_day = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if "startTime" in data or "start_time" in data:
        ev.start_time = _parse_iso_time(data.get("startTime", data.get("start_time")), "startTime")
    if "endTime" in data or "end_time" in data:
        ev.end_time = _parse_iso_time(data.get("endTime", data.get("end_time")), "endTime")
    if "location" in data:
        ev.location = str(data.get("location") or "")[:200]
    if "status" in data:
        status = str(data.get("status") or SchoolEvent.STATUS_PUBLISHED)
        if status not in {key for key, _ in SchoolEvent.STATUS_CHOICES}:
            raise ValidationError({"status": "Unknown status {!r}.".format(status)})
        ev.status = status
    if "audience" in data:
        audience = str(data.get("audience") or SchoolEvent.AUDIENCE_ALL)
        if audience not in {key for key, _ in SchoolEvent.AUDIENCE_CHOICES}:
            raise ValidationError({"audience": "Unknown audience {!r}.".format(audience)})
        ev.audience = audience
    # A one-day activity stores its end date equal to its start date, so when
    # the start moves and the client sends no new end date, carry the end date
    # across by the same offset. Without this, moving a single-day activity
    # would leave end_date behind and fail validation as end < start.
    if "endDate" not in data and "end_date" not in data and ev.end_date is not None:
        if old_start and ev.start_date != old_start:
            shift = (ev.start_date - old_start).days
            ev.end_date = (old_end or old_start) + timedelta(days=shift)
    # Clearing the times is what turns a timed activity back into an all-day one.
    if ev.all_day:
        ev.start_time = None
        ev.end_time = None
    # A one-day activity stores its end date equal to its start date so the
    # overlap query below stays a simple two-sided range check.
    if ev.end_date is None:
        ev.end_date = ev.start_date
    ev.updated_by = user
    return ev


@login_required
def calendar(request, event_id=None):
    """GET lists activities in a date range; POST/PUT/DELETE manage them.

    GET is open to every signed-in role. The three write verbs are limited to
    the System Administrator, the Principals and the Registrar.
    """
    if request.method == "GET":
        params = request.GET
        try:
            start = _parse_iso_date(params.get("start"), "start")
            end = _parse_iso_date(params.get("end"), "end")
        except ValidationError as exc:
            return _err(_ve_messages(exc), 400)
        if start is None and end is None:
            start = timezone.localdate().replace(day=1)
        if start is None:
            start = end.replace(day=1)
        if end is None:
            # Six weeks covers the widest month grid (a 31-day month starting on
            # a Saturday needs 36 cells) without a second request.
            end = start + timedelta(days=41)
            end = end.replace(day=1) - timedelta(days=1) if end.day != 1 else end

        # An activity overlaps the window when it starts inside it, or when it
        # began earlier and is still running, so multi-day spans are included.
        qs = SchoolEvent.objects.filter(
            Q(start_date__lte=end) & (Q(end_date__isnull=True) | Q(end_date__gte=start))
        ).select_related("academic_year", "created_by", "updated_by")
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("category"):
            qs = qs.filter(category=params["category"])
        user = request.user
        user_groups = set(user.groups.values_list("name", flat=True))
        is_student = "student" in user_groups and not user.is_superuser and not user_groups & {"teacher"}
        is_teacher = "teacher" in user_groups and not user.is_superuser
        if is_student and not (user.is_superuser or user_groups & {"principal", "elementary_principal", "system_admin", "registrar"}):
            qs = qs.exclude(audience=SchoolEvent.AUDIENCE_TEACHERS)
        elif is_teacher and not (user.is_superuser or user_groups & {"principal", "elementary_principal", "system_admin", "registrar"}):
            qs = qs.exclude(audience=SchoolEvent.AUDIENCE_STUDENTS)
        events = [_event_json(ev) for ev in qs.order_by("start_date", "title")]
        return _ok(events=events, canEdit=_can_edit_calendar(request.user),
                   start=start.isoformat(), end=end.isoformat())

    if not _can_edit_calendar(request.user):
        return _err("Only the System Administrator, a Principal, or the Registrar can change school activities.", 403)

    data = _payload(request)

    if request.method == "POST":
        ev = SchoolEvent(start_date=timezone.localdate())
        try:
            _apply_event_fields(ev, data, request.user)
            ev.created_by = request.user
            ev.full_clean(exclude=["academic_year"])
        except ValidationError as exc:
            return _err(_ve_messages(exc), 400)
        ev.save()
        _log(request.user, "school_event_created", entity_type="SchoolEvent", entity_id=ev.pk)
        return _ok(event=_event_json(ev))

    existing = SchoolEvent.objects.filter(pk=event_id).first()
    if existing is None:
        return _err("That activity no longer exists.", 404)

    if request.method in ("PUT", "PATCH"):
        try:
            _apply_event_fields(existing, data, request.user)
            existing.full_clean(exclude=["academic_year"])
        except ValidationError as exc:
            return _err(_ve_messages(exc), 400)
        existing.save()
        _log(request.user, "school_event_updated", entity_type="SchoolEvent", entity_id=existing.pk)
        return _ok(event=_event_json(existing))

    if request.method == "DELETE":
        pk = existing.pk
        existing.delete()
        _log(request.user, "school_event_deleted", entity_type="SchoolEvent", entity_id=pk)
        return _ok(deleted=pk)

    return _err("Method not allowed.", 405)


# ---------------------------------------------------------------------------
# GET /api/teacher-dashboard/ and /api/student-dashboard/ — polling snapshots
# ---------------------------------------------------------------------------
# Auto-refresh polling foundation: the same dashboard_json that is embedded in
# each template is exposed as a light JSON endpoint so an open dashboard can
# re-fetch it in the background and repaint whatever changed, without a page
# reload. Guarding matches the dashboard view: superuser or the named group.
@login_required
@require_GET
def teacher_dashboard_snapshot(request):
    if not (
        request.user.is_superuser
        or "teacher" in role_codes_for(request.user)
    ):
        return _err("You do not have access to this dashboard.", 403)
    from apps.dashboards.context import teacher_context

    try:
        data = teacher_context(request.user)["dashboard_json"]
    except Exception:
        return _err("Could not build the dashboard.", 500)
    return _ok(**data)


@login_required
@require_GET
def student_dashboard_snapshot(request):
    if not (
        request.user.is_superuser
        or "student" in role_codes_for(request.user)
        or getattr(request.user, "student_profile", None) is not None
    ):
        return _err("You do not have access to this dashboard.", 403)
    from apps.dashboards.context import student_context

    try:
        data = student_context(request.user)["dashboard_json"]
    except Exception:
        return _err("Could not build the dashboard.", 500)
    return _ok(**data)

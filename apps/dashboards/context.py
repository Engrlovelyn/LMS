import datetime
import json
import shutil
import time
from collections import defaultdict
from django.contrib.auth.models import Group
from django.utils import timezone
from datetime import timedelta
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Avg, Count, Q, Sum
from django.db.utils import DatabaseError, ProgrammingError
from django.urls import reverse

from apps.accounts import campus
from apps.accounts.ordering import order_students, order_teachers
from apps.accounts.account_admin import (
    locked_accounts_queryset,
    serialize_locked_account,
)
from apps.accounts.context_processors import avatar_url
from apps.accounts.decorators import has_permission
from apps.accounts.models import (
    ActivityLog,
    EmployeeProfile,
    LoginSession,
    Role,
    StudentProfile,
    User,
)
from apps.academic_structure.models import AcademicYear, Section, StudentEnrollment, Subject
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment
from apps.teaching_learning.models import LearningResource, LearningResourcePlacement, Topic
from apps.assessments.models import (
    Assessment,
    AssessmentAssignment,
    AssessmentPart,
    AssessmentProctoringEvent,
    StudentGrade,
    Submission,
)
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.communication.models import ClassAnnouncement, SchoolAnnouncement, SchoolEvent
from apps.governance.models import RoleAccessReview
from apps.system_settings.models import BackupRecord, SystemConfiguration
from apps.analytics.models import GeneratedReport, SFReportTemplate

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

PRESENT_STATUSES = ("present_on_time", "present_late")

SD_BUCKETS = [
    (0, 59, "0-59"),
    (60, 69, "60-69"),
    (70, 79, "70-79"),
    (80, 89, "80-89"),
    (90, 100, "90-100"),
]


def _non_archived_users():
    return User.objects.exclude(status="archived")


def _role_user_count(role_code):
    return User.objects.filter(groups__name=role_code).count()


# Human labels for the permission-matrix rows. The grant itself is read from
# each role Group's real Django permissions, so the matrix cannot drift from
# what the system actually enforces.
_PERMISSION_ROWS = [
    ("Log in to the system", "accounts.log_in_user"),
    ("Reach your role dashboard", "accounts.view_role_based_dashboard"),
    ("Create employee accounts", "accounts.create_employee_account"),
    ("View employee accounts", "accounts.view_employee_account"),
    ("Edit employee accounts", "accounts.update_employee_account"),
    ("Archive accounts", "accounts.archive_account"),
    ("Reset a user password", "accounts.reset_account_password"),
    ("Create student accounts", "accounts.create_student_profile_account"),
    ("View student records", "accounts.view_student_record"),
    ("Edit student records", "accounts.update_student_record"),
    ("Archive student records", "accounts.archive_student_record"),
    ("Bulk import student records", "accounts.bulk_import_student_records"),
    ("Role-based access control", "accounts.role_based_access_control"),
    ("Manage active sessions", "accounts.manage_active_sessions"),
    ("View academic year", "academic_structure.view_academic_year"),
    ("Create / edit / archive sections", "academic_structure.update_section_custom"),
    ("Create / edit / archive subjects", "academic_structure.update_subject_custom"),
    ("Promote a student", "academic_structure.promote_student"),
    ("Rollover academic year", "academic_structure.rollover_academic_year"),
    ("Manage academic structure template", "academic_structure.manage_academic_structure_template"),
    ("Validate section-subject coverage", "academic_structure.validate_section_subject_coverage"),
    ("Create virtual classrooms", "virtual_classrooms.create_virtual_classroom"),
    ("View virtual classrooms", "virtual_classrooms.view_virtual_classroom"),
    ("Enroll students to a classroom", "virtual_classrooms.enroll_students_to_vc"),
    ("Archive a virtual classroom", "virtual_classrooms.archive_virtual_classroom"),
    ("Create assessments", "assessments.create_assessment_custom"),
    ("View assessments", "assessments.view_assessment_custom"),
    ("Edit assessments", "assessments.update_assessment_custom"),
    ("Access assessment repository", "assessments.access_assessment_repository"),
    ("Monitor student submissions", "assessments.monitor_student_submissions"),
    ("Manually grade subjective work", "assessments.manually_grade_subjective_assessment"),
    ("Auto-grade objective work", "assessments.auto_grade_objective_assessment"),
    ("Compute student grades", "assessments.compute_student_grade"),
    ("View the grade book", "assessments.view_grade_book"),
    ("View student performance", "assessments.view_student_performance_tracking"),
    ("Complete an assessment", "assessments.complete_assessment_custom"),
    ("View attendance records", "attendance.view_attendance_records"),
    ("Record class attendance", "attendance.record_class_attendance"),
    ("Update an attendance record", "attendance.update_attendance_record_custom"),
    ("Generate attendance report", "attendance.generate_attendance_report"),
    ("Teaching-learning repository", "teaching_learning.access_subject_level_repository"),
    ("Upload learning resources", "teaching_learning.upload_learning_resource"),
    ("Manage learning resources", "teaching_learning.manage_learning_resources"),
    ("Download learning resources", "teaching_learning.view_download_learning_resource"),
    ("Add class topics", "teaching_learning.add_topic_custom"),
    ("Post school-wide announcements", "communication.post_school_wide_announcement"),
    ("Update school announcements", "communication.update_school_wide_announcement"),
    ("Archive school announcements", "communication.archive_school_wide_announcement"),
    ("Post class announcements", "communication.post_class_announcement"),
    ("Update class announcements", "communication.update_class_announcement"),
    ("Archive class announcements", "communication.archive_class_announcement"),
    ("View announcements", "communication.view_announcements_custom"),
    ("Manage school events", "communication.add_school_event"),
    ("Send group messages", "communication.send_group_messaging"),
    ("Send private messages", "communication.send_private_message"),
    ("School-wide analytics", "analytics.view_school_wide_analytics"),
    ("Class-level analytics", "analytics.view_class_level_analytics"),
    ("Attendance trend analytics", "analytics.generate_attendance_trend_analytics"),
    ("Submission assessment reports", "analytics.generate_submission_assessment_reports"),
    ("Manage SF report templates", "analytics.manage_sf_report_templates"),
    ("Generate SF reports", "analytics.generate_sf_reports"),
    ("Monitor student activity logs", "analytics.monitor_student_activity_logs"),
    ("Conduct annual access review", "governance.conduct_annual_role_access_review"),
    ("Retrieve access review records", "governance.retrieve_role_access_review_records"),
    ("Configure system settings", "system_settings.configure_system_settings"),
    ("Manage password policy", "system_settings.management_password_policy"),
    ("Backup / restore database", "system_settings.backup_restore_database"),
]


def _role_matrix_payload():
    """Build the permission matrix from the role Groups' live permissions.

    The dashboard used to render a hardcoded JavaScript array labelled
    "read-only - contact system developer to modify roles". That array was
    documentation, not data: it did not reflect a single real grant. This
    payload is derived from ``Group.permissions``, so every tick shown is a
    permission the request-checking code genuinely honours.
    """
    from apps.accounts.models import Role

    # Group.permissions yields Permission rows whose ``codename`` is bare
    # ("view_attendance_records") while the ContentType carries the app label.
    # _PERMISSION_ROWS is written in Django's qualified form
    # ("attendance.view_attendance_records"), so the sets are keyed by
    # "app_label.codename" -- comparing the two directly made every tick false.
    granted = {}
    for code, _label in Role.Code.choices:
        group = Group.objects.filter(name=code).first()
        granted[code] = {
            "{}.{}".format(p.content_type.app_label, p.codename)
            for p in (group.permissions.all() if group else [])
        }

    return {
        "roles": [{"code": c, "label": l} for c, l in Role.Code.choices],
        "rows": [
            {
                "label": label,
                "codename": codename,
                "grants": {
                    code: (codename in granted.get(code, set()))
                    for code, _l in Role.Code.choices
                },
            }
            for label, codename in _PERMISSION_ROWS
        ],
    }


def _active_academic_year():
    return (
        AcademicYear.objects.filter(status="ACTIVE")
        .order_by("-start_date")
        .first()
    )


def _stat(label, value, icon):
    return {"label": label, "value": value, "icon": icon}


# ── System Administrator: measured health, real services, real alerts ──────
# Nothing in this block may assert a value it cannot observe. CPU and memory
# load are intentionally absent: they need a host agent (psutil), and the panel
# renders an em dash rather than a made-up percentage.

def _human_bytes(num):
    try:
        num = float(num)
    except (TypeError, ValueError):
        return None
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return "{:.0f} {}".format(num, unit) if unit == "B" else "{:.1f} {}".format(num, unit)
        num /= 1024.0
    return "{:.1f} PB".format(num)


def _measured_health():
    """Readings taken from the live system. A missing reading stays missing."""
    health = {}

    started = time.perf_counter()
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        health["response"] = round((time.perf_counter() - started) * 1000, 1)
    except Exception:
        health["response"] = None

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            health["dbSize"] = _human_bytes(cur.fetchone()[0])
    except Exception:
        health["dbSize"] = None

    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT date_trunc('second', now() - pg_postmaster_start_time())"
            )
            delta = cur.fetchone()[0]
        total = int(delta.total_seconds())
        days, rem = divmod(total, 86400)
        hours = rem // 3600
        health["uptime"] = "{}d {}h".format(days, hours) if days else "{}h".format(hours)
    except Exception:
        health["uptime"] = None

    try:
        usage = shutil.disk_usage(str(settings.BASE_DIR))
        health["disk"] = round(usage.used / usage.total * 100, 1) if usage.total else None
        health["diskSub"] = "{} free of {}".format(
            _human_bytes(usage.free), _human_bytes(usage.total)
        )
    except Exception:
        health["disk"] = None
        health["diskSub"] = None

    last_backup = (
        BackupRecord.objects.filter(status=BackupRecord.STATUS_COMPLETED)
        .order_by("-created_at")
        .first()
    )
    if last_backup:
        age = timezone.now() - timezone.localtime(last_backup.created_at)
        seconds = int(age.total_seconds())
        if seconds < 60:
            health["backup"] = "Just now"
        elif seconds < 3600:
            health["backup"] = "{} min ago".format(max(seconds // 60, 1))
        elif age.days:
            health["backup"] = "{}d ago".format(age.days)
        else:
            health["backup"] = "{}h ago".format(seconds // 3600)
    else:
        health["backup"] = "No backups yet"
    return health


def _service_checks():
    """Only services this process can actually verify are reported."""
    checks = []

    started = time.perf_counter()
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks.append({
            "s": "PostgreSQL",
            "st": "Operational",
            "d": "Query round-trip {} ms".format(round((time.perf_counter() - started) * 1000, 1)),
        })
    except Exception:
        checks.append({"s": "PostgreSQL", "st": "Down", "d": "Connection failed"})

    try:
        cache.set("sa_health_probe", "1", 5)
        ok = cache.get("sa_health_probe") == "1"
        checks.append({
            "s": "Cache",
            "st": "Operational" if ok else "Degraded",
            "d": "{} backend".format(settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1]),
        })
    except Exception:
        checks.append({"s": "Cache", "st": "Degraded", "d": "Probe failed"})

    mail_backend = getattr(settings, "EMAIL_BACKEND", "") or ""
    if "console" in mail_backend.lower() or "locmem" in mail_backend.lower():
        checks.append({
            "s": "Email",
            "st": "Degraded",
            "d": "Console backend — messages are not delivered",
        })
    else:
        checks.append({"s": "Email", "st": "Operational", "d": mail_backend.rsplit(".", 1)[-1]})

    try:
        usage = shutil.disk_usage(str(settings.MEDIA_ROOT))
        pct = round(usage.used / usage.total * 100, 1) if usage.total else 0
        checks.append({
            "s": "Media storage",
            "st": "Operational" if pct < 90 else "Degraded",
            "d": "{} used".format(_human_bytes(usage.used)),
        })
    except Exception:
        checks.append({"s": "Media storage", "st": "Unknown", "d": "MEDIA_ROOT not readable"})

    layer = getattr(settings, "CHANNEL_LAYERS", {}).get("default", {}).get("BACKEND", "")
    if layer:
        checks.append({
            "s": "Realtime channel layer",
            "st": "Operational",
            "d": layer.rsplit(".", 1)[-1],
        })
    return checks


def _system_alerts(cfg, pending_count, locked_count, unassigned_count):
    """Alerts derived from rows that actually need attention."""
    alerts = []

    if getattr(cfg, "maintenance_mode", False):
        alerts.append({
            "level": "warning", "icon": "fa-screwdriver-wrench",
            "t": "Maintenance mode is enabled",
            "d": "Users may be unable to work normally.",
        })

    if pending_count:
        alerts.append({
            "level": "warning", "icon": "fa-envelope",
            "t": "{} account(s) awaiting verification".format(pending_count),
            "d": "Pending accounts cannot sign in until verified.",
        })

    if locked_count:
        alerts.append({
            "level": "danger", "icon": "fa-lock",
            "t": "{} account(s) locked".format(locked_count),
            "d": "Locked by the lockout policy or by an administrator.",
        })

    if unassigned_count:
        alerts.append({
            "level": "warning", "icon": "fa-user-slash",
            "t": "{} account(s) without a role".format(unassigned_count),
            "d": "No active role assignment for the current year.",
        })

    stale_reviews = RoleAccessReview.objects.filter(completed_at__isnull=True).count()
    if stale_reviews:
        alerts.append({
            "level": "info", "icon": "fa-clipboard-check",
            "t": "{} access review(s) not completed".format(stale_reviews),
            "d": "Annual role and access review is still open.",
        })

    if not BackupRecord.objects.filter(status=BackupRecord.STATUS_COMPLETED).exists():
        alerts.append({
            "level": "danger", "icon": "fa-database",
            "t": "No successful backup on record",
            "d": "There is no recovery point for this system.",
        })
    return alerts


def _calendar_payload(user, today=None):
    """Current-month school activities plus whether this user may edit them.

    Every dashboard embeds the same month so the calendar opens populated on
    first paint; later months are fetched from /api/calendar/. Reads are open
    to all roles, writes are limited to the System Administrator, the
    Principals and the Registrar.
    """
    today = today or timezone.localdate()
    first = today.replace(day=1)
    # A month grid can need 42 cells; six weeks covers the widest case.
    last = (first + timedelta(days=41)).replace(day=1) - timedelta(days=1)
    rows = SchoolEvent.objects.filter(
        Q(start_date__lte=last) & (Q(end_date__isnull=True) | Q(end_date__gte=first))
    ).select_related("created_by", "updated_by")

    if user and user.is_authenticated:
        user_groups = set(user.groups.values_list("name", flat=True))
        is_student = "student" in user_groups and not user.is_superuser
        is_teacher = "teacher" in user_groups and not user.is_superuser
        is_admin_like = user.is_superuser or bool(user_groups & {"system_admin", "principal", "elementary_principal", "registrar"})
        if is_student and not is_admin_like:
            rows = rows.exclude(audience="teachers")
        elif is_teacher and not is_admin_like:
            rows = rows.exclude(audience="students")

    can_edit = bool(
        user
        and user.is_authenticated
        and (user.is_superuser or user.groups.filter(name__in=("system_admin", "principal", "elementary_principal", "registrar")).exists())
    )
    return {
        "month": first.strftime("%Y-%m"),
        "today": today.isoformat(),
        "canEdit": can_edit,
        "categories": [{"key": key, "label": label} for key, label in SchoolEvent.CATEGORY_CHOICES],
        "audiences": [{"key": key, "label": label} for key, label in getattr(SchoolEvent, "AUDIENCE_CHOICES", [("all","Everyone"),("students","Students Only"),("teachers","Teachers Only")])],
        "events": [
            {
                "id": ev.id,
                "title": ev.title,
                "description": ev.description or "",
                "category": ev.category,
                "startDate": ev.start_date.isoformat(),
                "endDate": ev.end_date.isoformat() if ev.spans_multiple_days else None,
                "allDay": ev.all_day,
                "startTime": ev.start_time.strftime("%H:%M") if ev.start_time else None,
                "endTime": ev.end_time.strftime("%H:%M") if ev.end_time else None,
                "location": ev.location or "",
                "status": ev.status,
                "audience": getattr(ev, "audience", "all"),
            }
            for ev in rows.order_by("start_date", "title")
        ],
    }


def _self_profile(user):
    """The signed-in user's own record, for the Profile panel on every dashboard.

    Only stored columns are reported. A field the account does not have stays
    null so the panel can print an em dash instead of a placeholder identity.
    """
    if not user or not user.is_authenticated:
        return None
    employee = getattr(user, "employee_profile", None)
    student = getattr(user, "student_profile", None)
    role_label = None
    ra = (
        user.role_assignments.filter(academic_year__status="ACTIVE")
        .select_related("role")
        .last()
    )
    if ra and hasattr(ra, "role"):
        role_label = ra.role.get_code_display() or None
    if not role_label and student is not None:
        role_label = "Student"
    if not role_label:
        role_label = "Unassigned"
    if user.is_superuser:
        role_label = "Superuser"

    # A person can hold both profiles (a staff member who is also a student).
    # Prefer the student record when their resolved role is Student, so the
    # dashboard shows an LRN rather than an employee number.
    person = student if (role_label == "Student" and student is not None) else (employee or student)

    # The number and its label must come from the same record, otherwise a
    # student with an employee profile is labelled "LRN" and given EMP-0004.
    if person is student and student is not None:
        account_no, account_no_label = student.lrn or None, "LRN"
    elif person is employee and employee is not None:
        account_no, account_no_label = employee.employee_number or None, "Employee No."
    else:
        account_no, account_no_label = None, "Account No."

    def when(value, fmt="%b %d, %Y · %I:%M %p"):
        return timezone.localtime(value).strftime(fmt) if value else None

    groups = sorted(g.name for g in user.groups.all())

    # FieldFile.url raises when nothing was uploaded, so check truthiness first.
    # Use the stable /accounts/avatar/<key>/ path rather than .url: with a
    # private R2 bucket .url is a presigned URL that expires, and this payload
    # is embedded in the page and can sit there far longer than the signature.
    avatar = None
    if person is not None and getattr(person, "avatar", None):
        avatar = avatar_url(person.avatar) or None

    return {
        "name": user.get_full_name() or user.email,
        "firstName": getattr(person, "first_name", "") or "",
        "lastName": getattr(person, "last_name", "") or "",
        "middleInitial": getattr(person, "middle_initial", "") or "",
        "email": user.email,
        "role": role_label,
        "groups": groups,
        "status": user.get_status_display() if hasattr(user, "get_status_display") else user.status,
        "accountNo": account_no,
        "accountNoLabel": account_no_label,
        "contact": getattr(person, "contact_number", None) or None,
        "address": getattr(student, "address", None) or None,
        "birthdate": (getattr(student, "birthdate", None).isoformat() if getattr(student, "birthdate", None) else None),
        "sex": (dict(student._meta.get_field("sex").choices).get(student.sex) if student and student.sex else None),
        "guardian": getattr(student, "guardian_name", None) or None,
        "guardianContact": getattr(student, "guardian_contact_number", None) or None,
        "guardianEmail": getattr(student, "guardian_email", None) or None,
        "initials": user.get_initials() if hasattr(user, "get_initials") else None,
        "avatar": avatar,
        "createdAt": when(user.date_created),
        "lastLogin": when(user.last_login),
        "passwordChanged": when(user.password_changed_at),
        "failedAttempts": user.failed_login_attempts,
        "isActive": bool(user.is_active),
        "isSuperuser": bool(user.is_superuser),
    }


def _sex_label(value):
    """``M``/``F`` → a display label, anything else → ``""``.

    Empty means "not recorded" and the front end draws an em dash for it, so a
    missing value is never silently reported as Male or Female.
    """
    return {"M": "Male", "F": "Female"}.get(value or "", "")


def _age_years(birthdate):
    """Whole years since ``birthdate``, or ``None`` when it is not on file."""
    if not birthdate:
        return None
    today = timezone.localdate()
    return (
        today.year
        - birthdate.year
        - ((today.month, today.day) < (birthdate.month, birthdate.day))
    )


def _demo_row(name, code, gender, birth, group, meta=""):
    """One row for the Age/Sex demographics panels.

    Shared by the learner and staff variants so a single front-end renderer can
    draw either list. ``gender`` and ``birth`` are empty strings when the record
    has no value on file.
    """
    return {
        "name": name or "",
        "code": code or "",
        "gender": gender or "",
        "birth": birth or "",
        "group": group or "",
        "meta": meta or "",
    }


def _learner_demo_row(profile, grade_label="", section_name=""):
    """Demographics row for one :class:`StudentProfile`."""
    if profile is None:
        return None
    name = " ".join(
        filter(None, [profile.first_name, profile.middle_initial, profile.last_name])
    ) or (profile.user.get_full_name() if profile.user_id else "")
    return _demo_row(
        name=name,
        code=getattr(profile, "lrn", "") or "",
        gender=_sex_label(getattr(profile, "sex", None)),
        birth=(
            profile.birthdate.isoformat()
            if getattr(profile, "birthdate", None)
            else ""
        ),
        group=" – ".join(p for p in (grade_label, section_name) if p),
        meta="Learner",
    )


def _employee_demo_rows(user_accounts):
    """Demographics rows for staff, from ``EmployeeProfile.sex``/``birthdate``.

    ``sex`` and ``birthdate`` exist on the model but had never been serialized,
    so the staff panels could only ever report an empty split. ``user_accounts``
    is the System Administrator's own account list, which already carries each
    user's role labels, so rows line up one-to-one with the Accounts table.

    A user whose only identity is a learner (student profile, ``student`` role
    alone) is skipped even if a stray ``EmployeeProfile`` row exists, so the
    staff panel counts staff and never drifts into listing enrolled learners.
    """
    rows = []
    for account in user_accounts:
        user = account.get("user")
        profile = getattr(user, "employee_profile", None)
        if profile is None:
            continue
        role_codes = set(account.get("role_codes") or [])
        if (
            role_codes == {"student"}
            and getattr(user, "student_profile", None) is not None
        ):
            continue
        rows.append(
            _demo_row(
                name=account.get("name")
                or "{} {}".format(profile.first_name, profile.last_name),
                code=profile.employee_number or account.get("emp", ""),
                gender=_sex_label(profile.sex),
                birth=(
                    profile.birthdate.isoformat() if profile.birthdate else ""
                ),
                group=account.get("role") or "Unassigned",
                meta=account.get("emp") or account.get("email", ""),
            )
        )
    return rows


def _all_staff_demo_rows():
    """Demographics rows for every staff account, System-Admin-panel parity.

    The Principal/QA staff panel previously listed only teachers who taught a
    visible classroom, so registrars, the QA office and the system
    administrator never appeared. This builds the same all-roles list the ICT
    dashboard shows, reading sex/birthdate from EmployeeProfile and the role
    label from active-year RoleAssignments. Learners whose only role is
    ``student`` are skipped, exactly like the System Administrator panel.
    """
    rows = []
    accounts = (
        User.objects.filter(employee_profile__isnull=False)
        .exclude(status="archived")
        .select_related("employee_profile", "student_profile")
        .order_by("employee_profile__last_name", "employee_profile__first_name")
    )
    for u in accounts:
        profile = u.employee_profile
        assignments = list(
            u.role_assignments.filter(academic_year__status="ACTIVE")
            .select_related("role")
            .order_by("role__code")
        )
        role_codes = [a.role.code for a in assignments if a.role_id]
        role_labels = [a.role.get_code_display() for a in assignments if a.role_id]
        if (
            getattr(u, "student_profile", None) is not None
            and "student" not in role_codes
        ):
            role_codes.insert(0, "student")
            role_labels.insert(0, "Student")
        if set(role_codes) == {"student"}:
            continue
        name = " ".join(
            p
            for p in (
                profile.first_name,
                profile.middle_initial,
                profile.last_name,
            )
            if p
        ).strip() or u.get_full_name() or u.email
        rows.append(
            _demo_row(
                name=name,
                code=profile.employee_number or "",
                gender=_sex_label(profile.sex),
                birth=profile.birthdate.isoformat() if profile.birthdate else "",
                group=role_labels[0] if role_labels else "Unassigned",
                meta="Staff",
            )
        )
    return rows


def _student_demo_rows():
    """Demographics rows for every learner profile.

    Same row shape as the staff variant, so the shared renderer draws the
    System Administrator's Learner panel exactly like the Registrar's. Grade
    and section come from the current enrolled section (``section_display``).
    """
    rows = []
    profiles = order_students(
        StudentProfile.objects.select_related("user").prefetch_related(
            "enrollments__section"
        )
    )
    for prof in profiles:
        grade = ""
        section = ""
        sec_disp = prof.section_display
        if sec_disp and " - " in sec_disp:
            grade = "Grade " + sec_disp.split(" - ")[0].replace("Grade ", "")
            section = sec_disp.split(" - ", 1)[1]
        row = _learner_demo_row(prof, grade_label=grade, section_name=section)
        if row:
            row["meta"] = (
                prof.user.get_status_display() if prof.user else "Learner"
            )
            rows.append(row)
    return rows


def _sf2_template_payload():
    """The school's current active SF2 template (None when none is uploaded).

    The System Administrator uploads templates from the ICT dashboard; the
    Teacher dashboard reads this so the newest official SF2 form is offered
    to teachers automatically, without anyone asking for the file.
    """
    tpl = (
        SFReportTemplate.objects.filter(
            form_type=SFReportTemplate.FORM_SF2, is_active=True
        )
        .order_by("-uploaded_at")
        .first()
    )
    if tpl is None:
        return None
    return {
        "id": tpl.pk,
        "name": tpl.name,
        "url": reverse("analytics:sf_template_download", args=[tpl.pk]),
        "at": (
            timezone.localtime(tpl.uploaded_at).strftime("%b %d, %Y")
            if tpl.uploaded_at
            else "—"
        ),
    }


def system_admin_context(user):
    active_sessions = LoginSession.objects.select_related("user").order_by("-last_active_at")[:8]
    pending_statuses = ("pending_verification", "pending_password_setup")
    pending_users = (
        User.objects.filter(status__in=pending_statuses)
        .order_by("date_created")[:6]
    )
    role_counts = {
        code: _role_user_count(code)
        for code in (
            "system_admin", "registrar", "teacher", "principal",
            "assistant_principal", "elementary_principal", "quality_audit", "student",
        )
    }
    locked_accounts = list(locked_accounts_queryset(limit=6))
    total_locked = User.objects.filter(status="locked").count()
    user_accounts = []
    for u in (
        User.objects.exclude(status="archived")
        .select_related("employee_profile", "student_profile")
        .order_by("date_created")
    ):
        # A user may hold several roles at once (e.g. Teacher + Principal), so
        # every active-year assignment is collected. The old code took `.last()`
        # and therefore hid every role but one, which is why the admin UI could
        # never show or edit a second role.
        assignments = list(
            u.role_assignments.filter(academic_year__status="ACTIVE")
            .select_related("role")
            .order_by("role__code")
        )
        role_codes = [a.role.code for a in assignments if a.role_id]
        role_labels = [a.role.get_code_display() for a in assignments if a.role_id]
        if hasattr(u, "student_profile") and "student" not in role_codes:
            role_codes.insert(0, "student")
            role_labels.insert(0, "Student")

        role_label = role_labels[0] if role_labels else "Unassigned"
        employee = getattr(u, "employee_profile", None)
        emp_no = getattr(employee, "employee_number", None) or (
            getattr(getattr(u, "student_profile", None), "lrn", None) if hasattr(u, "student_profile") else ""
        )
        user_accounts.append({
            "user": u,
            "name": u.get_full_name(),
            "emp_no": emp_no or "",
            "email": u.email,
            "id": u.pk,
            "role": role_label,
            "roles": role_labels,
            "role_codes": role_codes,
            "status": u.get_status_display(),
            "last": u.password_changed_at or u.date_created,
        })
    stats = [
        _stat("Total Accounts", _non_archived_users().count(), "fa-users"),
        _stat("Students", _role_user_count("student"), "fa-user-graduate"),
        _stat("Teachers", _role_user_count("teacher"), "fa-chalkboard-user"),
        _stat("Pending Verification", User.objects.filter(status="pending_verification").count(), "fa-envelope"),
        _stat("Password Setup", User.objects.filter(status="pending_password_setup").count(), "fa-key"),
        _stat("Locked Accounts", total_locked, "fa-lock"),
    ]

    # ── Real-data payload for the new front-end dashboard ─────────────────
    account_styles = [
        "linear-gradient(135deg,#1e5faf,#3a86d4)",
        "linear-gradient(135deg,#16a34a,#4ade80)",
        "linear-gradient(135deg,#f59e0b,#fbbf24)",
        "linear-gradient(135deg,#7c3aed,#a78bfa)",
        "linear-gradient(135deg,#e11d48,#f43f5e)",
        "linear-gradient(135deg,#0891b2,#22d3ee)",
        "linear-gradient(135deg,#b45309,#fbbf24)",
        "linear-gradient(135deg,#9333ea,#a78bfa)",
        "linear-gradient(135deg,#334155,#64748b)",
    ]
    accounts_json = []
    for idx, a in enumerate(user_accounts):
        u = a["user"]
        last = timezone.localtime(a["last"]).strftime("%b %d, %Y · %I:%M %p") if a["last"] else "—"
        accounts_json.append(
            {
                "id": a["id"],
                "name": a["name"],
                "emp": a["emp_no"],
                "email": a["email"],
                # ``role`` stays the primary label for existing single-select
                # UI; ``roles``/``role_codes`` carry the full multi-role set.
                "role": a["role"],
                "roles": a["roles"],
                "role_codes": a["role_codes"],
                "initials": u.get_initials(),
                "gradient": account_styles[idx % len(account_styles)],
                "status": a["status"],
                "last": last,
                # Staff demographics on file (EmployeeProfile) so the Edit
                # Account modal can show and change them; missing values stay
                # empty strings so they render as an em dash, never a guess.
                "sex": getattr(u, "employee_profile", None) and getattr(u.employee_profile, "sex", "") or "",
                "birthdate": (
                    getattr(u, "employee_profile", None).birthdate.isoformat()
                    if getattr(u, "employee_profile", None)
                    and getattr(u.employee_profile, "birthdate", None)
                    else ""
                ),
            }
        )

    sessions_json = []
    for ls in active_sessions:
        su = ls.user
        sname = su.get_full_name() or su.email
        role_lbl = "User"
        ra = su.role_assignments.filter(academic_year__status="ACTIVE").select_related("role").last()
        if ra and hasattr(ra, "role"):
            role_lbl = ra.role.get_code_display() or role_lbl
        sessions_json.append(
            {
                "name": sname,
                "role": role_lbl,
                "device": ls.device_info or "Unknown device",
                "browser": ls.ip_address or "—",
                "time": timezone.localtime(ls.last_active_at).strftime("%b %d · %I:%M %p"),
                "initials": su.get_initials(),
                "gradient": account_styles[sessions_json.__len__() % len(account_styles)],
            }
        )

    # The card is now actionable, so each row carries the User pk the unlock
    # endpoint needs, the real lock timestamp, the attempt count that caused the
    # lock, and whether the signed-in administrator may actually press the
    # button. Previously it sent a name and a made-up reason with no id, which is
    # why the card could only ever display a list.
    can_unlock = has_permission(user, "unlock_account")
    can_reset = has_permission(user, "reset_account_password")

    locked_json = []
    for i, lu in enumerate(locked_accounts):
        row = serialize_locked_account(lu, can_unlock=can_unlock, can_reset=can_reset)
        row["gradient"] = account_styles[i % len(account_styles)]
        locked_json.append(row)

    activity_json = []
    for al in ActivityLog.objects.select_related("user").order_by("-timestamp")[:12]:
        uname = al.user.get_full_name() if al.user else "System"
        activity_json.append(
            {
                "type": "security",
                "icon": "fa-user-shield",
                "cls": "security",
                "text": "{} — {}".format(al.action, uname),
                "role": "System Administrator",
                "time": timezone.localtime(al.timestamp).strftime("%b %d, %Y · %I:%M %p"),
            }
        )

    # ── Measured system state (never asserted) ───────────────────────────
    cfg = SystemConfiguration.get_solo()
    health = _measured_health()

    # A session counts as online only if it was seen inside the configured
    # timeout. LoginSession.is_current is not a liveness signal — every row
    # carries it, so using it reported 279 "active" sessions out of 279.
    timeout = getattr(cfg, "session_timeout_minutes", None) or 30
    live_cutoff = timezone.now() - timedelta(minutes=timeout)
    live_sessions = (
        LoginSession.objects.select_related("user")
        .filter(last_active_at__gte=live_cutoff)
        .order_by("-last_active_at")
    )
    live_count = live_sessions.count()
    online_json = []
    for ls in live_sessions[:8]:
        su = ls.user
        sname = su.get_full_name() or su.email
        ra = su.role_assignments.filter(academic_year__status="ACTIVE").select_related("role").last()
        role_lbl = ra.role.get_code_display() if ra and hasattr(ra, "role") else "Unassigned"
        if hasattr(su, "student_profile"):
            role_lbl = "Student"
        online_json.append({
            "n": sname,
            "r": role_lbl,
            "g": account_styles[len(online_json) % len(account_styles)],
            "t": timezone.localtime(ls.last_active_at).strftime("%b %d · %I:%M %p"),
        })

    # Recent sign-ins come from the audit trail, not from an invented list.
    recent_logins = []
    for al in (
        ActivityLog.objects.select_related("user")
        .filter(action__in=["user_login", "new_device_login", "session_revoked"])
        .order_by("-timestamp")[:10]
    ):
        who = al.user.get_full_name() if al.user else "Unknown"
        if al.action == "user_login":
            verb, icon, kind = "signed in", "fa-right-to-bracket", "info"
        elif al.action == "new_device_login":
            verb, icon, kind = "signed in from a new device", "fa-mobile-screen", "warning"
        else:
            verb, icon, kind = "had a session revoked", "fa-ban", "danger"
        recent_logins.append({
            "n": who,
            "e": al.user.email if al.user else "—",
            "a": verb,
            "icon": icon,
            "level": kind,
            "t": timezone.localtime(al.timestamp).strftime("%b %d, %Y · %I:%M %p"),
        })

    day_ago = timezone.now() - timedelta(days=1)
    security_stats = {
        "logins24h": ActivityLog.objects.filter(action="user_login", timestamp__gte=day_ago).count(),
        "newDevices24h": ActivityLog.objects.filter(action="new_device_login", timestamp__gte=day_ago).count(),
        "revoked": ActivityLog.objects.filter(action="session_revoked", timestamp__gte=day_ago).count(),
        "passwordResets": ActivityLog.objects.filter(action="admin_password_reset", timestamp__gte=day_ago).count(),
        "locked": User.objects.filter(status="locked").count(),
        "pending": User.objects.filter(status__in=pending_statuses).count(),
        "liveSessions": live_count,
        "totalSessions": LoginSession.objects.count(),
        "unassigned": User.objects.exclude(status=STATUS_ARCHIVED)
        .filter(is_active=True)
        .exclude(role_assignments__academic_year__status="ACTIVE")
        .exclude(groups__name="student")
        .distinct()
        .count(),
    }

    # Notifications are derived from real pending work, as on the other panels.
    notifs_json = []
    pending_count = security_stats["pending"]
    if pending_count:
        notifs_json.append({
            "icon": "fa-envelope", "t": "Accounts awaiting verification",
            "msg": "{} account(s) cannot sign in until verified.".format(pending_count),
            "time": "Now", "unread": True,
        })
    if security_stats["unassigned"]:
        notifs_json.append({
            "icon": "fa-user-slash", "t": "Role assignment missing",
            "msg": "{} account(s) have no role for the active year.".format(security_stats["unassigned"]),
            "time": "Now", "unread": True,
        })
    if security_stats["newDevices24h"]:
        notifs_json.append({
            "icon": "fa-mobile-screen", "t": "New device sign-in",
            "msg": "{} new-device login(s) in the last 24 hours.".format(security_stats["newDevices24h"]),
            "time": "24h", "unread": True,
        })
    if not BackupRecord.objects.filter(status=BackupRecord.STATUS_COMPLETED).exists():
        notifs_json.append({
            "icon": "fa-database", "t": "No backup on record",
            "msg": "This system has no recovery point yet.",
            "time": "Now", "unread": True,
        })
    open_reviews = RoleAccessReview.objects.filter(completed_at__isnull=True).count()
    if open_reviews:
        notifs_json.append({
            "icon": "fa-clipboard-check", "t": "Access review open",
            "msg": "{} annual role review(s) not yet completed.".format(open_reviews),
            "time": "Now", "unread": False,
        })

    # Backups: the real BackupRecord rows. There is no size column on the
    # model, so size stays null instead of showing an invented "4.2 GB".
    backup_history = [
        {
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
        }
        for b in BackupRecord.objects.select_related("initiated_by").order_by("-created_at")[:10]
    ]

    backup_settings = dict(getattr(cfg, "backup_settings", None) or {})

    # ── Directory lists for the Sections / Subjects / Classrooms sidebar views ──
    # `grading_period` is a free-text CharField with no choices, so Django's
    # get_<field>_display() just echoes the stored code. The field documents the
    # shape as e.g. "1ST_TRIMESTER", so split it into "1st Trimester" and fall
    # back to the raw code for anything that does not fit.
    _ORDINALS = {"1ST": "1st", "2ND": "2nd", "3RD": "3rd", "4TH": "4th"}

    def _period(value):
        if not value:
            return None
        parts = str(value).strip().split("_")
        if len(parts) == 2 and parts[0].upper() in _ORDINALS:
            return f"{_ORDINALS[parts[0].upper()]} {parts[1].replace('_', ' ').title()}"
        return str(value).replace("_", " ").title()

    # Every field below is read straight off a stored column. Nothing is
    # derived from a seed list, so an empty table renders as an empty state.
    active_year_obj = _active_academic_year()
    year_qs = Section.objects.all()
    if active_year_obj is not None:
        year_qs = year_qs.filter(academic_year=active_year_obj)

    section_enroll_counts = dict(
        StudentEnrollment.objects.filter(
            section__in=year_qs.values("id")
        )
        .exclude(status__in=["archived", "dropped"])
        .values_list("section_id")
        .annotate(n=Count("id"))
    )
    section_room_counts = dict(
        VirtualClassroom.objects.filter(section__in=year_qs.values("id"))
        .values_list("section_id")
        .annotate(n=Count("id"))
    )
    directory_sections = [
        {
            "id": s.id,
            "name": s.name,
            "gradeLevel": s.grade_level,
            "status": s.get_status_display() if hasattr(s, "get_status_display") else s.status,
            "adviser": (s.adviser.get_full_name() if s.adviser_id else None),
            "classrooms": section_room_counts.get(s.id, 0),
            "students": section_enroll_counts.get(s.id, 0),
        }
        for s in year_qs.select_related("adviser", "academic_year").order_by("grade_level", "name")
    ]

    # ── Sections across all school years (view-sections year filter) ────────
    # The roster may span several S.Y.s, but directory.sections stays scoped to
    # the active year because that powers the section assignment the registrar
    # is editing right now. These two extras let the System Administrator pull
    # up any past year's sections from the Sections panel.
    section_years = list(
        AcademicYear.objects.order_by("-start_date").values_list("label", flat=True)
    )
    all_year_sections = Section.objects.all()
    if section_years:
        all_year_sections = all_year_sections.filter(academic_year__label__in=section_years)
    all_section_enroll_counts = dict(
        StudentEnrollment.objects.filter(section__in=all_year_sections.values("id"))
        .exclude(status__in=["archived", "dropped"])
        .values_list("section_id")
        .annotate(n=Count("id"))
    )
    all_section_room_counts = dict(
        VirtualClassroom.objects.filter(section__in=all_year_sections.values("id"))
        .values_list("section_id")
        .annotate(n=Count("id"))
    )
    sections_by_year = {}
    for s in all_year_sections.select_related("adviser", "academic_year").order_by(
        "academic_year__start_date", "grade_level", "name"
    ):
        yr = (s.academic_year.label if s.academic_year_id else "—")
        row = {
            "id": s.id,
            "name": s.name,
            "gradeLevel": s.grade_level,
            "year": yr,
            "status": s.get_status_display() if hasattr(s, "get_status_display") else s.status,
            "adviser": (s.adviser.get_full_name() if s.adviser_id else None),
            "classrooms": all_section_room_counts.get(s.id, 0),
            "students": all_section_enroll_counts.get(s.id, 0),
        }
        sections_by_year.setdefault(yr, []).append(row)

    # ── Subjects across all school years (view-subjects year filter) ────────
    all_year_subjects = Subject.objects.all()
    if section_years:
        all_year_subjects = all_year_subjects.filter(academic_year__label__in=section_years)
    all_subject_room_counts = dict(
        VirtualClassroom.objects.filter(subject__in=all_year_subjects.values("id"))
        .values_list("subject_id")
        .annotate(n=Count("id"))
    )
    all_subject_assessment_counts = dict(
        Assessment.objects.filter(subject__in=all_year_subjects.values("id"))
        .values_list("subject_id")
        .annotate(n=Count("id"))
    )
    subjects_by_year = {}
    for s in all_year_subjects.select_related("academic_year").order_by(
        "academic_year__start_date", "grade_level", "name"
    ):
        yr = (s.academic_year.label if s.academic_year_id else "—")
        row = {
            "id": s.id,
            "name": s.name,
            "gradeLevel": s.grade_level,
            "year": yr,
            "subjectType": (s.get_subject_type_display() if hasattr(s, "get_subject_type_display") else s.subject_type),
            "gradingPeriod": _period(s.get_grading_period_display() if hasattr(s, "get_grading_period_display") else s.grading_period),
            "status": s.get_status_display() if hasattr(s, "get_status_display") else s.status,
            "classrooms": all_subject_room_counts.get(s.id, 0),
            "assessments": all_subject_assessment_counts.get(s.id, 0),
        }
        subjects_by_year.setdefault(yr, []).append(row)

    # ── Virtual Classrooms across all school years (view-classrooms filter) ─
    all_year_rooms = VirtualClassroom.objects.all()
    if section_years:
        all_year_rooms = all_year_rooms.filter(academic_year__label__in=section_years)
    all_room_enroll_counts = dict(
        VirtualClassroomEnrollment.objects.filter(virtual_classroom__in=all_year_rooms.values("id"))
        .exclude(status__in=["removed", "archived", "dropped"])
        .values_list("virtual_classroom_id")
        .annotate(n=Count("id"))
    )
    classrooms_by_year = {}
    for vc in all_year_rooms.select_related("subject", "section", "teacher", "academic_year").order_by(
        "academic_year__start_date", "subject__name"
    ):
        yr = (vc.academic_year.label if vc.academic_year_id else "—")
        row = {
            "id": vc.id,
            "name": vc.subject.name,
            "subjectId": vc.subject_id,
            "sectionId": vc.section_id,
            "section": vc.section.name,
            "gradeLevel": vc.subject.grade_level,
            "teacher": (vc.teacher.get_full_name() if vc.teacher_id else None),
            "teacherId": vc.teacher_id,
            "year": yr,
            "enrolled": all_room_enroll_counts.get(vc.id, 0),
            "status": vc.get_status_display() if hasattr(vc, "get_status_display") else vc.status,
            "gradingPeriod": _period(vc.get_grading_period_display() if hasattr(vc, "get_grading_period_display") else vc.grading_period),
        }
        classrooms_by_year.setdefault(yr, []).append(row)

    # ── Students per school year (view-students year filter) ────────────────
    # A learner can appear once per S.Y. they were enrolled, showing how the
    # registrar sectioned them for that year. This is separate from the flat
    # dashboard students list, which powers the active-year Assign action.
    students_by_year = {}
    for se in order_students(
        StudentEnrollment.objects.filter(academic_year__label__in=section_years)
        .select_related("student__user", "section", "academic_year")
        .exclude(status__in=["archived", "dropped"]),
        prefix="student__",
        leading_order=(
            "academic_year__start_date",
            "section__grade_level",
        ),
    ):
        sp = se.student
        yr = (se.academic_year.label if se.academic_year_id else "—")
        _name = " ".join(x for x in (sp.first_name, sp.middle_initial, sp.last_name) if x)
        _initials = "".join(x[0] for x in (sp.first_name, sp.last_name) if x).upper()
        row = {
            "lrn": sp.lrn,
            "name": _name,
            "first": sp.first_name,
            "last": sp.last_name,
            "grade": (se.section.grade_level if se.section_id else None),
            "section": (se.section.name if se.section_id else ""),
            "sectionId": (se.section_id or None),
            "sex": sp.sex or "",
            "status": (sp.user.get_status_display() if sp.user_id else "Unassigned"),
            "email": (sp.user.email if sp.user_id else ""),
            "initials": _initials or "..",
            "gradient": account_styles[len(students_by_year.get(yr, [])) % len(account_styles)],
        }
        students_by_year.setdefault(yr, []).append(row)

    subject_qs = Subject.objects.all()
    if active_year_obj is not None:
        subject_qs = subject_qs.filter(academic_year=active_year_obj)
    subject_room_counts = dict(
        VirtualClassroom.objects.filter(subject__in=subject_qs.values("id"))
        .values_list("subject_id")
        .annotate(n=Count("id"))
    )
    subject_assessment_counts = dict(
        Assessment.objects.filter(subject__in=subject_qs.values("id"))
        .values_list("subject_id")
        .annotate(n=Count("id"))
    )
    directory_subjects = [
        {
            "id": s.id,
            "name": s.name,
            "gradeLevel": s.grade_level,
            "subjectType": (s.get_subject_type_display() if hasattr(s, "get_subject_type_display") else s.subject_type),
            "gradingPeriod": _period(s.get_grading_period_display() if hasattr(s, "get_grading_period_display") else s.grading_period),
            "status": s.get_status_display() if hasattr(s, "get_status_display") else s.status,
            "classrooms": subject_room_counts.get(s.id, 0),
            "assessments": subject_assessment_counts.get(s.id, 0),
        }
        for s in subject_qs.order_by("grade_level", "name")
    ]

    room_qs = VirtualClassroom.objects.all()
    if active_year_obj is not None:
        room_qs = room_qs.filter(academic_year=active_year_obj)
    room_enroll_counts = dict(
        VirtualClassroomEnrollment.objects.filter(virtual_classroom__in=room_qs.values("id"))
        .exclude(status__in=["removed", "archived", "dropped"])
        .values_list("virtual_classroom_id")
        .annotate(n=Count("id"))
    )
    directory_classrooms = [
        {
            "id": vc.id,
            "name": vc.subject.name,
            "subjectId": vc.subject_id,
            "sectionId": vc.section_id,
            "section": vc.section.name,
            "gradeLevel": vc.subject.grade_level,
            "teacher": (vc.teacher.get_full_name() if vc.teacher_id else None),
            "teacherId": vc.teacher_id,
            "enrolled": room_enroll_counts.get(vc.id, 0),
            "status": vc.get_status_display() if hasattr(vc, "get_status_display") else vc.status,
            "gradingPeriod": _period(vc.get_grading_period_display() if hasattr(vc, "get_grading_period_display") else vc.grading_period),
        }
        for vc in room_qs.select_related("subject", "section", "teacher", "academic_year").order_by("subject__name")
    ]

    # ── School years (view-schoolyear) ───────────────────────────────────────
    # Every AcademicYear with its structure counts, newest first. Powers the
    # year-management table plus the create/activate/archive flow.
    school_year_rows = []
    for y in (
        AcademicYear.objects.select_related("structure_version").annotate(
            _sect=Count("sections", distinct=True),
            _subj=Count("subjects", distinct=True),
            _stu=Count("student_enrollments", distinct=True),
        ).order_by("-start_date")
    ):
        _sv = getattr(y, "structure_version", None)
        _gpt = getattr(_sv, "grading_period_type", None) if _sv else None
        school_year_rows.append({
            "id": y.id,
            "label": y.label,
            "start": y.start_date.isoformat(),
            "end": y.end_date.isoformat(),
            "status": "Active" if y.status == AcademicYear.STATUS_ACTIVE else "Archived",
            "sections": y._sect,
            "subjects": y._subj,
            "enrolled": y._stu,
            "gradingPeriod": {
                "code": _gpt,
                "label": (_sv.get_grading_period_type_display() if _gpt else None),
            } if _gpt else None,
        })

    directory = {
        "year": (active_year_obj.label if active_year_obj else None),
        "sections": directory_sections,
        "subjects": directory_subjects,
        "classrooms": directory_classrooms,
        "academicYears": section_years,
        "sectionsByYear": sections_by_year,
        "subjectsByYear": subjects_by_year,
        "classroomsByYear": classrooms_by_year,
        "studentsByYear": students_by_year,
        "schoolYears": school_year_rows,
    }

    # ── Students (view-students) ────────────────────────────────────────────
    # Every learner record with its active-year section. Students with no
    # enrollment in the active year still appear so they can be (re)assigned.
    active_enroll = {}
    if active_year_obj is not None:
        for se in (
            StudentEnrollment.objects.filter(academic_year=active_year_obj)
            .exclude(status__in=["archived", "dropped"])
            .select_related("section")
            .order_by("section__grade_level", "section__name")
        ):
            active_enroll.setdefault(se.student_id, se)

    students_json = []
    for i, sp in enumerate(
        order_students(StudentProfile.objects.select_related("user"))
    ):
        se = active_enroll.get(sp.pk)
        section = se.section if se and se.section_id else None
        grade = section.grade_level if section else None
        name = " ".join(x for x in (sp.first_name, sp.middle_initial, sp.last_name) if x)
        initials = "".join(
            x[0] for x in (sp.first_name, sp.last_name) if x
        ).upper()
        students_json.append({
            "lrn": sp.lrn,
            "name": name,
            "first": sp.first_name,
            "last": sp.last_name,
            "grade": grade,
            "section": section.name if section else "",
            "sectionId": section.pk if section else None,
            "sex": sp.sex or "",
            "status": sp.user.get_status_display() if sp.user_id else "Unassigned",
            "email": sp.user.email if sp.user_id else "",
            "initials": initials or "..",
            "gradient": account_styles[i % len(account_styles)],
        })

    # ── Assessments (view-assessmentrepo) ───────────────────────────────────
    # A read-only repository of every assessment stored on the platform. The
    # number of classes each assessment is assigned to comes from the join
    # table, so a template reused across sections counts once per section.
    assessment_json = []
    assess_class_counts = dict(
        AssessmentAssignment.objects.values_list("assessment_id").annotate(n=Count("id"))
    )
    for a in (
        Assessment.objects.select_related("subject", "created_by")
        .annotate(part_count=Count("parts", distinct=True))
        .order_by("-created_at")
    ):
        assessment_json.append({
            "id": a.id,
            "title": a.title,
            "subject": (a.subject.name if a.subject_id else "—"),
            "grade": (a.subject.grade_level if a.subject_id else None),
            "type": (a.get_assessment_type_display() if hasattr(a, "get_assessment_type_display") else (a.assessment_type or "")),
            "method": (a.get_assessment_method_display() if hasattr(a, "get_assessment_method_display") else (a.assessment_method or "")),
            "parts": a.part_count,
            "classes": assess_class_counts.get(a.id, 0),
            "by": (a.created_by.get_full_name() if a.created_by_id else "System"),
            "updated": timezone.localtime(a.created_at).strftime("%b %d, %Y"),
        })

    # ── Announcements (view-announcements) ──────────────────────────────────
    announcement_json = [
        {
            "id": ann.id,
            "title": ann.title,
            "content": ann.content,
            "by": (ann.posted_by.get_full_name() if ann.posted_by_id else "School"),
            "at": (timezone.localtime(ann.posted_at).strftime("%b %d, %Y · %I:%M %p") if ann.posted_at else "—"),
        }
        for ann in (
            campus.filter_school_announcements(
                SchoolAnnouncement.objects.select_related("posted_by"), user
            )
            .filter(status="active")
            .order_by("-posted_at")[:20]
        )
    ]

    # ── Attendance overview (view-attendance) ──────────────────────────────
    # Read-only school-wide attendance from real AttendanceRecord rows, one
    # row per Virtual Classroom per school year, mirroring the Principal's
    # panel. Grouped by S.Y. so past years can be inspected too.
    attend_year_list = list(
        VirtualClassroom.objects.filter(status=VirtualClassroom.STATUS_ACTIVE)
        .order_by("-academic_year__start_date")
        .values_list("academic_year__label", flat=True)
        .distinct()
    )
    attendance_by_year = {}
    for yr in attend_year_list:
        yr_vcs = VirtualClassroom.objects.filter(
            status=VirtualClassroom.STATUS_ACTIVE, academic_year__label=yr
        )
        yr_rows = []
        yr_enroll = dict(
            VirtualClassroomEnrollment.objects.filter(virtual_classroom__in=yr_vcs.values("id"))
            .exclude(status__in=["removed", "archived", "dropped"])
            .values_list("virtual_classroom_id")
            .annotate(n=Count("id"))
        )
        for vc in yr_vcs.select_related("section", "teacher").order_by(
            "section__grade_level", "section__name", "subject__name"
        ):
            vc_recs = AttendanceRecord.objects.filter(session__virtual_classroom=vc)
            vc_total = vc_recs.count()
            vc_present = vc_recs.filter(status__in=PRESENT_STATUSES).count()
            vc_sessions = AttendanceSession.objects.filter(virtual_classroom=vc).count()
            last_rec = vc_recs.order_by("-session__session_date").first()
            yr_rows.append({
                "id": vc.id,
                "cl": "G{}-{}".format(vc.section.grade_level, vc.section.name) if vc.section_id else "—",
                "section": vc.section.name if vc.section_id else "",
                "grade": vc.section.grade_level if vc.section_id else None,
                "vc": str(vc),
                "subject": vc.subject.name if vc.subject_id else "—",
                "teacher": (vc.teacher.get_full_name() if vc.teacher_id else "Unassigned"),
                "students": yr_enroll.get(vc.id, 0),
                "sessions": vc_sessions,
                "present": vc_present,
                "total": vc_total,
                "avg": round(vc_present / vc_total * 100, 1) if vc_total else 0,
                "last": (last_rec.session.session_date.strftime("%b %d, %Y") if last_rec and last_rec.session_id and last_rec.session.session_date else "—"),
                "status": "at-risk" if vc_total and (vc_present / vc_total) < 0.9 else ("no-data" if not vc_total else "normal"),
            })

        yr_q = AttendanceRecord.objects.filter(session__virtual_classroom__in=yr_vcs.values("id"))
        yr_total = yr_q.count()
        yr_present = yr_q.filter(status__in=PRESENT_STATUSES).count()
        attendance_by_year[yr] = {
            "schoolAvg": round(yr_present / yr_total * 100, 1) if yr_total else None,
            "present": yr_present,
            "absent": yr_total - yr_present,
            "total": yr_total,
            "sessions": AttendanceSession.objects.filter(virtual_classroom__in=yr_vcs.values("id")).count(),
            "atRisk": sum(1 for r in yr_rows if r["status"] == "at-risk"),
            "rows": yr_rows,
        }

    sa_attendance = {
        "academicYears": attend_year_list or section_years,
        "byYear": attendance_by_year,
    }

    # ── Options for creating Virtual Classrooms (view-classrooms) ───────────
    # Subject / section / teacher selections for the Create VC modal, sourced
    # from the same tables the registrar's API resolves against. Archived
    # sections are left out: a new classroom cannot be attached to one, and
    # offering it produced a create that silently did nothing.
    option_subjects = [
        {"id": s.id, "name": s.name, "grade": s.grade_level, "type": s.subject_type}
        for s in subject_qs.filter(status=Subject.STATUS_ACTIVE).order_by("grade_level", "name")
    ]
    option_sections = [
        {"id": c.id, "name": c.name, "grade": c.grade_level}
        for c in year_qs.filter(status=Section.STATUS_ACTIVE).order_by("grade_level", "name")
    ]
    option_teachers = [
        {"id": t.id, "name": t.get_full_name()}
        for t in order_teachers(
            User.objects.filter(groups__name="teacher", is_active=True)
            .select_related("employee_profile")
        )[:200]
    ]
    vc_options = {
        "year": (active_year_obj.label if active_year_obj else None),
        "subjects": option_subjects,
        "sections": option_sections,
        "teachers": option_teachers,
        "periods": [
            {"code": "", "label": "N/A — Year-long"},
            {"code": "1ST_TRIMESTER", "label": "1st Trimester"},
            {"code": "2ND_TRIMESTER", "label": "2nd Trimester"},
            {"code": "3RD_TRIMESTER", "label": "3rd Trimester"},
        ],
    }

    dashboard_json = {
        "calendar": _calendar_payload(user),
        "profile": _self_profile(user),
        "directory": directory,
        "students": students_json,
        "assessments": assessment_json,
        "announcements": announcement_json,
        "vcOptions": vc_options,
        "attendance": sa_attendance,
        "stats": {
            "total": _non_archived_users().count(),
            "active": _non_archived_users().exclude(
                status__in=["locked", "pending_verification", "pending_password_setup"]
            ).count(),
            "locked": User.objects.filter(status="locked").count(),
            "pending": User.objects.filter(status__in=pending_statuses).count(),
        },
        "accounts": accounts_json,
        "sessions": sessions_json,
        "locked": locked_json,
        "sessionCount": live_count,
        "sessionTotal": LoginSession.objects.count(),
        "activity": activity_json,
        "health": health,
        "services": _service_checks(),
        "alerts": _system_alerts(cfg, pending_count, security_stats["locked"], security_stats["unassigned"]),
        "online": online_json,
        "notifs": notifs_json,
        "recentLogins": recent_logins,
        "securityStats": security_stats,
        "backupHistory": backup_history,
        "backupSettings": backup_settings,
        "templates": [
            {
                "id": t.id,
                "name": t.name,
                "form": t.form_type or "—",
                "active": bool(t.is_active),
                "at": timezone.localtime(t.uploaded_at).strftime("%b %d, %Y") if t.uploaded_at else "—",
                "download": reverse("analytics:sf_template_download", args=[t.id]),
            }
            for t in SFReportTemplate.objects.select_related("uploaded_by").order_by("name")
        ],
        "reports": [
            {
                "id": r.id,
                "type": r.report_type or "Report",
                "year": getattr(r.academic_year, "label", "") or "—",
                "scope": r.scope_description or "—",
                "at": timezone.localtime(r.generated_at).strftime("%b %d, %Y · %I:%M %p") if r.generated_at else "—",
                "by": (r.generated_by.get_full_name() if r.generated_by else "System"),
            }
            for r in GeneratedReport.objects.select_related("academic_year", "generated_by").order_by("-generated_at")[:20]
        ],
        "userDist": [
            {"label": "Students", "value": role_counts["student"]},
            {"label": "Teachers", "value": role_counts["teacher"]},
            {"label": "Registrars", "value": role_counts["registrar"]},
            {"label": "Principals", "value": role_counts["principal"] + role_counts["assistant_principal"] + role_counts["elementary_principal"]},
            {"label": "Quality Assurance", "value": role_counts["quality_audit"]},
            {"label": "System Admins", "value": role_counts["system_admin"]},
        ],
        # ── Roles & Permissions (view-roles) ──────────────────────────────
        # Drives the searchable role manager and the permission matrix. Both
        # read real database state, so the System Administrator can grant or
        # revoke a role without a developer, and the change lands in
        # RoleAssignment (which syncs the Django Group via the post_save signal).
        "assignableRoles": [
            {"code": code, "label": label}
            for code, label in Role.Code.choices
        ],
        "roleDirectory": [
            {
                "id": a["id"],
                "name": a["name"],
                "emp": a["emp"],
                "email": a["email"],
                # accounts_json is a plain dict of primitives; it has no "user"
                # key, so a["user"] here raised KeyError and broke the page.
                "initials": a["initials"],
                "gradient": a["gradient"],
                "status": a["status"],
                "last": a["last"],
                "role": a["role"],
                "roles": a["roles"],
                "role_codes": a["role_codes"],
                "isSelf": a["id"] == user.pk,
            }
            for a in accounts_json
        ],
        "roleMatrix": _role_matrix_payload(),
        # Age/Sex panel over the staff accounts. Students are the Registrar's
        # concern; the ICT dashboard shows the people it actually manages.
        "demographics": {
            "kind": "employee",
            "label": "Staff",
            "rows": _employee_demo_rows(user_accounts),
        },
        # Learner counterpart to the staff panel above. The ICT view also ships
        # a Student Demographics panel so the System Administrator can see the
        # school-wide Age/Sex spread without leaving the dashboard.
        "studentDemographics": {
            "kind": "student",
            "label": "Learner",
            "rows": _student_demo_rows(),
        },
    }

    return {
        "stats": stats,
        "total_users": _non_archived_users().count(),
        "active_users": _non_archived_users().exclude(
            status__in=["locked", "pending_verification", "pending_password_setup"]
        ).count(),
        "locked_users": User.objects.filter(status="locked").count(),
        "pending_total": (
            User.objects.filter(status__in=pending_statuses).count()
        ),
        "active_sessions_count": LoginSession.objects.count(),
        "backup_count": BackupRecord.objects.count(),
        "pending_review_count": RoleAccessReview.objects.filter(completed_at__isnull=True).count(),
        "pending_users": pending_users,
        "recent_backups": BackupRecord.objects.order_by("-created_at")[:5],
        "recent_activity": ActivityLog.objects.select_related("user").order_by("-timestamp")[:8],
        "academic_years": AcademicYear.objects.order_by("-start_date").values_list("label", flat=True)[:5] or [],
        "active_sessions": active_sessions,
        "role_counts": role_counts,
        "locked_accounts": locked_accounts,
        "user_accounts": user_accounts,
        "user_dist": [
            {"label": "Students", "value": role_counts["student"]},
            {"label": "Teachers", "value": role_counts["teacher"]},
            {"label": "Registrars", "value": role_counts["registrar"]},
            {"label": "System Admins", "value": role_counts["system_admin"]},
            {"label": "Others", "value": role_counts["principal"] + role_counts["assistant_principal"] + role_counts["quality_audit"]},
        ],
        "dashboard_json": dashboard_json,
    }


def registrar_context(user):
    active_year = _active_academic_year()
    current = active_year.id if active_year else None
    enrolled = VirtualClassroomEnrollment.objects.filter(status=VirtualClassroomEnrollment.STATUS_ACTIVE)
    if current:
        enrolled = enrolled.filter(virtual_classroom__academic_year_id=current)
    sections_qs = Section.objects.filter(status=Section.STATUS_ACTIVE)
    subjects_qs = Subject.objects.filter(status=Subject.STATUS_ACTIVE)
    vcs_qs = VirtualClassroom.objects.filter(status=STATUS_ACTIVE)
    if current:
        sections_qs = sections_qs.filter(academic_year_id=current)
        subjects_qs = subjects_qs.filter(academic_year_id=current)
        vcs_qs = vcs_qs.filter(academic_year_id=current)
    stats = [
        _stat("Students", _role_user_count("student"), "fa-user-graduate"),
        _stat("Enrolled This Year", enrolled.count(), "fa-user-check"),
        _stat("Active Sections", sections_qs.count(), "fa-users-between-lines"),
        _stat("Active Subjects", subjects_qs.count(), "fa-book-open"),
        _stat("Virtual Classrooms", vcs_qs.count(), "fa-chalkboard-user"),
        _stat("Archived Students", User.objects.filter(groups__name="student", status="archived").count(), "fa-box-archive"),
    ]
    recent_enrollments = VirtualClassroomEnrollment.objects.select_related(
        "student__user", "virtual_classroom__section", "virtual_classroom__subject"
    ).order_by("-enrolled_at")
    if current:
        recent_enrollments = recent_enrollments.filter(virtual_classroom__academic_year_id=current)
    recent_enrollments = recent_enrollments[:6]

    # ── Real-data payload for the new front-end dashboard ─────────────────
    ay_label = ""
    if active_year:
        ay_label = active_year.label
    year_label = "S.Y. {}".format(ay_label.replace("-", "–")) if ay_label else ""

    # ── Final grades (SF1 School Register) ─────────────────────────────────
    # StudentGrade rows are written when a grading formula runs. SF1 averages
    # them into the learner's general average and derives Promoted/Retained
    # from it; with no grading run yet the list stays empty and the form keeps
    # printing "—" instead of a made-up mark.
    final_grades_by_student = {}
    if current:
        for grade_row in StudentGrade.objects.filter(
            academic_year_id=current
        ).iterator():
            final_grades_by_student.setdefault(grade_row.student_id, []).append(
                float(grade_row.grade_value)
            )

    students_json = []
    for prof in order_students(
        StudentProfile.objects.select_related("user").prefetch_related(
            "enrollments__section"
        )
    ):
        sec_disp = prof.section_display
        grade = ""
        section = ""
        if sec_disp and " - " in sec_disp:
            grade = "Grade " + sec_disp.split(" - ")[0].replace("Grade ", "")
            section = sec_disp.split(" - ", 1)[1]
        u_email = prof.user.email if prof.user else ""
        account = "Generated" if prof.user and prof.user.status == "active" else (
            "Pending" if prof.user else "Generated"
        )
        students_json.append(
            {
                "last": prof.last_name,
                "first": prof.first_name,
                "middle": (prof.middle_initial or "").rstrip(".") or "",
                "lrn": prof.lrn,
                "gender": "Male" if prof.sex == "M" else ("Female" if prof.sex == "F" else ""),
                "birth": prof.birthdate.isoformat() if prof.birthdate else "",
                "contact": prof.contact_number or "",
                "grade": grade or "",
                "section": section or "—",
                "guardian": prof.guardian_name or "",
                "email": u_email,
                "status": "Enrolled",
                "account": account,
                "acctUsername": u_email,
                "tempPass": "",
                "accountVerified": False,
                "finalGrades": final_grades_by_student.get(prof.pk, []),
            }
        )

    sections_json = []
    for sec in sections_qs.select_related("adviser").order_by("grade_level"):
        advance = getattr(sec, "advised_sections", None)
        adviser_name = sec.adviser.get_full_name() if getattr(sec, "adviser", None) else ""
        if not adviser_name and advance is not None:
            adv = sec.advised_sections.select_related("user", "employee_profile").first()
            adviser_name = adv.user.get_full_name() if adv and adv.user else ""
        sections_json.append(
            {
                "ay": "S.Y. {}".format((sec.academic_year.label.replace("-", "–")) if sec.academic_year else ""),
                "grade": "Grade {}".format(sec.grade_level),
                "name": sec.name,
                "adviser": adviser_name or "—",
                "status": "Active" if sec.status == Section.STATUS_ACTIVE else "Archived",
            }
        )

    subjects_json = []
    for subj in subjects_qs.select_related("academic_year").order_by("grade_level", "name"):
        subjects_json.append(
            {
                "name": subj.name,
"grade": "Grade {}".format(subj.grade_level) if subj.grade_level else "",
                "type": "Applied" if getattr(subj, "subject_type", "") in ("CORE", "APPLIED", "core", "applied") else "Elective",
                "period": subj.grading_period or "Whole Academic Year",
                "status": "Active" if subj.status == Subject.STATUS_ACTIVE else "Archived",
                "ay": "S.Y. {}".format((subj.academic_year.label.replace("-", "–")) if subj.academic_year else ""),
            }
        )

    # Active rosters per classroom, as LRNs. The Add-Student modal reads this to
    # exclude students who are already enrolled; leaving it empty made every
    # classroom look unenrolled and hid the roster from renderVCRoster().
    vc_lrns = {}
    for vc_id, lrn in VirtualClassroomEnrollment.objects.filter(
        virtual_classroom__in=vcs_qs,
        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).values_list("virtual_classroom_id", "student__lrn"):
        if lrn:
            vc_lrns.setdefault(vc_id, []).append(lrn)

    vcs_json = []
    for vc in vcs_qs.select_related("subject", "section", "teacher", "academic_year").order_by("subject__name")[:200]:
        vcs_json.append(
            {
                "id": vc.id,
                "subject": vc.subject.name if vc.subject else "",
                "grade": "Grade {}".format(vc.section.grade_level) if vc.section else "",
                "section": vc.section.name if vc.section else "",
                "type": "Applied",
                "gradingPeriod": vc.grading_period or "Quarter",
                "teacher": vc.teacher.get_full_name() if vc.teacher else "—",
                "schedule": vc.schedule or "",
                "ay": "S.Y. {}".format((vc.academic_year.label.replace("-", "–")) if vc.academic_year else ""),
                "students": vc_lrns.get(vc.id, []),
                "status": "Active" if vc.status == STATUS_ACTIVE else "Archived",
            }
        )

    acad_years_json = []
    for ay in AcademicYear.objects.order_by("-start_date"):
        acad_years_json.append(
            {
                "label": "S.Y. {}".format(ay.label.replace("-", "–")),
                "start": ay.start_date.isoformat() if ay.start_date else "",
                "end": ay.end_date.isoformat() if ay.end_date else "",
                "status": "Active" if ay.status == "ACTIVE" else "Archived",
            }
        )

    logs_json = []
    for enr in VirtualClassroomEnrollment.objects.select_related(
        "student", "virtual_classroom", "virtual_classroom__section", "virtual_classroom__subject"
    ).order_by("-enrolled_at")[:10]:
        prof = enr.student
        if prof is None:
            continue
        logs_json.append(
            {
                "action": "Enrolled student",
                "detail": "{} {} enrolled to {} - {}".format(
                    prof.first_name,
                    prof.last_name,
                    getattr(enr.virtual_classroom.subject, "name", "") if enr.virtual_classroom else "",
                    getattr(enr.virtual_classroom.section, "name", "") if enr.virtual_classroom else "",
                ),
                "time": timezone.localtime(enr.enrolled_at).strftime("%b %d, %Y, %I:%M %p"),
            }
        )
    for ay in AcademicYear.objects.order_by("-start_date")[:3]:
        logs_json.append(
            {
                "action": "Updated Academic Structure",
                "detail": "Academic Year {} configured".format(ay.label),
                "time": timezone.localtime(ay.created_at).strftime("%b %d, %Y, %I:%M %p") if hasattr(ay, "created_at") else "—",
            }
        )

    grade_counts = {}
    for s in students_json:
        grade_counts[s["grade"]] = grade_counts.get(s["grade"], 0) + 1

    # ── Attendance Overview (view-attendance) + attendance-backed reports ──
    # One row per learner x subject x calendar month for the active school
    # year, which is exactly the shape renderAttendance(), openSF2() and
    # generateAtRiskReport() read: P / L / A / E counts plus the LRN so the
    # roster can be joined back on the client. Nothing is invented — a month
    # with no recorded session simply never appears.
    att_qs = AttendanceRecord.objects.select_related(
        "student", "session__virtual_classroom__subject"
    )
    if current:
        att_qs = att_qs.filter(session__virtual_classroom__academic_year_id=current)
    attendance_bucket = {}
    for rec in att_qs.iterator():
        prof = rec.student
        session = rec.session
        if prof is None or session is None:
            continue
        vc = session.virtual_classroom
        subject = (
            vc.subject.name
            if vc is not None and vc.subject_id and getattr(vc, "subject", None)
            else "—"
        )
        session_date = getattr(session, "session_date", None)
        month = session_date.strftime("%B") if session_date else ""
        key = (prof.pk, subject, month)
        counts = attendance_bucket.get(key)
        if counts is None:
            name = " ".join(
                p
                for p in (prof.first_name, prof.middle_initial, prof.last_name)
                if p
            ).strip() or (prof.user.get_full_name() if prof.user_id else "")
            counts = attendance_bucket[key] = {
                "name": name,
                "lrn": prof.lrn or "",
                "subject": subject,
                "month": month,
                "present": 0,
                "late": 0,
                "absent": 0,
                "excused": 0,
            }
        if rec.status == AttendanceRecord.PRESENT_ON_TIME:
            counts["present"] += 1
        elif rec.status == AttendanceRecord.PRESENT_LATE:
            counts["late"] += 1
        elif rec.status == AttendanceRecord.ABSENT_EXCUSED:
            counts["excused"] += 1
        else:
            counts["absent"] += 1
    attendance_rows = sorted(
        attendance_bucket.values(),
        key=lambda r: (r["name"].lower(), r["subject"], r["month"]),
    )

    dashboard_json = {
        "profile": _self_profile(user),
        "calendar": _calendar_payload(user),
        "yearLabel": ("S.Y. {}".format(active_year.label)) if active_year else "",
        "stats": {
            "students": students_json.__len__(),
            "sections": sections_json.__len__(),
            "subjects": subjects_json.__len__(),
            "vcs": vcs_json.__len__(),
            "teachers": _role_user_count("teacher"),
            "accounted": sum(1 for s in students_json if s["account"] == "Generated"),
            "pending": sum(1 for s in students_json if s["account"] == "Pending"),
        },
        "gradeCounts": grade_counts,
        "students": students_json,
        # Age/Sex panel over the same learner rows the Registrar edits, so the
        # demographics view and the student list can never disagree.
        "demographics": {
            "kind": "student",
            "label": "Learner",
            "rows": [
                _demo_row(
                    name=" ".join(
                        filter(
                            None,
                            [s["first"], s["middle"], s["last"]],
                        )
                    ),
                    code=s["lrn"],
                    gender=s["gender"],
                    birth=s["birth"],
                    group=" – ".join(
                        p
                        for p in (
                            s["grade"],
                            "" if s["section"] == "—" else s["section"],
                        )
                        if p
                    ),
                    meta=s["status"],
                )
                    for s in students_json
            ],
        },
        # Same row shape as the learner panel, so the shared renderer draws the
        # registrar's Staff panel exactly like the Principal's / ICT's.
        "staffDemographics": {
            "kind": "employee",
            "label": "Staff",
            "rows": _all_staff_demo_rows(),
        },
        # Read-only P/L/A/E monitoring data behind Attendance Overview, SF2,
        # Attendance Summary and the At-Risk Students report.
        "attendance": attendance_rows,
        "sections": sections_json,
        "subjects": subjects_json,
        "virtualClassrooms": vcs_json,
        "academicYears": acad_years_json,
        "logs": logs_json,
    }

    return {
        "stats": stats,
        "active_year": active_year,
        "recent_enrollments": recent_enrollments,
        "section_count": sections_qs.count(),
        "subject_count": subjects_qs.count(),
        "teacher_count": _role_user_count("teacher"),
        "lms_student_accounts": _role_user_count("student"),
        "pending_accounts": User.objects.filter(
            groups__name="student",
            status__in=("pending_verification", "pending_password_setup"),
        ).count(),
        "archived_students": User.objects.filter(
            groups__name="student", status="archived"
        ).count(),
        "dashboard_json": dashboard_json,
    }


def _student_display_name(prof):
    return (
        " ".join(filter(None, [prof.first_name, prof.last_name]))
        or (prof.user.get_full_name() if prof.user_id else prof.lrn or "Student")
    )


def _dt(v):
    """Local-time 'YYYY-MM-DD HH:MM' or None. Module level because the report
    builder needs it too, and teacher_context used to carry a local copy."""
    return timezone.localtime(v).strftime("%Y-%m-%d %H:%M") if v else None


# Compiled `AssessmentPart.question_type` -> the short code the teacher
# frontend's assessment builder speaks. `numeric` has no builder type, so it
# degrades to a short answer (its `accepted_answers` is still editable there).
_BUILDER_QTYPE = {
    AssessmentPart.TYPE_MULTIPLE_CHOICE: "mc",
    AssessmentPart.TYPE_TRUE_FALSE: "tf",
    AssessmentPart.TYPE_MATCHING: "mt",
    AssessmentPart.TYPE_IDENTIFICATION: "id",
    AssessmentPart.TYPE_SHORT_ANSWER: "sa",
    AssessmentPart.TYPE_NUMERIC: "sa",
    AssessmentPart.TYPE_ESSAY: "es",
    AssessmentPart.TYPE_POSITION_PAPER: "pp",
    AssessmentPart.TYPE_CASE_STUDY: "cs",
    AssessmentPart.TYPE_NARRATIVE_REPORT: "nr",
}
_BUILDER_SUBJECTIVE = {"es", "pp", "cs", "nr", "pt"}
_BUILDER_ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]


def _assessment_builder_payload(assessment):
    """Rebuild the teacher builder's `parts`/`questions` from stored parts.

    `teacher_context` exposes assessment *metadata* only, so a dashboard refresh
    used to leave the builder with nothing to repopulate and editing an
    assessment reopened an empty form. This walks the compiled
    `AssessmentPart.items` banks and reshapes them back into the flat question
    objects the builder works in. Purely additive: anything the reshape cannot
    express (matching pairs kept only as answer keys, rubric lists) degrades to
    a best-effort text value instead of dropping the question.
    """
    parts_json = []
    questions_json = []
    has_manual = False
    # `.all()` (not `.order_by()`) reuses the prefetched cache; AssessmentPart's
    # Meta.ordering is already ["order", "id"].
    parts = list(assessment.parts.all())
    for idx, part in enumerate(parts):
        qtype = _BUILDER_QTYPE.get(part.question_type, "mc")
        mode = "subjective" if qtype in _BUILDER_SUBJECTIVE else "objective"
        if mode == "subjective":
            has_manual = True
        part_id = "p{}".format(part.id)
        kind = "Subjective" if mode == "subjective" else "Objective"
        label = part.question_text or "Part {} \u2013 {}".format(
            _BUILDER_ROMAN[idx] if idx < len(_BUILDER_ROMAN) else idx + 1, kind
        )
        parts_json.append({"id": part_id, "label": label, "mode": mode, "qtype": qtype})
        items = part.items if isinstance(part.items, dict) else {}

        def _item_order(key):
            tail = str(key).rsplit("_", 1)[-1]
            return int(tail) if tail.isdigit() else 0

        for item_key in sorted(items, key=_item_order):
            item = items[item_key]
            if not isinstance(item, dict):
                continue
            q = {
                "type": qtype,
                "mode": mode,
                "partId": part_id,
                "itemKey": item_key,
                "text": item.get("question", ""),
                "points": item.get("point", 0),
                "media": ({
                    "kind": "image",
                    "name": "Question image",
                    "data": item.get("image", ""),
                } if str(item.get("image", "")).startswith("data:image/") else None),
                "optImgs": {},
            }
            if qtype == "mc":
                options = [str(c) for c in (item.get("choices") or [])]
                q["options"] = options
                for choice_index, image in enumerate(item.get("choice_images") or []):
                    if image and choice_index < 4 and str(image).startswith("data:image/"):
                        q["optImgs"][chr(65 + choice_index)] = image
                try:
                    q["correct"] = options.index(str(item.get("correct_answer", "")))
                except ValueError:
                    q["correct"] = 0
            elif qtype == "tf":
                q["tf"] = str(item.get("correct_answer", "True")) != "False"
            elif qtype == "id":
                answers = item.get("accepted_answers") or []
                q["idAns"] = str(answers[0]) if answers else str(item.get("correct_answer", ""))
            elif qtype == "sa":
                answers = item.get("accepted_answers") or []
                q["saAns"] = str(answers[0]) if answers else str(item.get("correct_answer", ""))
            elif qtype == "mt":
                matches = item.get("correct_matches") or {}
                if isinstance(matches, dict):
                    q["match"] = "; ".join("{} -> {}".format(k, v) for k, v in matches.items())
                else:
                    q["match"] = str(matches)
            else:
                rubric = item.get("grading_rubric", "")
                if isinstance(rubric, (list, tuple)):
                    rubric = "\n".join(str(r) for r in rubric)
                q["rubric"] = rubric
            questions_json.append(q)
    return parts_json, questions_json, has_manual


def _vc_label(vc):
    """Display identity of a virtual classroom.

    Sections repeat across subjects in this school (English, Filipino and
    Mathematics can all sit in "St. Teresa of Avilla"), so the section name on
    its own does not identify the classroom. Subject plus section does.
    """
    subject = vc.subject.name if vc.subject_id else ""
    section = vc.section.name if vc.section_id else ""
    if subject and section:
        return "{} · {}".format(subject, section)
    return section or subject or "Virtual Classroom"


# Thresholds shared by the At-Risk report and the analytics banner. Kept here so
# the dashboard banner and the exported report can never drift apart.
ATT_RISK_ATTENDANCE_PCT = 75
ATT_RISK_SCORE_PCT = 60


def _teacher_report_data(my_classes):
    """Per-student metrics backing the six Analytics & Reports tiles.

    Everything else in `teacher_context` is an aggregate (rates, totals, chart
    series), but these reports are inherently per-student, so they need their
    own query set rather than a reshuffle of the existing one.

    Returns::

        {
          "assessments": [{"id", "title", "classroom", "deadline"}],
          "students": [{
              "id", "name", "lrn", "classroom", "classroom_id",
              "att":   {"present", "late", "absent", "excused", "total", "pct"},
              "score": {"avg", "graded", "by_assessment": {title: pct}},
              "sub":   {"submitted", "late", "missing", "expected", "rate"},
              "risk":  {"at_risk", "reasons": [...]},
          }],
        }

    `att` counts excused absences separately from unexcused ones but rolls both
    into `absent`, matching how AttendanceRecord models them: an excused day is
    still a day the student was not present for the lesson.
    """
    if not my_classes:
        return {"assessments": [], "students": []}

    vc_ids = [vc.id for vc in my_classes]
    vc_by_id = {vc.id: vc for vc in my_classes}

    assigns = list(
        AssessmentAssignment.objects.filter(virtual_classroom_id__in=vc_ids)
        .select_related("assessment", "virtual_classroom")
        .order_by("virtual_classroom__subject__name", "assessment__title")
    )
    possible_map = {a.id: (a.assessment.total_possible_score or 0) for a in assigns}

    # Roster: every actively enrolled student, so a student with no attendance
    # and no submissions still appears (with zeros) instead of vanishing.
    enrollments = list(
        VirtualClassroomEnrollment.objects.filter(
            virtual_classroom_id__in=vc_ids,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).select_related("student", "virtual_classroom")
    )
    # expected submissions per assignment, used to score "missing".
    enr_per_vc = {}
    students = {}
    for en in enrollments:
        vc = en.virtual_classroom
        enr_per_vc[vc.id] = enr_per_vc.get(vc.id, 0) + 1
        prof = en.student
        sid = str(prof.pk)
        vc_name = _vc_label(vc)
        row = students.setdefault(
            sid,
            {
                "id": sid,
                "name": _student_display_name(prof),
                "lrn": prof.lrn or "",
                "classroom": vc_name,
                "classroom_id": vc.id,
                # A student can be enrolled in several of the teacher's
                # classrooms; expected submissions and scores must aggregate over
                # all of them, not just whichever row we happened to see first.
                "classroom_ids": [],
                "classrooms": [],
                "att": {
                    "present": 0, "late": 0, "absent": 0,
                    "excused": 0, "total": 0, "pct": 0,
                },
                "score": {"avg": 0, "graded": 0, "by_assessment": {}},
                "sub": {
                    "submitted": 0, "late": 0, "missing": 0,
                    "expected": 0, "rate": 0,
                },
                "risk": {"at_risk": False, "reasons": []},
            },
        )
        if vc.id not in row["classroom_ids"]:
            row["classroom_ids"].append(vc.id)
        if vc_name and vc_name not in row["classrooms"]:
            row["classrooms"].append(vc_name)
        row["classroom_count"] = len(row["classrooms"])

    # A teacher can hold a dozen classrooms, so an untruncated join would make
    # the "Class" cell in every report table unreadable. Show two, then a count.
    for row in students.values():
        rooms = row["classrooms"]
        if len(rooms) > 2:
            row["classroom"] = "{}, and {} more".format(
                ", ".join(rooms[:2]), len(rooms) - 2
            )
        else:
            row["classroom"] = ", ".join(rooms)

    # ---- Attendance -------------------------------------------------------
    att_rows = (
        AttendanceRecord.objects.filter(
            session__virtual_classroom_id__in=vc_ids
        )
        .values_list("student_id", "status")
    )
    for sid, status in att_rows:
        row = students.get(str(sid))
        if row is None:
            continue
        att = row["att"]
        att["total"] += 1
        if status == AttendanceRecord.PRESENT_ON_TIME:
            att["present"] += 1
        elif status == AttendanceRecord.PRESENT_LATE:
            att["late"] += 1
            att["present"] += 1
        elif status == AttendanceRecord.ABSENT_EXCUSED:
            att["absent"] += 1
            att["excused"] += 1
        else:
            att["absent"] += 1

    # ---- Submissions and scores ------------------------------------------
    assigns_per_vc = {}
    for a in assigns:
        assigns_per_vc.setdefault(a.virtual_classroom_id, []).append(a)

    for row in students.values():
        row["sub"]["expected"] = sum(
            len(assigns_per_vc.get(vc_id, [])) for vc_id in row["classroom_ids"]
        )

    # Two classes can both use the title "Quiz 1"; keying the per-assessment
    # score map by bare title would silently drop one of them.
    title_counts = {}
    for a in assigns:
        title_counts[a.assessment.title or "Assessment"] = (
            title_counts.get(a.assessment.title or "Assessment", 0) + 1
        )
    label_map = {}
    used_labels = {}
    for a in assigns:
        title = a.assessment.title or "Assessment"
        label = title
        if title_counts[title] > 1:
            label = "{} ({})".format(title, _vc_label(a.virtual_classroom))
        if label in used_labels:
            used_labels[label] += 1
            label = "{} #{}".format(label, used_labels[label])
        else:
            used_labels[label] = 1
        label_map[a.id] = label

    subs = Submission.objects.filter(
        assessment_assignment__virtual_classroom_id__in=vc_ids
    ).values_list(
        "student_id", "assessment_assignment_id", "is_late", "total_score"
    )
    for sid, aa_id, is_late, total_score in subs:
        row = students.get(str(sid))
        if row is None:
            # Submission from a class the teacher no longer has open, or from a
            # student who has since been unenrolled; not part of this report.
            continue
        if aa_id not in possible_map:
            continue
        row["sub"]["submitted"] += 1
        if is_late:
            row["sub"]["late"] += 1
        if total_score is not None:
            possible = possible_map.get(aa_id) or 0
            if possible > 0:
                pct = float(total_score) * 100.0 / float(possible)
                row["score"]["by_assessment"][label_map[aa_id]] = round(pct, 1)

    # ---- Derive percentages, risk flags ----------------------------------
    for row in students.values():
        att, sc, sub, risk = row["att"], row["score"], row["sub"], row["risk"]

        att["pct"] = round(att["present"] * 100 / att["total"]) if att["total"] else 0

        pcts = list(sc["by_assessment"].values())
        sc["graded"] = len(pcts)
        sc["avg"] = round(sum(pcts) / len(pcts), 1) if pcts else 0

        # "missing" is expected minus what actually arrived.
        sub["missing"] = max(0, sub["expected"] - sub["submitted"])
        if sub["expected"]:
            # Plain submission rate. Late work still counts as submitted; it is
            # reported in its own column rather than being hidden inside the
            # rate, which previously made one late paper look like a no-show.
            sub["rate"] = round(sub["submitted"] * 100 / sub["expected"])

        # Criteria must stay in step with the banner on the Analytics tab
        # ("attendance below 75% or average score below 60%"): a missing
        # submission on its own is not an at-risk flag.
        if att["total"] and att["pct"] < ATT_RISK_ATTENDANCE_PCT:
            risk["reasons"].append(
                "Attendance {}% (below {}%)".format(att["pct"], ATT_RISK_ATTENDANCE_PCT)
            )
        if sc["graded"] and sc["avg"] < ATT_RISK_SCORE_PCT:
            risk["reasons"].append(
                "Average score {}% (below {}%)".format(sc["avg"], ATT_RISK_SCORE_PCT)
            )
        risk["at_risk"] = bool(risk["reasons"])

    ordered = sorted(
        students.values(), key=lambda r: (-(r["risk"]["at_risk"]), r["name"])
    )
    for r in ordered:
        # "classroom_ids"/"classrooms" are only needed while aggregating.
        r.pop("classroom_ids", None)
        r.pop("classrooms", None)
    return {
        # The report's scope. Every report below aggregates across all of the
        # teacher's active classrooms, so the UI has to name them explicitly
        # rather than the old "Assigned Virtual Classroom" placeholder.
        "classrooms": [
            {
                "id": vc.id,
                "label": _vc_label(vc),
                "name": (vc.section.name if vc.section_id else vc.subject.name)
                or vc.subject.name,
                "subject": vc.subject.name,
                "section": vc.section.name if vc.section_id else "",
                "academic_year": vc.academic_year.label
                if getattr(vc, "academic_year_id", None)
                else "",
                "students": enr_per_vc.get(vc.id, 0),
            }
            for vc in sorted(
                my_classes, key=lambda v: (v.subject.name, v.section.name or "")
            )
        ],
        "assessments": [
            {
                "id": a.id,
                "title": a.assessment.title or "Assessment",
                "classroom": _vc_label(a.virtual_classroom),
                "deadline": _dt(a.assessment.submission_deadline),
                "has_points": possible_map.get(a.id, 0) > 0,
            }
            for a in assigns
        ],
        # Assessment.total_possible_score is the sum of its parts' item points,
        # so an assessment with no parts (or no points on its items) has no
        # scale and cannot be turned into a percentage. The score report says
        # so explicitly instead of looking empty for no visible reason.
        "unscored_assignments": sum(
            1 for a in assigns if possible_map.get(a.id, 0) <= 0
        ),
        "students": ordered,
    }


def _class_counts(vc):
    topics = Topic.objects.filter(virtual_classroom=vc).count()
    resources = LearningResourcePlacement.objects.filter(virtual_classroom=vc).count()
    assessments = (
        AssessmentAssignment.objects.filter(virtual_classroom=vc).values("assessment").distinct().count()
    )
    return topics, resources, assessments


def _empty_analytics():
    return {
        "att": 0, "sub": 0, "avg": 0, "risk": 0,
        "attL": [], "attV": [],
        "brkP": 0, "brkA": 0, "brkL": 0, "brkE": 0,
        "sdL": [b[2] for b in SD_BUCKETS], "sdV": [0] * len(SD_BUCKETS),
        "srL": [], "srV": [],
        "spL": [], "spV": [],
        "dlL": [], "dlV": [],
        "pfL": [], "pfP": [], "pfF": [],
        "chips": [],
    }


def _class_analytics(vcs):
    """Real per-class statistics keyed the same way the teacher dashboard reads them.

    Values are computed directly from attendance records, assessment
    assignments, submissions and resource placements — no fabricated data.
    """
    qs = VirtualClassroom.objects.filter(pk__in=[v.pk for v in vcs])
    data = _empty_analytics()

    # ---- Attendance breakdown & trend ------------------------------------
    recs = AttendanceRecord.objects.filter(session__virtual_classroom__in=qs)
    status_counts = dict(recs.order_by().values_list("status").annotate(c=Count("id")))
    total_rec = sum(status_counts.values())
    present = status_counts.get(AttendanceRecord.PRESENT_ON_TIME, 0) + status_counts.get(
        AttendanceRecord.PRESENT_LATE, 0
    )
    if total_rec:
        data["att"] = round(present * 100 / total_rec)
    data["brkP"] = status_counts.get(AttendanceRecord.PRESENT_ON_TIME, 0)
    data["brkL"] = status_counts.get(AttendanceRecord.PRESENT_LATE, 0)
    data["brkA"] = status_counts.get(AttendanceRecord.ABSENT_UNEXCUSED, 0)
    data["brkE"] = status_counts.get(AttendanceRecord.ABSENT_EXCUSED, 0)
    # The attendance-breakdown doughnut on the teacher dashboard reads brL/brV.
    # Order matches its slice colors: green Present, red Absent, orange Late,
    # blue Excused.
    data["brL"] = ["Present", "Absent", "Late", "Excused"]
    data["brV"] = [
        data["brkP"],
        data["brkA"],
        data["brkL"],
        data["brkE"],
    ]

    day_rows = recs.values("session__session_date").order_by().annotate(
        present=Count("id", filter=Q(status__in=PRESENT_STATUSES)),
        total=Count("id"),
    )
    day_map = {r["session__session_date"]: r for r in day_rows}
    dates = sorted(day_map.keys())[-6:]
    data["attL"] = [d.strftime("%b %d") for d in dates]
    data["attV"] = [
        round(day_map[d]["present"] * 100 / day_map[d]["total"]) if day_map[d]["total"] else 0
        for d in dates
    ]

    # ---- Submissions -----------------------------------------------------
    assigns = list(
        AssessmentAssignment.objects.filter(virtual_classroom__in=qs)
        .select_related("assessment", "virtual_classroom")
    )
    enr_counts = dict(
        VirtualClassroomEnrollment.objects.filter(
            virtual_classroom__in=qs, status=VirtualClassroomEnrollment.STATUS_ACTIVE
        ).values_list("virtual_classroom_id").annotate(c=Count("id"))
    )
    expected = sum(enr_counts.get(a.virtual_classroom_id, 0) for a in assigns)
    subs = Submission.objects.filter(assessment_assignment__in=assigns)
    sub_by_a = dict(
        subs.order_by().values_list("assessment_assignment_id").annotate(c=Count("id"))
    )
    actual = sum(sub_by_a.values())
    if expected:
        data["sub"] = round(actual * 100 / expected)

    for a in assigns[:6]:
        exp_a = enr_counts.get(a.virtual_classroom_id, 0)
        act_a = sub_by_a.get(a.id, 0)
        data["srL"].append((a.assessment.title or "Assessment")[:26])
        data["srV"].append(round(act_a * 100 / exp_a) if exp_a else 0)

    # ---- Scores (graded submissions) -------------------------------------
    possible_map = {a.id: a.assessment.total_possible_score for a in assigns}
    graded = subs.filter(total_score__isnull=False, total_score__gt=0).select_related(
        "student", "assessment_assignment__assessment"
    )
    pct_by_student = {}
    pf_counts = {a.id: [0, 0] for a in assigns}  # [pass, fail]
    score_dist = [0] * len(SD_BUCKETS)
    for s in graded:
        poss = possible_map.get(s.assessment_assignment_id) or 0
        if poss <= 0:
            continue
        pct = float(s.total_score) * 100.0 / float(poss)
        pct_by_student.setdefault(s.student_id, []).append(pct)
        for idx, (lo, hi, _label) in enumerate(SD_BUCKETS):
            if lo <= pct <= hi:
                score_dist[idx] += 1
                break
        if pct >= 60:
            pf_counts[s.assessment_assignment_id][0] += 1
        else:
            pf_counts[s.assessment_assignment_id][1] += 1

    pcts_all = [p for lst in pct_by_student.values() for p in lst]
    if pcts_all:
        data["avg"] = round(sum(pcts_all) / len(pcts_all), 1)
    data["sdV"] = score_dist

    for a in assigns[:6]:
        p, f = pf_counts.get(a.id, [0, 0])
        data["pfL"].append((a.assessment.title or "Assessment")[:26])
        data["pfP"].append(p)
        data["pfF"].append(f)

    # ---- Per-student performance + at-risk flags ---------------------------
    student_avgs = [
        (sid, sum(lst) / len(lst)) for sid, lst in pct_by_student.items() if lst
    ]
    students = StudentProfile.objects.filter(pk__in=[sid for sid, _ in student_avgs])
    name_by_id = {
        sp.pk: (
            " ".join(filter(None, [sp.first_name, sp.last_name]))
            or (sp.user.get_full_name() if sp.user else sp.lrn or "Student")
        )
        for sp in students
    }
    student_avgs.sort(key=lambda x: x[1], reverse=True)
    for sid, avg in student_avgs[:6]:
        data["spL"].append(name_by_id.get(sid, "Student"))
        data["spV"].append(round(avg, 1))

    risk_rows = [x for x in student_avgs if x[1] < 60]
    data["risk"] = len(risk_rows)
    data["chips"] = [
        "{} · {}%".format(name_by_id.get(sid, "Student"), round(avg))
        for sid, avg in risk_rows[:8]
    ]

    # ---- Download activity (resource placements per month) ----------------
    month_counts = {}
    placements = LearningResourcePlacement.objects.filter(virtual_classroom__in=qs)
    for row in placements.values("placed_at").order_by("placed_at"):
        key = (row["placed_at"].year, row["placed_at"].month)
        month_counts[key] = month_counts.get(key, 0) + 1
    last_keys = sorted(month_counts.keys())[-6:]
    data["dlL"] = [datetime.date(y, m, 1).strftime("%b") for y, m in last_keys]
    data["dlV"] = [month_counts[k] for k in last_keys]

    return data


def _series(labels, values):
    return {"labels": list(labels), "values": list(values)}


def _school_chart_payload(scopes=None):
    """School-wide chart series for the Principal and Coordinator dashboards.

    Both frontends already declare the shapes they expect above ``const
    prinCharts`` / ``const acCharts`` and read them off ``data.charts``; this
    supplies that key. Every series is a plain aggregate of stored rows, so an
    empty table produces empty labels and the page falls back to its empty state
    instead of drawing a placeholder.

    ``scopes`` are the caller's campus grade bands. A campus principal must
    never be shown the whole-school aggregate, so every series is derived from
    the classrooms they may read (and, through those, their attendance,
    submissions, resources and assessments). ``None``/empty means whole school.

    Per-classroom bars are capped at TOP_CLASSES. One bar per classroom is
    unreadable at this size, so the attendance view keeps the weakest classes
    (the actionable signal for an oversight dashboard) and the score view the
    strongest. A class with no submissions or no attendance is omitted rather
    than drawn as a zero bar.
    """
    TOP_CLASSES = 12
    WHOLE_YEAR = "Whole Year"

    classrooms = {
        vc.pk: vc
        for vc in campus.virtual_classrooms_for_campus(
            VirtualClassroom.objects.filter(status=STATUS_ACTIVE).select_related(
                "section", "subject", "teacher__employee_profile"
            ),
            scopes,
        )
    }
    vc_ids = list(classrooms)
    if not scopes:
        scopes = None

    # ---- attendance ------------------------------------------------------
    day_rows = list(
        AttendanceRecord.objects.filter(session__virtual_classroom__in=vc_ids)
        .values("session__session_date")
        .order_by()
        .annotate(
            total=Count("id"),
            present=Count("id", filter=Q(status__in=PRESENT_STATUSES)),
        )
        .order_by("session__session_date")
    )[-6:]
    by_period = _series(
        [row["session__session_date"].strftime("%b %d") for row in day_rows],
        [
            round(row["present"] * 100 / row["total"]) if row["total"] else 0
            for row in day_rows
        ],
    )

    status_counts = dict(
        AttendanceRecord.objects.filter(session__virtual_classroom__in=vc_ids)
        .order_by().values_list("status").annotate(c=Count("id"))
    )
    breakdown = _series(
        ["Present On Time", "Present Late", "Absent Excused", "Absent Unexcused"],
        [
            status_counts.get(AttendanceRecord.PRESENT_ON_TIME, 0),
            status_counts.get(AttendanceRecord.PRESENT_LATE, 0),
            status_counts.get(AttendanceRecord.ABSENT_EXCUSED, 0),
            status_counts.get(AttendanceRecord.ABSENT_UNEXCUSED, 0),
        ],
    )

    class_rates = []
    for row in (
        AttendanceRecord.objects.values("session__virtual_classroom_id")
        .order_by()
        .annotate(
            total=Count("id"),
            present=Count("id", filter=Q(status__in=PRESENT_STATUSES)),
        )
    ):
        classroom = classrooms.get(row["session__virtual_classroom_id"])
        if classroom is None or not row["total"]:
            continue
        class_rates.append(
            (
                f"{classroom.section.name} - {classroom.subject.name}",
                round(row["present"] * 100 / row["total"]),
            )
        )
    class_rates.sort(key=lambda item: item[1])
    attendance_by_class = _series(
        [name for name, _ in class_rates[:TOP_CLASSES]],
        [value for _, value in class_rates[:TOP_CLASSES]],
    )

    # ---- scores ----------------------------------------------------------
    # total_possible_score is a property that walks parts, so the denominators
    # are summed once here rather than queried per assignment.
    possible = {
        row["assessment_id"]: row["total"]
        for row in AssessmentPart.objects.values("assessment_id").annotate(total=Sum("points"))
    }
    assignment_map = {
        pk: (vc_id, assessment_id)
        for pk, vc_id, assessment_id in AssessmentAssignment.objects.values_list(
            "id", "virtual_classroom_id", "assessment_id"
        )
    }

    score_by_vc = {}
    score_by_grade = {}
    score_by_period = {}
    pass_fail = {}
    for row in Submission.objects.filter(
        total_score__isnull=False, total_score__gt=0
    ).values_list("assessment_assignment_id", "total_score"):
        assignment_id, score = row
        target = assignment_map.get(assignment_id)
        if target is None:
            continue
        vc_id, assessment_id = target
        classroom = classrooms.get(vc_id)
        denominator = possible.get(assessment_id) or 0
        if classroom is None or not denominator:
            continue
        pct = float(score) * 100.0 / float(denominator)
        score_by_vc.setdefault(vc_id, []).append(pct)
        score_by_grade.setdefault(classroom.section.grade_level, []).append(pct)
        period = classroom.grading_period or WHOLE_YEAR
        score_by_period.setdefault(period, []).append(pct)
        bucket = pass_fail.setdefault((classroom, assessment_id), [0, 0])
        bucket[0 if pct >= 60 else 1] += 1

    def _averages(buckets):
        return {
            key: round(sum(values) / len(values), 1)
            for key, values in buckets.items()
            if values
        }

    vc_scores = _averages(score_by_vc)
    ranked = sorted(vc_scores.items(), key=lambda item: item[1], reverse=True)
    score_by_class = _series(
        [f"{classrooms[vc_id].section.name} - {classrooms[vc_id].subject.name}"
         for vc_id, _ in ranked[:TOP_CLASSES]],
        [value for _, value in ranked[:TOP_CLASSES]],
    )

    grade_scores = _averages(score_by_grade)
    average_by_grade = _series(
        [f"Grade {level}" for level in sorted(grade_scores)],
        [grade_scores[level] for level in sorted(grade_scores)],
    )

    # Prefer official report-card grades, grouped by their stored grading
    # period. Assessment scores remain the fallback where no report-card grades
    # have been computed yet.
    grade_rows = StudentGrade.objects.filter(
        subject__in=campus.subjects_for_campus(Subject.objects.all(), scopes),
        grade_value__isnull=False,
    )
    grade_period_rows = list(
        grade_rows.order_by()
        .values("grading_period")
        .annotate(average=Avg("grade_value"), count=Count("id"))
    )
    period_scores = {}
    period_counts = {}
    ordinals = {"1ST": "1st", "2ND": "2nd", "3RD": "3rd", "4TH": "4th"}
    for row in grade_period_rows:
        raw_period = (row["grading_period"] or "").strip()
        if not raw_period:
            label = WHOLE_YEAR
        else:
            parts = raw_period.split("_")
            label = (
                "{} {}".format(ordinals[parts[0].upper()], parts[1].replace("_", " ").title())
                if len(parts) == 2 and parts[0].upper() in ordinals
                else raw_period.replace("_", " ").title()
            )
        count = row["count"] or 0
        if count:
            period_scores[label] = period_scores.get(label, 0) + float(row["average"]) * count
            period_counts[label] = period_counts.get(label, 0) + count
    period_scores = {
        label: round(total / period_counts[label], 1)
        for label, total in period_scores.items()
        if period_counts[label]
    }
    if not period_scores:
        period_scores = _averages(score_by_period)
    period_rank = {"1ST": 1, "Q1": 1, "2ND": 2, "Q2": 2, "3RD": 3, "Q3": 3, "4TH": 4, "Q4": 4}
    def _period_order(label):
        token = label.upper().split()[0]
        return (label == WHOLE_YEAR, period_rank.get(token, 99), label)
    period_order = sorted(period_scores, key=_period_order)
    performance_by_quarter = _series(
        period_order, [period_scores[key] for key in period_order]
    )

    # Completion is submissions against the students actually enrolled, so a
    # class nobody submitted in is absent rather than reading as 0%.
    enrolled = dict(
        VirtualClassroomEnrollment.objects.filter(status=STATUS_ACTIVE)
        .values_list("virtual_classroom_id")
        .annotate(c=Count("id"))
    )
    submitted = dict(
        Submission.objects.order_by()
        .values_list("assessment_assignment__virtual_classroom_id")
        .annotate(c=Count("id"))
    )
    completion = {}
    for vc_id, count in enrolled.items():
        classroom = classrooms.get(vc_id)
        if classroom is None or not count:
            continue
        completion[classroom.grading_period or WHOLE_YEAR] = completion.get(
            classroom.grading_period or WHOLE_YEAR, [0, 0]
        )
        bucket = completion[classroom.grading_period or WHOLE_YEAR]
        bucket[0] += submitted.get(vc_id, 0)
        bucket[1] += count
    completion_series = _series(
        [key for key in sorted(completion, key=lambda k: (k == WHOLE_YEAR, k))],
        [
            round(completion[key][0] * 100 / completion[key][1])
            for key in sorted(completion, key=lambda k: (k == WHOLE_YEAR, k))
            if completion[key][1]
        ],
    )

    # Pass/fail is reported per assessment rather than per classroom, so an
    # assessment assigned to several sections accumulates their submissions.
    totals = {}
    for (classroom, assessment_id), bucket in pass_fail.items():
        current = totals.setdefault(assessment_id, [0, 0])
        current[0] += bucket[0]
        current[1] += bucket[1]
    top_assessments = sorted(totals.items(), key=lambda item: item[1][0] + item[1][1], reverse=True)[
        :TOP_CLASSES
    ]
    titles = dict(
        Assessment.objects.filter(pk__in=[key for key, _ in top_assessments]).values_list(
            "pk", "title"
        )
    )
    pass_fail_series = {
        "labels": [
            (titles.get(assessment_id) or "Assessment")[:26]
            for assessment_id, _ in top_assessments
        ],
        "passed": [totals[assessment_id][0] for assessment_id, _ in top_assessments],
        "failed": [totals[assessment_id][1] for assessment_id, _ in top_assessments],
    }

    # ---- assessments and resources ---------------------------------------
    type_counts = dict(
        Assessment.objects.filter(status="active")
        .filter(assignments__virtual_classroom__in=vc_ids)
        .order_by()
        .values_list("assessment_type")
        .annotate(c=Count("id"))
    )
    type_labels = dict(Assessment.ASSESSMENT_TYPE_CHOICES)
    assessment_by_type = _series(
        [type_labels.get(key, key) for key in sorted(type_counts)],
        [type_counts[key] for key in sorted(type_counts)],
    )

    placement_counts = dict(
        LearningResourcePlacement.objects.filter(virtual_classroom__in=vc_ids)
        .order_by()
        .values_list("virtual_classroom__teacher_id")
        .annotate(c=Count("id"))
    )
    assessment_counts = dict(
        AssessmentAssignment.objects.filter(virtual_classroom__in=vc_ids)
        .order_by()
        .values_list("virtual_classroom__teacher_id")
        .annotate(c=Count("id"))
    )
    teacher_names = {}
    for classroom in classrooms.values():
        teacher_id = classroom.teacher_id
        if teacher_id and teacher_id not in teacher_names:
            profile = getattr(classroom.teacher, "employee_profile", None)
            teacher_names[teacher_id] = (
                f"{profile.last_name}, {profile.first_name}" if profile else "Unassigned"
            )
    ranked_teachers = sorted(placement_counts.items(), key=lambda item: item[1], reverse=True)[
        :TOP_CLASSES
    ]
    posts_by_teacher = _series(
        [teacher_names.get(teacher_id, "Unassigned") for teacher_id, _ in ranked_teachers],
        [count for _, count in ranked_teachers],
    )
    teacher_post = {
        "labels": [teacher_names.get(teacher_id, "Unassigned") for teacher_id, _ in ranked_teachers],
        "uploads": [count for _, count in ranked_teachers],
        "assessments": [assessment_counts.get(teacher_id, 0) for teacher_id, _ in ranked_teachers],
    }
    teacher_content = _series(
        posts_by_teacher["labels"],
        [
            assessment_counts.get(teacher_id, 0)
            for teacher_id, _ in sorted(
                placement_counts.items(), key=lambda item: item[1], reverse=True
            )[:TOP_CLASSES]
        ],
    )

    # ---- weekly publishing activity -------------------------------------
    # Feeds the Principal dashboard's "Uploads vs Assessments" chart
    # (``prinCharts.uploads``). Previously this key had no producer at all, so
    # the chart could only ever fall back to its empty state. Weeks are built
    # from the union of both date axes so a week with uploads but no
    # assessments still gets a zero rather than a gap.
    weekly_uploads, weekly_assessments = _weekly_publishing_activity(classrooms)

    return {
        "attendanceByPeriod": by_period,
        "attendanceByClass": attendance_by_class,
        "attendanceBreakdown": breakdown,
        "scoreByClass": score_by_class,
        "averageByGrade": average_by_grade,
        "performanceByQuarter": performance_by_quarter,
        "completionByQuarter": completion_series,
        "passFail": pass_fail_series,
        "assessmentByType": assessment_by_type,
        "postsByTeacher": posts_by_teacher,
        "teacherPost": teacher_post,
        "teacherContent": teacher_content,
        "enrollmentByGrade": _enrollment_by_grade(classrooms),
        "uploads": {
            "labels": [label for label, _ in weekly_uploads],
            "uploads": [count for _, count in weekly_uploads],
            "assessments": [count for _, count in weekly_assessments],
        },
    }


WEEKLY_WINDOW = 8


def _performance_class_rows(vcs, active_year=None):
    """Per-class performance summary for the Student Performance panel.

    One row per classroom: score average (as a percentage of each assessment's
    possible points), attendance rate, submission completion rate and an
    on-track flag (≥ 75). ``vcs`` is the caller's campus-scoped, active-year
    classroom queryset; the weakest/strongest classes are capped at 12 so the
    table stays readable.
    """
    classrooms = list(
        vcs.select_related("section", "teacher__employee_profile", "subject")
        .order_by("section__grade_level", "section__name", "subject__name")[:12]
    )
    if not classrooms:
        return []
    vc_ids = list({vc.pk for vc in classrooms})

    possible = {
        row["assessment_id"]: row["total"]
        for row in AssessmentPart.objects.values("assessment_id").annotate(total=Sum("points"))
    }
    assignment_map = {
        pk: (vc_id, assessment_id)
        for pk, vc_id, assessment_id in AssessmentAssignment.objects.filter(
            virtual_classroom_id__in=vc_ids
        ).values_list("id", "virtual_classroom_id", "assessment_id")
    }

    stats = {
        vc_id: {"score_sum": 0.0, "score_n": 0, "subs": 0} for vc_id in vc_ids
    }
    for assignment_id, score in Submission.objects.filter(
        total_score__isnull=False,
        total_score__gt=0,
        assessment_assignment_id__in=list(assignment_map.keys()),
    ).values_list("assessment_assignment_id", "total_score"):
        target = assignment_map.get(assignment_id)
        if not target:
            continue
        vc_id, assessment_id = target
        denominator = possible.get(assessment_id) or 0
        if not denominator:
            continue
        bucket = stats[vc_id]
        bucket["score_sum"] += float(score) * 100.0 / float(denominator)
        bucket["score_n"] += 1
        bucket["subs"] += 1

    enrolled = dict(
        VirtualClassroomEnrollment.objects.filter(
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            virtual_classroom_id__in=vc_ids,
        )
        .order_by()
        .values_list("virtual_classroom_id")
        .annotate(c=Count("id"))
    )
    assignment_counts = dict(
        AssessmentAssignment.objects.filter(virtual_classroom_id__in=vc_ids)
        .order_by()
        .values_list("virtual_classroom_id")
        .annotate(c=Count("id"))
    )
    att_rows = (
        AttendanceRecord.objects.filter(session__virtual_classroom_id__in=vc_ids)
        .order_by()
        .values("session__virtual_classroom_id")
        .annotate(total=Count("id"), present=Count("id", filter=Q(status__in=PRESENT_STATUSES)))
    )
    attendance = {
        row["session__virtual_classroom_id"]: (row["present"], row["total"])
        for row in att_rows
    }

    rows = []
    for vc in classrooms:
        bucket = stats.get(vc.pk)
        students = enrolled.get(vc.pk, 0)
        assignments = assignment_counts.get(vc.pk, 0)
        avg = (
            round(bucket["score_sum"] / bucket["score_n"], 1)
            if bucket and bucket["score_n"]
            else 0
        )
        present_count, total = attendance.get(vc.pk, (0, 0))
        att_pct = round(present_count * 100.0 / total, 1) if total else 0
        expected = students * assignments
        sub_pct = (
            round(min(bucket["subs"] * 100.0 / expected, 100), 1)
            if bucket and expected
            else 0
        )
        rows.append(
            {
                "cls": (
                    "G{}-{}".format(vc.section.grade_level, vc.section.name)
                    if vc.section
                    else "—"
                ),
                "teacher": vc.teacher.get_full_name() if vc.teacher else "—",
                "students": students,
                "avg": avg,
                "att": att_pct,
                "sub": sub_pct,
                "onTrack": avg >= 75,
            }
        )
    return rows


def _coordinator_class_rows(vcs, limit=12):
    """Class-level attendance rows for the Academic Coordinator's table.

    Same per-class shape the Principal's Attendance Overview ships
    (``{cls, teacher, students, avg, last, status}``), but the wording and
    spelling are the ones ``renderAttendTable()`` in
    ``academic_coordinator.html`` compares against: status must read exactly
    ``"At Risk"`` to colour the pill, and the Teacher column takes the full
    name rather than initials.
    """
    rows = []
    for vc in vcs.select_related("section", "teacher").order_by(
        "section__grade_level", "section__name", "subject__name"
    )[:limit]:
        recs = AttendanceRecord.objects.filter(session__virtual_classroom=vc)
        total = recs.count()
        present = recs.filter(status__in=PRESENT_STATUSES).count()
        last_rec = recs.select_related("session").order_by("-session__session_date").first()
        rows.append(
            {
                "cls": "G{}-{}".format(vc.section.grade_level, vc.section.name)
                if vc.section
                else "—",
                "teacher": vc.teacher.get_full_name() if vc.teacher else "—",
                "students": vc.enrollments.filter(
                    status=VirtualClassroomEnrollment.STATUS_ACTIVE
                ).count(),
                "avg": round(present / total * 100, 1) if total else 0,
                "last": last_rec.session.session_date.strftime("%b %d")
                if last_rec and last_rec.session
                else "—",
                "status": "At Risk" if total and (present / total) < 0.9 else "Normal",
            }
        )
    return rows


def _coordinator_performance_rows(enrolled, active_year=None, limit=40):
    """Per-learner performance rows for the Academic Coordinator's table.

    Emits exactly the keys ``renderPerfTable()`` reads — ``{student, cls,
    teacher, avg, att, sub}`` — one row per learner enrolled in the
    Coordinator's campus-scoped classrooms:

    * ``avg``  mean of the learner's graded submissions as a percentage of
      each assessment's possible points (``None`` when nothing is graded);
    * ``att``  present rate over every attendance record in their classrooms
      (``None`` when no session has been recorded);
    * ``sub``  graded work against the assignments published to those same
      classrooms, capped at 100.

    A learner with no score, no attendance and no submissions is left out, so
    a school that has just started reporting shows the panel's empty state
    instead of a list of zeros that would all read "At Risk". Rows are sorted
    weakest first so the students who need attention are at the top.
    """
    members = {}
    vc_info = {}
    for enr in enrolled.select_related(
        "student",
        "student__user",
        "virtual_classroom",
        "virtual_classroom__section",
        "virtual_classroom__teacher",
    ):
        vc = enr.virtual_classroom
        if vc is None:
            continue
        label = vc_info.setdefault(
            vc.pk,
            (
                "G{}-{}".format(vc.section.grade_level, vc.section.name)
                if vc.section
                else "—",
                vc.teacher.get_full_name() if vc.teacher else "—",
            ),
        )
        entry = members.setdefault(
            enr.student_id, {"profile": enr.student, "vc_ids": set()}
        )
        entry["vc_ids"].add(vc.pk)
        entry["cls"], entry["teacher"] = label
    if not members:
        return []

    student_ids = sorted(members)
    vc_ids = sorted(vc_info)

    assignments = AssessmentAssignment.objects.filter(
        virtual_classroom_id__in=vc_ids
    )
    if active_year:
        assignments = assignments.filter(assessment__academic_year=active_year)
    assignment_of = {}
    assignments_per_vc = defaultdict(int)
    for assignment_id, assessment_id, vc_id in assignments.values_list(
        "id", "assessment_id", "virtual_classroom_id"
    ):
        assignment_of[assignment_id] = assessment_id
        assignments_per_vc[vc_id] += 1

    possible = {
        row["assessment_id"]: row["total"]
        for row in AssessmentPart.objects.values("assessment_id").annotate(
            total=Sum("points")
        )
    }
    score_sum = defaultdict(float)
    score_n = defaultdict(int)
    graded_n = defaultdict(int)
    for student_id, assignment_id, score in Submission.objects.filter(
        student_id__in=student_ids,
        assessment_assignment_id__in=list(assignment_of),
        total_score__isnull=False,
        total_score__gt=0,
    ).values_list("student_id", "assessment_assignment_id", "total_score"):
        denominator = possible.get(assignment_of.get(assignment_id)) or 0
        if not denominator:
            continue
        score_sum[student_id] += float(score) * 100.0 / float(denominator)
        score_n[student_id] += 1
        graded_n[student_id] += 1

    present_n = defaultdict(int)
    total_n = defaultdict(int)
    for student_id, status in AttendanceRecord.objects.filter(
        student_id__in=student_ids, session__virtual_classroom_id__in=vc_ids
    ).values_list("student_id", "status"):
        total_n[student_id] += 1
        if status in PRESENT_STATUSES:
            present_n[student_id] += 1

    rows = []
    for student_id, entry in members.items():
        n = score_n.get(student_id, 0)
        avg = round(score_sum[student_id] / n, 1) if n else None
        total = total_n.get(student_id, 0)
        att = round(present_n[student_id] * 100.0 / total, 1) if total else None
        expected = sum(assignments_per_vc.get(vc_id, 0) for vc_id in entry["vc_ids"])
        sub = (
            round(min(graded_n.get(student_id, 0) * 100.0 / expected, 100.0), 1)
            if expected
            else None
        )
        if avg is None and att is None and sub is None:
            continue
        rows.append(
            {
                "student": _qa_display_name(entry["profile"]),
                "cls": entry["cls"],
                "teacher": entry["teacher"],
                "avg": avg,
                "att": att,
                "sub": sub,
            }
        )
    rows.sort(
        key=lambda row: (
            row["avg"] is None,
            row["avg"] if row["avg"] is not None else 0,
            row["student"],
        )
    )
    return rows[:limit]


def _weekly_publishing_activity(classrooms=None):
    """Learning-resource placements and assessment assignments per ISO week.

    Returns two ``[(week_label, count), ...]`` lists aligned on the same week
    axis, oldest first. Only weeks that saw any activity are returned, so an
    empty table yields two empty lists and the chart keeps its empty state.

    ``classrooms`` (a ``{pk: vc}`` dict, possibly empty meaning none-in-scope)
    scopes the aggregates to the caller's campus. When ``None`` the whole school
    is counted.
    """
    cutoff = timezone.now() - timedelta(weeks=WEEKLY_WINDOW)

    def by_week(queryset, field):
        buckets = defaultdict(int)
        for value in queryset.filter(**{"{}__gte".format(field): cutoff}).values_list(
            field, flat=True
        ):
            iso_year, iso_week, _ = value.isocalendar()
            buckets[(iso_year, iso_week)] += 1
        return buckets

    placements = LearningResourcePlacement.objects.all()
    assignments = AssessmentAssignment.objects.all()
    if classrooms is not None:
        placements = placements.filter(virtual_classroom__in=classrooms.keys())
        assignments = assignments.filter(virtual_classroom__in=classrooms.keys())

    uploads = by_week(placements, "placed_at")
    assessments = by_week(assignments, "assigned_at")

    weeks = sorted(set(uploads) | set(assessments))[-WEEKLY_WINDOW:]
    labels = [
        "Week {:02d}".format(week) for _, week in weeks
    ]
    return (
        list(zip(labels, [uploads.get(week, 0) for week in weeks])),
        list(zip(labels, [assessments.get(week, 0) for week in weeks])),
    )


def _enrollment_by_grade(classrooms):
    counts = {}
    for vc_id, count in (
        VirtualClassroomEnrollment.objects.filter(status=STATUS_ACTIVE)
        .values_list("virtual_classroom_id")
        .annotate(c=Count("id"))
    ):
        classroom = classrooms.get(vc_id)
        if classroom is None:
            continue
        level = classroom.section.grade_level
        counts[level] = counts.get(level, 0) + count
    levels = sorted(counts)
    return _series([f"Grade {level}" for level in levels], [counts[level] for level in levels])


def teacher_context(user):
    my_classes = VirtualClassroom.objects.filter(
        teacher=user, status=STATUS_ACTIVE
    ).select_related("subject", "section", "academic_year").order_by("subject__name")

    class_items = []
    for vc in my_classes:
        topics, resources, assessments = _class_counts(vc)
        submissions = Submission.objects.filter(
            assessment_assignment__virtual_classroom=vc
        )
        pending_grading = submissions.filter(graded_at__isnull=True).count()
        students_count = (
            vc.enrollments.filter(status=VirtualClassroomEnrollment.STATUS_ACTIVE)
            .values("student_id")
            .distinct()
            .count()
        )
        class_items.append(
            {
                "vc": vc,
                "topics_count": topics,
                "resources_count": resources,
                "assessments_count": assessments,
                "students_count": students_count,
                "pending_grading": pending_grading,
            }
        )

    today = timezone.localdate()
    today_sessions = AttendanceSession.objects.filter(
        virtual_classroom__in=my_classes, session_date=today
    ).select_related("virtual_classroom__subject")

    needs_grading = (
        Submission.objects.filter(
            assessment_assignment__virtual_classroom__in=my_classes,
            graded_at__isnull=True,
        )
        .select_related(
            "student",
            "assessment_assignment__assessment",
            "assessment_assignment__virtual_classroom__subject",
        )
        .defer(
            "grading_status",
            "assessment_assignment__assessment__assessment_method",
            "assessment_assignment__assessment__lifecycle_stage",
            "assessment_assignment__assessment__available_at",
            "assessment_assignment__assessment__notification_settings",
        )
        .order_by("-submitted_at")[:6]
    )

    # Not truncated to the newest five: the front-end scopes this list to the
    # virtual classroom the teacher has open, so every classroom needs its own
    # rows present here.
    class_announcements = (
        ClassAnnouncement.objects.filter(
            virtual_classroom__in=my_classes, status=STATUS_ACTIVE
        )
        .select_related("virtual_classroom")
        .order_by("-posted_at")[:60]
    )

    # ── Real-data payload for the new front-end dashboard ─────────────────
    def _iso(v):
        return v.isoformat() if v else None

    def _dt(v):
        return timezone.localtime(v).strftime("%Y-%m-%d %H:%M") if v else None

    teacher_display = user.get_full_name() or user.email

    palette = ["#1e5faf", "#16a34a", "#e11d48", "#7c3aed", "#d97706", "#0891b2", "#4f46e5"]
    classes_json = []
    for idx, item in enumerate(class_items):
        vc = item["vc"]
        subj = vc.subject
        sect = vc.section
        grade = getattr(subj, "grade_level", None)
        grade_label = ("Grade " + str(grade)) if grade else (subj.name or "Class")
        classes_json.append(
            {
                "id": vc.id,
                "title": (grade_label + " – " + subj.name) if grade else subj.name,
                "section": sect.name if sect else "",
                "teacher": teacher_display,
                "students": item["students_count"],
                "grade": grade_label,
                "subject": subj.name,
                "year": getattr(vc.academic_year, "label", None) or "",
                "status": "Active",
                "color": vc.theme_color or palette[idx % len(palette)],
                "background": (vc.background_image.url if vc.background_image else ""),
            }
        )

    assignments = AssessmentAssignment.objects.filter(
        virtual_classroom__in=my_classes, assessment__status=STATUS_ACTIVE
    ).select_related(
        "assessment",
        "assessment__subject",
        "virtual_classroom",
        "virtual_classroom__section",
        "virtual_classroom__subject",
    ).prefetch_related("assessment__parts").order_by("-assigned_at")[:40]

    assessments_json = []
    for aa in assignments:
        a = aa.assessment
        lifecycle = (
            "completed"
            if a.lifecycle_stage == Assessment.STAGE_COMPLETED
            else ("grading" if a.lifecycle_stage == Assessment.STAGE_GRADING else None)
        )
        vc = aa.virtual_classroom
        # The first uploaded file on any of this assessment's parts, so the
        # teacher's "uploaded assessment file" is visible from the Assessment
        # tab. An assessment without a file reports blanks.
        attachment_url = ""
        attachment_name = ""
        part = (
            a.parts.exclude(attached_file="")
            .exclude(attached_file__isnull=True)
            .first()
        )
        if part is not None:
            try:
                attachment_url = reverse(
                    "assessments:assessment_attachment", args=[a.pk, part.pk]
                )
                attachment_name = str(part.attached_file.name).rsplit("/", 1)[-1]
            except Exception:
                attachment_url = ""
                attachment_name = ""
        builder_parts, builder_questions, builder_has_manual = _assessment_builder_payload(a)
        assessments_json.append(
            {
                "id": a.id,
                "title": a.title,
                "type": a.get_assessment_method_display() or a.assessment_method,
                "openDate": _dt(a.available_at),
                "deadline": _dt(a.submission_deadline),
                "archived": a.status == STATUS_ARCHIVED,
                "classroomId": vc.id,
                "subject": (a.subject.name if a.subject else (vc.subject.name if vc.subject else "")),
                "cls": vc.section.name if vc.section else "",
                "lifecycle": lifecycle,
                "hasManual": builder_has_manual,
                "canEdit": a.created_by_id == user.pk,
                "parts": builder_parts,
                "questions": builder_questions,
                "attachmentUrl": attachment_url,
                "attachmentName": attachment_name,
            }
        )

    teacher_subject_ids = my_classes.values_list("subject_id", flat=True).distinct()
    repository_assessments = (
        Assessment.objects.filter(
            subject_id__in=teacher_subject_ids,
        )
        .select_related("subject")
        .prefetch_related("parts")
        .annotate(repository_share_count=Count("assignments", distinct=True))
        .order_by("subject__grade_level", "subject__name", "title", "pk")
    )
    assessment_repository_json = []
    for assessment in repository_assessments:
        repository_parts = list(assessment.parts.all())
        repository_questions = []
        for part in repository_parts:
            mode = "objective" if part.is_objective else "manual"
            for item in (part.items or {}).values():
                repository_questions.append({
                    "points": item.get("point", 0) if isinstance(item, dict) else 0,
                    "mode": mode,
                })
        attachment = next((part for part in repository_parts if part.attached_file), None)
        attachment_url = ""
        attachment_name = ""
        if attachment:
            attachment_url = reverse(
                "assessments:assessment_attachment",
                args=[assessment.pk, attachment.pk],
            )
            attachment_name = str(attachment.attached_file.name).rsplit("/", 1)[-1]
        assessment_repository_json.append({
            "id": assessment.pk,
            "title": assessment.title,
            "subject": assessment.subject.name if assessment.subject_id else "",
            "subjectId": assessment.subject_id,
            "type": assessment.get_assessment_method_display(),
            "items": len(repository_questions),
            "questions": repository_questions,
            "parts": [{
                "label": part.get_question_type_display(),
                "mode": "objective" if part.is_objective else "manual",
            } for part in repository_parts],
            "instructions": assessment.instructions or "",
            "openDate": assessment.available_at.isoformat() if assessment.available_at else "",
            "deadline": assessment.submission_deadline.isoformat() if assessment.submission_deadline else "",
            "lifecycle": "open",
            "attachmentUrl": attachment_url,
            "attachmentName": attachment_name,
            "canEdit": assessment.created_by_id == user.pk,
            "meta": timezone.localtime(assessment.submission_deadline).strftime("%b %d, %Y")
            if assessment.submission_deadline else "",
            "shares": assessment.repository_share_count,
            "updated": timezone.localtime(assessment.created_at).strftime("%b %d, %Y")
            if assessment.created_at else "",
        })

    # School-wide announcements (posted by the Principal) reach every teacher
    # dashboard. Class announcements stay separate because a teacher may edit
    # or archive their own, which they may never do to a Principal's post.
    school_announcements_json = [
        {
            "id": "school-" + str(sa.id),
            "kind": "school",
            "title": sa.title,
            "body": sa.content or "",
            "postedBy": (
                sa.posted_by.get_full_name() if sa.posted_by_id else "School Administration"
            ),
            "createdAt": _iso(sa.posted_at),
        }
        for sa in campus.filter_school_announcements(
            SchoolAnnouncement.objects.select_related("posted_by"),
            user,
        )
        .filter(status=STATUS_ACTIVE)
        .order_by("-posted_at")[:20]
    ]

    announcements_json = [
        {
            "id": ca.id,
            "announcement_id": ca.id,
            "kind": "class",
            "classroomId": ca.virtual_classroom_id,
            "title": (ca.title or (ca.content[:90] if ca.content else "Class announcement")),
            "body": ca.content or "",
            "postedBy": (
                ca.posted_by.get_full_name() if ca.posted_by_id else ""
            ),
            "createdAt": _iso(ca.posted_at),
        }
        for ca in class_announcements
    ]

    notifications_json = []
    ng_count = needs_grading.count()
    if ng_count:
        latest = needs_grading.first()
        notifications_json.append(
            {
                "id": "ng-" + str(ng_count),
                "type": "manual_grade",
                "msg": "{} submission(s) awaiting your grading".format(ng_count),
                "createdAt": _iso(latest.submitted_at if latest else timezone.now()),
                "unread": True,
            }
        )
    now_dt = timezone.now()
    for aa in assignments:
        dl = aa.assessment.submission_deadline
        if dl and 0 <= (dl - now_dt).total_seconds() <= 3 * 86400:
            notifications_json.append(
                {
                    "id": "dl-" + str(aa.id),
                    "type": "deadline_nearing",
                    "msg": '"{}" closes on {}'.format(
                        aa.assessment.title,
                        timezone.localtime(dl).strftime("%b %d, %I:%M %p"),
                    ),
                    "createdAt": _iso(dl),
                    "unread": True,
                }
            )
    notifications_json = notifications_json[:8]

    recent_subs = Submission.objects.filter(
        assessment_assignment__virtual_classroom__in=my_classes
    ).select_related(
        "student",
        "student__user",
        "assessment_assignment__assessment",
        "assessment_assignment__virtual_classroom",
        "assessment_assignment__virtual_classroom__section",
        "assessment_assignment__virtual_classroom__subject",
    ).order_by("-submitted_at")[:24]

    activity_json = []
    for sub in recent_subs:
        prof = sub.student
        sname = " ".join(filter(None, [prof.first_name, prof.last_name])) or (
            prof.user.get_full_name() if prof.user else prof.lrn or "Student"
        )
        vc = sub.assessment_assignment.virtual_classroom
        cls_label = vc.section.name if vc.section else (vc.subject.name if vc.subject else "Class")
        activity_json.append(
            {
                "icon": "fa-file-arrow-up",
                "bg": "#dbeafe",
                "color": "#2563eb",
                "text": '{} submitted "{}" ({} · {})'.format(
                    sname,
                    sub.assessment_assignment.assessment.title,
                    vc.subject.name if vc.subject else "",
                    cls_label,
                ),
                "time": _iso(sub.submitted_at),
            }
        )
    for ca in class_announcements:
        activity_json.append(
            {
                "icon": "fa-bullhorn",
                "bg": "#dcfce7",
                "color": "#16a34a",
                "text": "Posted announcement in {}".format(
                    ca.virtual_classroom.subject.name if ca.virtual_classroom.subject else "your class"
                ),
                "time": _iso(ca.posted_at),
            }
        )
    integrity_events = AssessmentProctoringEvent.objects.filter(
        assessment__assignments__virtual_classroom__in=my_classes,
        student__vc_enrollments__virtual_classroom__in=my_classes,
        student__vc_enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        event_type__in=(
            AssessmentProctoringEvent.EVENT_FOCUS_LOST,
            AssessmentProctoringEvent.EVENT_VISIBILITY_HIDDEN,
            AssessmentProctoringEvent.EVENT_SCREENSHOT_KEY,
            AssessmentProctoringEvent.EVENT_COPY_ATTEMPT,
            AssessmentProctoringEvent.EVENT_CONTEXT_MENU,
        ),
    ).select_related("assessment", "student").distinct().order_by("-created_at")[:32]
    for event in integrity_events:
        matching_assignment = AssessmentAssignment.objects.filter(
            assessment_id=event.assessment_id,
            virtual_classroom__in=my_classes,
            virtual_classroom__enrollments__student_id=event.student_id,
            virtual_classroom__enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).select_related("virtual_classroom__subject", "virtual_classroom__section").order_by(
            "virtual_classroom_id", "pk"
        ).first()
        if not matching_assignment:
            continue
        vc = matching_assignment.virtual_classroom
        prof = event.student
        student_name = " ".join(filter(None, [prof.first_name, prof.last_name])) or prof.lrn or "Student"
        class_label = vc.section.name if vc.section_id else (vc.subject.name if vc.subject_id else "Class")
        activity_json.append(
            {
                "icon": "fa-camera" if event.event_type == AssessmentProctoringEvent.EVENT_SCREENSHOT_KEY else "fa-triangle-exclamation",
                "bg": "#fee2e2",
                "color": "#dc2626",
                "text": '{}: {} during "{}" ({}).'.format(
                    student_name,
                    event.get_event_type_display().lower(),
                    event.assessment.title,
                    class_label,
                ),
                "time": _iso(event.created_at),
                "recordType": "proctoring",
                "recordId": event.pk,
            }
        )
    activity_json.sort(key=lambda x: x["time"] or "", reverse=True)
    activity_json = activity_json[:16]

    ts_total = today_sessions.count()
    ts_present = AttendanceRecord.objects.filter(
        session__virtual_classroom__in=my_classes,
        session__session_date=today,
        status__in=PRESENT_STATUSES,
    ).count()
    attendance_rate = round(ts_present * 100 / ts_total) if ts_total else None

    open_assessments = sum(
        1
        for aa in assignments
        if aa.assessment.lifecycle_stage != Assessment.STAGE_COMPLETED
        and (aa.assessment.submission_deadline is None or aa.assessment.submission_deadline > now_dt)
    )

    analytics = {"all": _class_analytics(list(my_classes))}
    for item in class_items:
        analytics[str(item["vc"].id)] = _class_analytics([item["vc"]])

    roster_json = []
    demo_rows = []
    demo_seen = set()
    seen_roster = set()
    for item in class_items:
        vc = item["vc"]
        enrolls = order_students(
            vc.enrollments.filter(
                status=VirtualClassroomEnrollment.STATUS_ACTIVE,
                student__user__status=User.Status.ACTIVE,
            ).select_related("student", "student__user"),
            prefix="student__",
        )
        for e in enrolls:
            prof = e.student
            key = (vc.id, prof.id)
            if key in seen_roster:
                continue
            seen_roster.add(key)
            roster_json.append(
                {
                    "id": str(prof.id),
                    "lrn": prof.lrn or "",
                    "name": " ".join(filter(None, [prof.first_name, prof.last_name]))
                    or (prof.user.get_full_name() if prof.user else prof.lrn or "Student"),
                    "first": prof.first_name or "",
                    "last": prof.last_name or "",
                    "sex": prof.sex or "",
                    "grade": vc.section.grade_level if vc.section_id else None,
                    "classroom_id": vc.id,
                    "classroom": (vc.section.name if vc.section else "")
                    or (vc.subject.name if vc.subject else ""),
                }
            )
            # One demographics row per learner even when they take several of
            # this teacher's classes — the panel counts heads, not seats.
            if prof.id not in demo_seen:
                demo_seen.add(prof.id)
                section = vc.section
                demo_rows.append(
                    _learner_demo_row(
                        prof,
                        _qa_grade_label(section.grade_level) if section else "",
                        (section.name if section else "")
                        or (vc.subject.name if vc.subject else ""),
                    )
                )
    demo_rows = [row for row in demo_rows if row]

    dashboard_json = {
        "profile": _self_profile(user),
        "calendar": _calendar_payload(user),
        "classes": classes_json,
        "roster": roster_json,
        # Age/Sex panel scoped to this teacher's own classrooms.
        "demographics": {
            "kind": "student",
            "label": "Learner",
            "rows": demo_rows,
        },
        "stats": {
            "classes": len(class_items),
            "students": sum(i["students_count"] for i in class_items),
            "openAssessments": open_assessments,
            "attendance": attendance_rate,
        },
        "assessments": assessments_json,
        "assessment_repository": assessment_repository_json,
        "notifications": notifications_json,
        "announcements": announcements_json,
        "school_announcements": school_announcements_json,
        "activity": activity_json,
        "analytics": analytics,
        # Per-student metrics for the six Analytics & Reports tiles. Without
        # this every report rendered the same hardcoded body.
        "report_data": _teacher_report_data(my_classes),
        # The school's official SF2 template (uploaded by the System Admin),
        # so teachers always use the current form without hunting for it.
        "sfTemplate": _sf2_template_payload(),
    }

    return {
        "class_items": class_items,
        "my_class_count": len(class_items),
        "total_students": sum(i["students_count"] for i in class_items),
        "open_assessments": sum(i["assessments_count"] for i in class_items),
        "today_sessions": today_sessions,
        "needs_grading": needs_grading,
        "needs_grading_count": ng_count,
        "recent_announcements": class_announcements,
        "dashboard_json": dashboard_json,
    }


def _attendance_summary(profile):
    records = AttendanceRecord.objects.filter(student=profile)
    total = records.count()
    present = records.filter(status__in=PRESENT_STATUSES).count()
    return {
        "total": total,
        "present": present,
        "absent": total - present,
        "rate": round((present / total) * 100, 1) if total else 0,
    }


def _school_avg_attendance(current=None):
    records = AttendanceRecord.objects.all()
    if current:
        records = records.filter(session__virtual_classroom__academic_year_id=current)
    total = records.count()
    if not total:
        return "—"
    present = records.filter(status__in=PRESENT_STATUSES).count()
    return f"{round(present / total * 100, 1)}%"


def _newest_announcements(user=None):
    """Newest announcements the given user is allowed to read.

    ``user=None`` (a System Administrator, Quality Assurance, anyone not
    signed in) sees every announcement, which keeps those dashboards unchanged.

    A school-wide notice is visible to both campuses. A campus-scoped notice is
    visible only to readers of that campus -- so an announcement the
    Elementary Principal posts never reaches a High School dashboard, and the
    reverse. Class announcements are already bounded by their classroom, and a
    classroom belongs to one campus, so they filter on the same grade bands.
    """
    school_qs = SchoolAnnouncement.objects.filter(status=STATUS_ACTIVE)
    class_qs = ClassAnnouncement.objects.filter(status=STATUS_ACTIVE)

    if user is not None:
        scopes = campus.campus_scopes_for(user)
        school_qs = campus.filter_school_announcements(school_qs, user)
        if campus.WHOLE_SCHOOL not in scopes:
            class_qs = campus.class_announcements_for_campus(class_qs, scopes)

    return {
        "school": school_qs.order_by("-posted_at")[:3],
        "class": class_qs.order_by("-posted_at")[:3],
    }


def _filter_roster_by_campus(directory, students, scopes):
    """Narrow an already-built roster payload to the caller's grade bands.

    ``_roster_directory`` returns plain dicts, not querysets, so the campus
    filter is applied to the assembled rows rather than in SQL. The grade band
    each row belongs to is ``gradeLevel`` on a section/subject row and ``grade``
    on a student row, which is exactly the column the query-level filter in
    ``apps.accounts.campus`` uses -- so the two agree.

    A row with no grade recorded (a student never enrolled this year, a section
    whose grade level was cleared) is dropped for a campus-scoped reader and
    kept for a whole-school reader: it cannot be attributed to a campus, so it
    cannot be asserted to be theirs.
    """
    if campus.WHOLE_SCHOOL in scopes:
        return directory, students

    def in_band(row, key):
        grade = row.get(key)
        if grade is None:
            return False
        low, high = campus.ELEMENTARY_GRADE_MIN, campus.ELEMENTARY_GRADE_MAX
        if campus.HIGH_SCHOOL in scopes:
            high = campus.HIGH_SCHOOL_GRADE_MAX
        else:
            low = campus.HIGH_SCHOOL_GRADE_MIN
        return low <= grade <= high

    def keep(rows, key):
        return [r for r in rows or [] if in_band(r, key)]

    scoped = dict(directory)
    scoped["sectionsByYear"] = {
        year: keep(rows, "gradeLevel") for year, rows in (directory.get("sectionsByYear") or {}).items()
    }
    scoped["subjectsByYear"] = {
        year: keep(rows, "gradeLevel") for year, rows in (directory.get("subjectsByYear") or {}).items()
    }
    scoped["studentsByYear"] = {
        year: keep(rows, "grade") for year, rows in (directory.get("studentsByYear") or {}).items()
    }
    scoped["sections"] = keep(directory.get("sections"), "gradeLevel")
    scoped["subjects"] = keep(directory.get("subjects"), "gradeLevel")
    return scoped, keep(students, "grade")


def _roster_directory(active_year_obj):
    """Read-only academic roster: sections, subjects and students per S.Y.

    Shared with the System Administrator directory so the two dashboards cannot
    drift apart on what a "section" or "student" row contains. It is only used
    by roles that hold the ``view_*`` codenames and no write permission, so
    nothing in the payload is actionable — every value is read straight off a
    stored column and an empty table renders as an empty state.

    The key names mirror ``system_admin_context``'s ``directory`` exactly
    (``sections``/``subjects`` flat lists alongside the per-year maps) so the
    Principal's Subjects, Sections and Students panels can reuse the System
    Administrator's rendering verbatim, minus every write affordance.
    """
    palette = [
        "linear-gradient(135deg,#1e5faf,#3a86d4)",
        "linear-gradient(135deg,#16a34a,#4ade80)",
        "linear-gradient(135deg,#f59e0b,#fbbf24)",
        "linear-gradient(135deg,#7c3aed,#a78bfa)",
        "linear-gradient(135deg,#e11d48,#f43f5e)",
        "linear-gradient(135deg,#0891b2,#22d3ee)",
        "linear-gradient(135deg,#b45309,#fbbf24)",
        "linear-gradient(135deg,#9333ea,#a78bfa)",
        "linear-gradient(135deg,#334155,#64748b)",
    ]
    ordinals = {"1ST": "1st", "2ND": "2nd", "3RD": "3rd", "4TH": "4th"}

    def period(value):
        if not value:
            return None
        parts = str(value).strip().split("_")
        if len(parts) == 2 and parts[0].upper() in ordinals:
            return "{} {}".format(ordinals[parts[0].upper()], parts[1].replace("_", " ").title())
        return str(value).replace("_", " ").title()

    years = list(AcademicYear.objects.order_by("-start_date").values_list("label", flat=True))
    year_sections = Section.objects.all()
    year_subjects = Subject.objects.all()
    if years:
        year_sections = year_sections.filter(academic_year__label__in=years)
        year_subjects = year_subjects.filter(academic_year__label__in=years)

    section_enroll = dict(
        StudentEnrollment.objects.filter(section__in=year_sections.values("id"))
        .exclude(status__in=["archived", "dropped"])
        .values_list("section_id")
        .annotate(n=Count("id"))
    )
    section_rooms = dict(
        VirtualClassroom.objects.filter(section__in=year_sections.values("id"))
        .values_list("section_id")
        .annotate(n=Count("id"))
    )
    sections_by_year = {}
    for s in year_sections.select_related("adviser", "academic_year").order_by(
        "academic_year__start_date", "grade_level", "name"
    ):
        label = s.academic_year.label if s.academic_year_id else "—"
        sections_by_year.setdefault(label, []).append(
            {
                "id": s.id,
                "name": s.name,
                "gradeLevel": s.grade_level,
                "year": label,
                "status": s.get_status_display() if hasattr(s, "get_status_display") else s.status,
                "adviser": (s.adviser.get_full_name() if s.adviser_id else None),
                "classrooms": section_rooms.get(s.id, 0),
                "students": section_enroll.get(s.id, 0),
            }
        )

    subject_rooms = dict(
        VirtualClassroom.objects.filter(subject__in=year_subjects.values("id"))
        .values_list("subject_id")
        .annotate(n=Count("id"))
    )
    subject_assessments = dict(
        Assessment.objects.filter(subject__in=year_subjects.values("id"))
        .values_list("subject_id")
        .annotate(n=Count("id"))
    )
    subjects_by_year = {}
    for s in year_subjects.select_related("academic_year").order_by(
        "academic_year__start_date", "grade_level", "name"
    ):
        label = s.academic_year.label if s.academic_year_id else "—"
        subjects_by_year.setdefault(label, []).append(
            {
                "id": s.id,
                "name": s.name,
                "gradeLevel": s.grade_level,
                "year": label,
                "subjectType": (s.get_subject_type_display() if hasattr(s, "get_subject_type_display") else s.subject_type),
                "gradingPeriod": period(s.get_grading_period_display() if hasattr(s, "get_grading_period_display") else s.grading_period),
                "status": s.get_status_display() if hasattr(s, "get_status_display") else s.status,
                "classrooms": subject_rooms.get(s.id, 0),
                "assessments": subject_assessments.get(s.id, 0),
            }
        )

    # A learner is listed once per S.Y. they were enrolled, so the school-year
    # filter can show how they were sectioned back then. The flat list below
    # stays scoped to the active year and backs the view when none is selected.
    students_by_year = {}
    for se in order_students(
        StudentEnrollment.objects.filter(academic_year__label__in=years)
        .select_related("student__user", "section", "academic_year")
        .exclude(status__in=["archived", "dropped"]),
        prefix="student__",
        leading_order=(
            "academic_year__start_date",
            "section__grade_level",
        ),
    ):
        sp = se.student
        label = se.academic_year.label if se.academic_year_id else "—"
        students_by_year.setdefault(label, []).append(
            {
                "lrn": sp.lrn,
                "name": " ".join(x for x in (sp.first_name, sp.middle_initial, sp.last_name) if x),
                "first": sp.first_name,
                "last": sp.last_name,
                "grade": (se.section.grade_level if se.section_id else None),
                "section": (se.section.name if se.section_id else ""),
                "sectionId": (se.section_id or None),
                "sex": sp.sex or "",
                "status": (sp.user.get_status_display() if sp.user_id else "Unassigned"),
                "email": (sp.user.email if sp.user_id else ""),
                "initials": "".join(x[0] for x in (sp.first_name, sp.last_name) if x).upper() or "..",
                "gradient": palette[len(students_by_year.get(label, [])) % len(palette)],
            }
        )

    active_enroll = {}
    if active_year_obj is not None:
        for se in (
            StudentEnrollment.objects.filter(academic_year=active_year_obj)
            .exclude(status__in=["archived", "dropped"])
            .select_related("section")
            .order_by("section__grade_level", "section__name")
        ):
            active_enroll.setdefault(se.student_id, se)

    students = []
    for i, sp in enumerate(
        order_students(StudentProfile.objects.select_related("user"))
    ):
        se = active_enroll.get(sp.pk)
        section = se.section if se and se.section_id else None
        students.append(
            {
                "lrn": sp.lrn,
                "name": " ".join(x for x in (sp.first_name, sp.middle_initial, sp.last_name) if x),
                "first": sp.first_name,
                "last": sp.last_name,
                "grade": (section.grade_level if section else None),
                "section": (section.name if section else ""),
                "sectionId": (section.pk if section else None),
                "sex": sp.sex or "",
                "status": (sp.user.get_status_display() if sp.user_id else "Unassigned"),
                "email": (sp.user.email if sp.user_id else ""),
                "initials": "".join(x[0] for x in (sp.first_name, sp.last_name) if x).upper() or "..",
                "gradient": palette[i % len(palette)],
            }
        )

    directory = {
        "year": (active_year_obj.label if active_year_obj else None),
        "academicYears": years,
        "sectionsByYear": sections_by_year,
        "subjectsByYear": subjects_by_year,
        "studentsByYear": students_by_year,
    }

    # Flat, active-year-scoped lists under the same keys the System Administrator
    # directory uses. They are sliced out of the per-year maps built above rather
    # than re-queried, which is what guarantees a section or subject shows the
    # identical adviser, classroom, enrolment and assessment counts in both
    # dashboards instead of merely looking alike.
    active_label = active_year_obj.label if active_year_obj else None
    directory["sections"] = list(sections_by_year.get(active_label, []))
    directory["subjects"] = list(subjects_by_year.get(active_label, []))
    return directory, students


_RESOURCE_TYPE_ICON = {
    "pdf": {"icon": "fa-solid fa-file-pdf", "cls": "pdf"},
    "presentation": {"icon": "fa-solid fa-file-powerpoint", "cls": "ppt"},
    "document": {"icon": "fa-solid fa-file-word", "cls": "doc"},
    "video": {"icon": "fa-solid fa-video", "cls": "vid"},
    "image": {"icon": "fa-solid fa-file-image", "cls": "img"},
    "audio": {"icon": "fa-solid fa-file-audio", "cls": "aud"},
    "link": {"icon": "fa-solid fa-link", "cls": "lnk"},
}


def _initials(full_name):
    """``'Ms. Lovelyn C. Malanot'`` -> ``'LM'``. Used wherever a compact
    attribution chip is wanted instead of a full name."""
    return "".join([p[0].upper() for p in full_name.replace(".", " ").split() if p][:2])


def _repository_materials_json(limit=12, virtual_classrooms=None):
    """Recent learning resources as placed into virtual classrooms.

    Backs the Learning Material Repository list on the oversight dashboards.
    The richer subject/topic/file-url view of the shared repository comes from
    ``/api/learning-resources/``; these rows additionally carry the class and the
    teacher who placed the item, which the oversight cards also read.

    ``virtual_classrooms`` narrows the list to a campus: a resource placed into
    a high-school classroom is not listed for the Elementary Principal. Callers
    that pass none (the System Administrator, Quality Assurance) are unaffected.
    """
    qs = LearningResourcePlacement.objects.select_related(
        "resource", "virtual_classroom__section", "placed_by"
    )
    if virtual_classrooms is not None:
        qs = qs.filter(virtual_classroom__in=virtual_classrooms)
    rows = []
    for pl in qs.order_by("-placed_at")[:limit]:
        res = pl.resource
        r_type = res.resource_type if res else "document"
        m_meta = dict(res.RESOURCE_TYPE_CHOICES).get(r_type, "File") if res else "File"
        t_meta = pl.placed_by.get_full_name() if pl.placed_by else "Faculty"
        clabel = ""
        if pl.virtual_classroom and pl.virtual_classroom.section:
            s = pl.virtual_classroom.section
            clabel = "G{}-{}".format(s.grade_level, s.name)
        rows.append(
            {
                "type": (_RESOURCE_TYPE_ICON.get(r_type) or {}).get("cls", "doc"),
                "icon": (_RESOURCE_TYPE_ICON.get(r_type) or {}).get("icon", "fa-solid fa-file"),
                "name": res.title[:60] if res else "Learning Resource",
                "meta": "{} · {}".format(
                    m_meta,
                    (
                        pl.resource.file.name.split(".")[-1].upper()
                        if pl.resource and pl.resource.file and "." in pl.resource.file.name
                        else "Online"
                    ),
                ),
                "teacher": _initials(t_meta),
                "cls": clabel,
                "date": timezone.localtime(pl.placed_at).strftime("%b %d")
                if hasattr(pl, "placed_at")
                else "",
                "dl": 0,
            }
        )
    return rows


def _repository_assessments_json(limit=12, virtual_classrooms=None):
    """Recent assessment assignments with their submission counts and averages.

    Backs the Assessment Repository list on the oversight dashboards. Subject is
    carried alongside the class because the repository filters on it; it was
    missing when the principal list was first built.

    ``virtual_classrooms`` narrows the list to one campus, so an Elementary
    Principal is not shown another campus's grade book.
    """
    qs = AssessmentAssignment.objects.select_related(
        "assessment",
        "virtual_classroom__section",
        "virtual_classroom__subject",
        "virtual_classroom__teacher",
    )
    if virtual_classrooms is not None:
        qs = qs.filter(virtual_classroom__in=virtual_classrooms)
    rows = []
    for aa in qs.order_by("-assigned_at")[:limit]:
        a = aa.assessment
        vc = aa.virtual_classroom
        subs = Submission.objects.filter(assessment_assignment=aa).count()
        avg = (
            Submission.objects.filter(assessment_assignment=aa)
            .exclude(total_score=None)
            .aggregate(a=Avg("total_score"))["a"]
        )
        rows.append(
            {
                "id": a.id if a else None,
                "title": a.title if a else "Assessment",
                "type": (a.get_assessment_method_display() if a else "Quiz"),
                "subject": (vc.subject.name if vc and vc.subject else "—"),
                "grade": (
                    "Grade {}".format(vc.subject.grade_level)
                    if vc and vc.subject and vc.subject.grade_level
                    else ""
                ),
                "teacher": _initials(
                    vc.teacher.get_full_name() if vc and vc.teacher else "Faculty"
                ),
                "cls": (vc.section.name if vc and vc.section else "—"),
                "deadline": timezone.localtime(a.submission_deadline).strftime("%b %d")
                if a and a.submission_deadline
                else "—",
                "subs": subs,
                "avg": round(avg, 1) if avg else 0,
                "status": "Graded"
                if subs and (a is None or a.lifecycle_stage == Assessment.STAGE_COMPLETED)
                else "Pending",
                "updated": timezone.localtime(aa.assigned_at).strftime("%b %d, %Y")
                if getattr(aa, "assigned_at", None)
                else "—",
            }
        )
    return rows


def principal_context(user):
    active_year = _active_academic_year()
    current = active_year.id if active_year else None

    # Both campuses share this screen; the difference is the data. The scopes
    # below resolve to Whole School for anyone who is not campus-bound (the
    # System Administrator reading through, Quality Assurance, a teacher), so
    # scoping only ever narrows a principal's view -- it never blanks a panel
    # for someone who was never restricted in the first place.
    scopes = campus.campus_scopes_for(user)
    # The High School Principal oversees both campuses on the principal
    # dashboard; the Elementary Principal remains limited to Grades 1-6.
    if "principal" in campus.role_codes_for(user):
        scopes = {campus.WHOLE_SCHOOL}
    campus_code = campus.WHOLE_SCHOOL if campus.WHOLE_SCHOOL in scopes else campus.campus_scope_for(user)
    campus_label = campus.CAMPUS_LABELS.get(campus_code, campus.CAMPUS_LABELS[campus.WHOLE_SCHOOL])

    vcs = VirtualClassroom.objects.filter(status=STATUS_ACTIVE)
    sections = Section.objects.filter(status=Section.STATUS_ACTIVE)
    subjects = Subject.objects.filter(status=Subject.STATUS_ACTIVE)
    if current:
        vcs = vcs.filter(academic_year_id=current)
        sections = sections.filter(academic_year_id=current)
        subjects = subjects.filter(academic_year_id=current)

    vcs = campus.virtual_classrooms_for_campus(vcs, scopes)
    sections = campus.sections_for_campus(sections, scopes)
    subjects = campus.subjects_for_campus(subjects, scopes)

    # AttendanceRecord hangs off an AttendanceSession, which is what owns the
    # VirtualClassroom, so the campus filter has to go through `session`.
    records = AttendanceRecord.objects.filter(session__virtual_classroom__in=vcs)
    total_records = records.count()
    present_records = records.filter(status__in=PRESENT_STATUSES).count()

    # A campus principal sees the teachers who actually teach their grade bands,
    # not the whole staff list.
    visible_teacher_ids = (
        VirtualClassroom.objects.filter(
            teacher__isnull=False, status=STATUS_ACTIVE
        )
        .filter(pk__in=vcs.values("pk"))
        .values_list("teacher_id", flat=True)
        .distinct()
    )
    student_count = (
        VirtualClassroomEnrollment.objects.filter(
            virtual_classroom__in=vcs,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        )
        .values("student_id")
        .distinct()
        .count()
    )

    stats = [
        _stat("Students", student_count, "fa-user-graduate"),
        _stat("Teachers", len(set(visible_teacher_ids)), "fa-chalkboard-user"),
        _stat("Active Sections", sections.count(), "fa-users-between-lines"),
        _stat("Active Subjects", subjects.count(), "fa-book-open"),
        _stat("Virtual Classrooms", vcs.count(), "fa-chalkboard-user"),
        _stat("Attendance Present", f"{round(present_records / total_records * 100, 1) if total_records else 0}%", "fa-clipboard-check"),
    ]
    active_assessments = Assessment.objects.filter(status=STATUS_ACTIVE)
    if current:
        active_assessments = active_assessments.filter(academic_year_id=current)
    # A Submission points at an AssessmentAssignment, which is what owns the
    # VirtualClassroom -- so the campus boundary is reached the same way as
    # attendance, through the classroom.
    campus_submissions = Submission.objects.filter(
        assessment_assignment__virtual_classroom__in=vcs
    )
    all_submissions = campus_submissions.count()
    graded_submissions = campus_submissions.exclude(total_score=None).count()
    attendance_test = round(present_records / total_records * 100, 1) if total_records else 0
    submission_test = round(graded_submissions / all_submissions * 100, 1) if all_submissions else 0

    # ── Real-data payload for the new front-end dashboard ─────────────────
    announcements = _newest_announcements(None if campus.WHOLE_SCHOOL in scopes else user)
    palette = ["#1e5faf", "#16a34a", "#e11d48", "#f59e0b", "#7c3aed", "#0891b2", "#14b8a6", "#d97706"]
    grad_list = [
        "linear-gradient(135deg,#1e5faf,#3a86d4)",
        "linear-gradient(135deg,#16a34a,#4ade80)",
        "linear-gradient(135deg,#e11d48,#f43f5e)",
        "linear-gradient(135deg,#f59e0b,#fbbf24)",
        "linear-gradient(135deg,#7c3aed,#a78bfa)",
        "linear-gradient(135deg,#0891b2,#22d3ee)",
    ]

    teachers_qs = order_teachers(
        User.objects.filter(
            groups__name="teacher",
            status="active",
            pk__in=visible_teacher_ids,
        )
        .select_related("employee_profile")
        .distinct()
    )
    teachers_json = []
    for idx, t in enumerate(teachers_qs):
        # Already campus-filtered by way of `visible_teacher_ids`, but the
        # per-teacher classroom count still needs narrowing so a high-school
        # teacher teaching one elementary class is not credited with both.
        t_vcs = VirtualClassroom.objects.filter(teacher=t, status=STATUS_ACTIVE)
        if current:
            t_vcs = t_vcs.filter(academic_year_id=current)
        t_vcs = campus.virtual_classrooms_for_campus(t_vcs, scopes)
        t_classes = t_vcs.count()
        t_students = (
            VirtualClassroomEnrollment.objects.filter(
                virtual_classroom__in=t_vcs, status=VirtualClassroomEnrollment.STATUS_ACTIVE
            ).values("student_id").distinct().count()
        )
        t_uploads = LearningResourcePlacement.objects.filter(placed_by=t).count()
        t_assessments = AssessmentAssignment.objects.filter(
            virtual_classroom__in=t_vcs
        ).select_related("assessment", "virtual_classroom__section").order_by("-assigned_at")
        t_assess = t_assessments.count()
        t_scores = dict(
            Submission.objects.filter(
                assessment_assignment__in=t_assessments,
                total_score__isnull=False,
            )
            .values_list("assessment_assignment_id")
            .annotate(avg_score=Avg("total_score"))
        )
        t_performance = {"labels": [], "values": []}
        for assignment in t_assessments:
            assessment = assignment.assessment
            possible = float(assessment.total_possible_score or 0) if assessment else 0
            average = t_scores.get(assignment.id)
            if possible <= 0 or average is None:
                continue
            label = assessment.title or "Assessment"
            if assignment.virtual_classroom.section_id:
                label = "{} ? {}".format(label, assignment.virtual_classroom.section.name)
            t_performance["labels"].append(label[:32])
            t_performance["values"].append(round(float(average) * 100 / possible, 1))
            if len(t_performance["labels"]) >= 8:
                break
        t_ann = ClassAnnouncement.objects.filter(virtual_classroom__in=t_vcs).count()
        t_attendance = []
        attendance_summary = (
            AttendanceRecord.objects.filter(session__virtual_classroom__in=t_vcs)
            .values("student_id", "student__first_name", "student__last_name")
            .annotate(
                total=Count("id"),
                present=Count("id", filter=Q(status=AttendanceRecord.PRESENT_ON_TIME)),
                absent=Count("id", filter=Q(status__in=(AttendanceRecord.ABSENT_EXCUSED, AttendanceRecord.ABSENT_UNEXCUSED))),
                late=Count("id", filter=Q(status=AttendanceRecord.PRESENT_LATE)),
                attended=Count("id", filter=Q(status__in=PRESENT_STATUSES)),
            )
            .order_by("student__last_name", "student__first_name")
        )
        for row in attendance_summary:
            total = row["total"]
            t_attendance.append(
                {
                    "name": "{} {}".format(row["student__first_name"], row["student__last_name"]).strip(),
                    "total": total,
                    "present": row["present"],
                    "absent": row["absent"],
                    "late": row["late"],
                    "rate": round(row["attended"] * 100 / total, 1) if total else 0,
                }
            )
        name = t.get_full_name() or t.email
        initials = "".join([p[0].upper() for p in name.replace(".", " ").split() if p][:2]) or (t.email[0:2].upper() if t.email else "?")
        subj = (
            t_vcs.select_related("subject").first().subject.name
            if t_vcs.select_related("subject").first() and t_vcs.select_related("subject").first().subject
            else ""
        )
        weekly = ActivityLog.objects.filter(user=t, timestamp__gte=timezone.now() - timedelta(days=7)).count()
        freq = "high" if t_classes >= 4 or weekly >= 3 else ("mid" if t_classes >= 2 or weekly else "low")
        bars = []
        for d in range(7):
            day = timezone.now() - timedelta(days=d)
            ct = ActivityLog.objects.filter(
                user=t, timestamp__date=timezone.localdate(day)
            ).count()
            bars.append(3 if ct >= 3 else ct)
        bars.reverse()
        profile = getattr(t, "employee_profile", None)
        teachers_json.append(
            {
                "id": "t{}".format(t.id),
                "name": name,
                "initials": initials,
                "subject": subj or "Teacher",
                "gender": _sex_label(getattr(profile, "sex", None)) if profile else "",
                "age": _age_years(profile.birthdate) if profile else None,
                "classes": t_classes,
                "students": t_students,
                "uploads": t_uploads,
                "assessments": t_assess,
                "performance": t_performance,
                "attendance": t_attendance,
                "announcements": t_ann,
                "freq": freq,
                "grad": grad_list[idx % len(grad_list)],
                "bars": [min(b, 5) for b in bars],
            }
        )
    if not teachers_json:
        teachers_json = []

    # Staff demographics across every role (SA-panel parity), not just the
    # teachers who happen to hold a visible classroom. QA's copy of this screen
    # reuses the whole-school scope, so it matches the System Administrator's
    # panel exactly.
    staff_demo_rows = _all_staff_demo_rows()

    classes_json = []
    for idx, vc in enumerate(
        vcs.select_related("subject", "section", "teacher").order_by("subject__name")[:40]
    ):
        vc_students = (
            vc.enrollments.filter(status=VirtualClassroomEnrollment.STATUS_ACTIVE)
            .values("student_id").distinct().count()
        )
        vc_assess = AssessmentAssignment.objects.filter(virtual_classroom=vc).count()
        grade_lbl = ("Grade {}".format(vc.subject.grade_level)) if vc.subject and vc.subject.grade_level else ""
        tname = vc.teacher.get_full_name() if vc.teacher else "SVI Faculty"
        classes_json.append(
            {
                "id": vc.pk,
                "title": "{} – {}".format(grade_lbl, vc.subject.name if vc.subject else "Class"),
                "section": vc.section.name if vc.section else "",
                "teacher": tname,
                "students": vc_students,
                "assess": vc_assess,
                "grad": grad_list[idx % len(grad_list)],
            }
        )

    materials_json = _repository_materials_json(virtual_classrooms=vcs)
    assessments_json = _repository_assessments_json(virtual_classrooms=vcs)

    attendance_rows_json = []
    for vc in vcs.select_related("section", "teacher").order_by("section__grade_level")[:12]:
        vc_sess = AttendanceSession.objects.filter(virtual_classroom=vc)
        vc_recs = AttendanceRecord.objects.filter(session__virtual_classroom=vc)
        vc_total = vc_recs.count()
        vc_present = vc_recs.filter(status__in=PRESENT_STATUSES).count()
        last_rec = vc_recs.order_by("-session__session_date").first()
        attendance_rows_json.append(
            {
                "cls": "G{}-{}".format(vc.section.grade_level, vc.section.name) if vc.section else "—",
                "teacher": "".join([p[0].upper() for p in (vc.teacher.get_full_name() if vc.teacher else "Faculty").replace(".", " ").split() if p][:2]),
                "students": vc.enrollments.filter(status=VirtualClassroomEnrollment.STATUS_ACTIVE).count(),
                "avg": round(vc_present / vc_total * 100, 1) if vc_total else 0,
                "last": last_rec.session.session_date.strftime("%b %d") if last_rec and last_rec.session and hasattr(last_rec.session, "session_date") else "—",
                "status": "at-risk" if vc_total and (vc_present / vc_total) < 0.9 else "normal",
            }
        )

    activity_json = []
    # Recent Activity is the signed-in user's own trail. System Administrators
    # keep the global view (see system_admin_context); everyone else sees only
    # what they themselves did, so a Principal no longer sees the System
    # Administrator's logins and backups.
    for log in ActivityLog.objects.filter(user=user).select_related("user").order_by("-timestamp")[:8]:
        act = log.action.lower() if log.action else ""
        if "upload" in act or "resource" in act or "topic" in act:
            ltype = "upload"
        elif "assess" in act or "grad" in act or "score" in act:
            ltype = "assess"
        elif "attend" in act:
            ltype = "attend"
        elif "backup" in act:
            ltype = "backup"
        else:
            ltype = "login"
        activity_json.append(
            {
                "type": ltype,
                "detail": _friendly_activity_action(log.action),
                "time": timezone.localtime(log.timestamp).strftime("%b %d, %I:%M %p") if log.timestamp else "",
            }
        )

    ann_json = []
    for ann in announcements["school"]:
        ann_json.append(
            {
                "id": "s{}".format(ann.id),
                "title": ann.title if hasattr(ann, "title") else (ann.content[:80] if ann.content else "Announcement"),
                "content": ann.content if hasattr(ann, "content") else "",
                "date": timezone.localtime(ann.posted_at).strftime("%b %d, %Y") if hasattr(ann, "posted_at") and ann.posted_at else "—",
                "updated": "—",
                "archived": False,
            }
        )

    roster_directory, roster_students = _filter_roster_by_campus(
        *_roster_directory(active_year), scopes
    )

    # Enrollment + demographics rows in the QA panel shape (gender label, age
    # from birthdate, section and grade label). Both the Enrollment Status and
    # Student Demographics views are shared with Quality Assurance, so the rows
    # are built campus-scoped and the two renderers read the same list.
    enroll_rows, section_lookup_for_demographics = [], {}
    enroll_qs = StudentEnrollment.objects.filter(status="ENROLLED")
    if active_year:
        enroll_qs = enroll_qs.filter(academic_year=active_year)
    enroll_qs = campus.enrollments_for_campus(enroll_qs, scopes).select_related(
        "student__user", "section"
    ).order_by("section__grade_level", "section__name", "student__last_name")
    for enr in enroll_qs:
        sp = enr.student
        if getattr(sp, "sex", None) == "M":
            gender = "Male"
        elif getattr(sp, "sex", None) == "F":
            gender = "Female"
        else:
            gender = ""
        section_lookup_for_demographics.setdefault(
            sp.id,
            {"grade": _qa_grade_label(enr.section.grade_level), "section": enr.section.name},
        )
        enroll_rows.append(
            {
                "last": sp.last_name,
                "first": sp.first_name,
                "middle": sp.middle_initial,
                "lrn": sp.lrn,
                "gender": gender,
                "birth": sp.birthdate.isoformat() if sp.birthdate else "",
                "grade": _qa_grade_label(enr.section.grade_level),
                "section": enr.section.name,
                "status": "Enrolled",
            }
        )

    enroll_sections_rows = []
    for sec in sections.select_related("adviser").order_by("grade_level", "name"):
        adviser = (
            _qa_display_name(sec.adviser.employee_profile)
            if sec.adviser_id
            and getattr(sec.adviser, "employee_profile", None)
            and sec.adviser.status != "archived"
            else "Unassigned"
        )
        enroll_sections_rows.append(
            {"grade": _qa_grade_label(sec.grade_level), "name": sec.name, "adviser": adviser}
        )

    # Academic standing reuses the Principal's own section→learner map so the
    # grouped standing table is sectioned exactly like the Students panel above.
    principal_sections = {}
    standing_enrollments = StudentEnrollment.objects.filter(
        academic_year=active_year
    ).exclude(status__in=["archived", "dropped"])
    if active_year:
        standing_enrollments = campus.enrollments_for_campus(standing_enrollments, scopes)
    for enrollment in standing_enrollments.select_related("section"):
        if enrollment.section_id:
            principal_sections.setdefault(
                enrollment.student_id,
                {
                    "grade": enrollment.section.grade_level,
                    "section": enrollment.section.name,
                },
            )
    standing_rows = _qa_guarded(
        lambda: academic_standing_rows(active_year, principal_sections)
    ) or []

    dashboard_json = {
        "profile": _self_profile(user),
        "calendar": _calendar_payload(user),
        "yearLabel": ("S.Y. {}".format(active_year.label)) if active_year else "",
        # Which campus this principal governs. The front end shows it in the
        # header; it is also what makes it obvious that the same screen is
        # showing different rows for each principal rather than being two
        # different dashboards.
        "campus": {
            "code": campus_code,
            "label": campus_label,
            "scopes": sorted(scopes),
            "postAudiences": [
                {"code": code, "label": campus.CAMPUS_LABELS.get(code, code)}
                for code in campus.visible_campuses(user)
            ],
        },
        # Chart series for the Principal's attendance / performance / teacher
        # panels. Shapes are documented above `const prinCharts`.
        "charts": _school_chart_payload(scopes),
        "stats": {
            "teachers": len(teachers_json),
            "students": student_count,
            "materials": LearningResourcePlacement.objects.filter(
                virtual_classroom__in=vcs
            ).count(),
            "assessments": active_assessments.count(),
            "attendanceRate": attendance_test,
            "submissionRate": submission_test,
            "classes": vcs.count(),
            "lowActivity": User.objects.filter(
                pk__in=visible_teacher_ids, status="active"
            )
            .exclude(
                activity_logs__timestamp__gte=timezone.now() - timedelta(days=7)
            )
            .count(),
        },
        "teachers": teachers_json,
        "classes": classes_json,
        "materials": materials_json,
        "assessments": assessments_json,
        "attendance": attendance_rows_json,
        "activity": activity_json,
        "announcements": ann_json,
        "directory": roster_directory,
        "students": roster_students,
        "standing": standing_rows,
        # Per-class rows for the Student Performance + Analytics overview tables.
        "performance": _performance_class_rows(vcs, active_year),
        "enrollStudents": enroll_rows,
        "enrollSections": enroll_sections_rows,
        # Staff counterpart to the learner rows above. Sex and birthdate come
        # from EmployeeProfile, which had never been serialized before, and
        # serve the Teacher Demographics panel on both the Principal and the
        # Quality Assurance views.
        "staffDemographics": {
            "kind": "employee",
            "label": "Staff",
            "rows": staff_demo_rows,
        },
    }

    return {
        "stats": stats,
        "active_year": active_year,
        "announcements": announcements,
        "recent_reports": GeneratedReport.objects.order_by("-generated_at")[:5],
        "recent_activity": ActivityLog.objects.select_related("user").order_by("-timestamp")[:8],
        "teacher_count": _role_user_count("teacher"),
        "student_count": student_count,
        "materials_count": LearningResourcePlacement.objects.filter(
            virtual_classroom__in=vcs
        ).count(),
        "active_assessments": active_assessments.filter(
            assignments__virtual_classroom__in=vcs
        ).distinct().count(),
        "attendance_rate": attendance_test,
        "submission_rate": submission_test,
        "active_classes": vcs.count(),
        "low_activity_count": User.objects.filter(
            pk__in=visible_teacher_ids, status="active"
        )
        .exclude(
            activity_logs__timestamp__gte=timezone.now() - timedelta(days=7)
        )
        .count(),
        "is_quality_audit": bool(user.groups.filter(name="quality_audit").exists()),
        "dashboard_json": dashboard_json,
    }


def assistant_principal_context(user):
    active_year = _active_academic_year()
    current = active_year.id if active_year else None
    scopes = campus.campus_scopes_for(user)
    sections = campus.sections_for_campus(
        Section.objects.filter(status=Section.STATUS_ACTIVE), scopes
    )
    subjects = Subject.objects.filter(status=Subject.STATUS_ACTIVE)
    vcs = VirtualClassroom.objects.filter(status=VirtualClassroom.STATUS_ACTIVE)
    enrolled = VirtualClassroomEnrollment.objects.filter(
        status=VirtualClassroomEnrollment.STATUS_ACTIVE
    )
    if current:
        sections = sections.filter(academic_year_id=current)
        subjects = subjects.filter(academic_year_id=current)
        vcs = vcs.filter(academic_year_id=current)
        enrolled = enrolled.filter(virtual_classroom__academic_year_id=current)
    subjects = campus.subjects_for_campus(subjects, scopes)
    vcs = campus.virtual_classrooms_for_campus(vcs, scopes)
    enrolled = enrolled.filter(virtual_classroom__in=vcs)
    stats = [
        _stat("Active Sections", sections.count(), "fa-users-between-lines"),
        _stat("Active Subjects", subjects.count(), "fa-book-open"),
        _stat("Virtual Classrooms", vcs.count(), "fa-chalkboard-user"),
        _stat("Enrolled Students", enrolled.count(), "fa-user-graduate"),
    ]

    # ── Real-data payload for the new front-end dashboard ─────────────────
    palette = ["#1e5faf", "#16a34a", "#e11d48", "#f59e0b", "#7c3aed", "#0891b2", "#14b8a6"]
    vc_list = vcs.select_related("subject", "section", "teacher", "academic_year").order_by("subject__name")[:60]

    vcs_json = []
    for vc in vc_list:
        sec = vc.section
        grade_label = ("Grade {}".format(sec.grade_level)) if sec and sec.grade_level else (
            ("Grade {}".format(vc.subject.grade_level)) if vc.subject and vc.subject.grade_level else ""
        )
        is_shs = grade_label == "Grade 11" or grade_label == "Grade 12"
        is_elective = bool(
            _qa_guarded(
                lambda: vc.subject_id
                and getattr(vc.subject, "subject_type", "") == getattr(Subject, "TYPE_ELECTIVE", "elective")
            )
        )
        base_name = "{} {}".format((vc.subject.name if vc.subject else "Class"), grade_label.replace("Grade ", ""))
        if is_shs:
            base_name = "{} • SHS".format((vc.subject.name if vc.subject else "Class"))
        vcs_json.append(
            {
                "name": base_name,
                "subject": vc.subject.name if vc.subject else "Class",
                "grade": grade_label,
                "section": sec.name if sec else "",
                "teacher": vc.teacher.get_full_name() if vc.teacher else "SVI Faculty",
                "schedule": vc.schedule or "",
                "type": "elective" if is_elective else "core",
                "students": vc.enrollments.filter(status=VirtualClassroomEnrollment.STATUS_ACTIVE).count(),
                "status": "active" if vc.status == STATUS_ACTIVE else "archived",
                "track": "N/A",
            }
        )

    sections_json = []
    for sec in sections.select_related("academic_year").order_by("grade_level")[:60]:
        adviser_name = sec.adviser.get_full_name() if sec.adviser else "—"
        sections_json.append(
            {
                "name": sec.name,
                "grade": "Grade {}".format(sec.grade_level),
                "adviser": adviser_name,
                "students": VirtualClassroomEnrollment.objects.filter(
                    virtual_classroom__section=sec, status=VirtualClassroomEnrollment.STATUS_ACTIVE
                ).values("student_id").distinct().count(),
                "status": "active" if sec.status == Section.STATUS_ACTIVE else "archived",
            }
        )

    subjects_json = [
        {
            "name": subj.name,
            "grade": "Grade {}".format(subj.grade_level) if subj.grade_level else "",
            "type": "Elective" if subj.subject_type == getattr(Subject, "TYPE_ELECTIVE", "elective") else "Core",
            "vcs": VirtualClassroom.objects.filter(subject=subj, status=STATUS_ACTIVE).count(),
            "status": "active" if subj.status == Subject.STATUS_ACTIVE else "archived",
        }
        for subj in subjects.select_related("academic_year").order_by("grade_level", "name")[:60]
    ]

    # Teachers selectable when creating or updating a Virtual Classroom. The
    # Create modal used to ship hardcoded sample names and the Update modal's
    # "Assigned Teacher" dropdown was empty, so the real assigned teacher could
    # never be shown or changed. This list backs both dropdowns.
    teachers_json = []
    for teacher in (
        _non_archived_users()
        .filter(groups__name="teacher")
        .select_related("employee_profile")
        .all()
    ):
        teacher_subject = (
            VirtualClassroom.objects.filter(teacher=teacher, status=STATUS_ACTIVE)
            .select_related("subject")
            .values_list("subject__name", flat=True)
            .first()
        )
        teachers_json.append(
            {
                "id": teacher.id,
                "name": teacher.get_full_name() or teacher.email,
                "subject": teacher_subject or "",
            }
        )

    enrolled_json = []
    for enr in enrolled.select_related("student", "virtual_classroom__subject", "virtual_classroom__section").order_by("-enrolled_at")[:10]:
        prof = enr.student
        enrolled_json.append(
            {
                "lrn": prof.lrn if prof else "",
                "student": "{} {}".format(prof.first_name, prof.last_name) if prof else "—",
                "grade": ("Grade {}".format(enr.virtual_classroom.section.grade_level)) if enr.virtual_classroom and enr.virtual_classroom.section else "",
                "section": enr.virtual_classroom.section.name if enr.virtual_classroom and enr.virtual_classroom.section else "",
                "subject": enr.virtual_classroom.subject.name if enr.virtual_classroom and enr.virtual_classroom.subject else "",
                "teacher": enr.virtual_classroom.teacher.get_full_name() if enr.virtual_classroom and enr.virtual_classroom.teacher else "—",
                "status": "active",
                "ay": "S.Y. {}".format(enr.virtual_classroom.academic_year.label if enr.virtual_classroom and enr.virtual_classroom.academic_year else ""),
                "enrolledAt": timezone.localtime(enr.enrolled_at).strftime("%Y-%m-%d %H:%M") if enr.enrolled_at else "",
            }
        )

    # One Age/Sex row per learner across the Coordinator's virtual classrooms.
    demo_rows, demo_seen = [], set()
    for enr in order_students(
        enrolled.select_related(
            "student", "student__user", "virtual_classroom__section"
        ),
        prefix="student__",
    ):
        if enr.student_id in demo_seen:
            continue
        demo_seen.add(enr.student_id)
        section = enr.virtual_classroom.section if enr.virtual_classroom else None
        row = _learner_demo_row(
            enr.student,
            _qa_grade_label(section.grade_level) if section else "",
            section.name if section else "",
        )
        if row:
            demo_rows.append(row)

    # Read-only school-wide notices for the Announcements view, already campus
    # filtered for this user.
    announcements = _newest_announcements(user)

    # Every academic year with its grading system and active section/subject
    # counts. Backs the Coordinator's read-only "Academic Years" table and the
    # school-year pickers; without it those rendered a hardcoded sample year.
    academic_years_json = []
    for year in AcademicYear.objects.order_by("-start_date"):
        version = getattr(year, "structure_version", None)
        grading = (
            version.get_grading_period_type_display()
            if version is not None
            else "—"
        )
        academic_years_json.append(
            {
                "value": "S.Y. {}".format(year.label),
                "label": "S.Y. {}".format(year.label.replace("-", "–")),
                "grading": grading,
                "sections": Section.objects.filter(
                    academic_year=year, status=Section.STATUS_ACTIVE
                ).count(),
                "subjects": Subject.objects.filter(
                    academic_year=year, status=Subject.STATUS_ACTIVE
                ).count(),
                "status": "Active" if year.status == "ACTIVE" else "Archived",
            }
        )

    dashboard_json = {
        "profile": _self_profile(user),
        "calendar": _calendar_payload(user),
        "yearLabel": ("S.Y. {}".format(active_year.label)) if active_year else "",
        "academicYears": academic_years_json,
        # Chart series for the Coordinator's score / attendance / assessment
        # panels. Shapes are documented above `const acCharts`.
        "charts": _school_chart_payload(scopes),
        "stats": {
            "sections": sections.count(),
            "subjects": subjects.count(),
            "vcs": vcs.count(),
            "enrolled": enrolled.count(),
            "teachers": _role_user_count("teacher"),
            "teacherAssigned": vcs.values("teacher").distinct().count(),
            "avgScore": _qa_guarded(
                lambda: f"{StudentGrade.objects.aggregate(a=Avg('grade_value'))['a'] or 0:.1f}%"
            ) or "—",
            "avgAttendance": _qa_guarded(lambda: _school_avg_attendance(current)) or "—",
            "elective": _qa_guarded(
                lambda: vcs.filter(subject__subject_type=Subject.TYPE_ELECTIVE).distinct().count()
            ) or 0,
        },
        "vcList": vcs_json,
        "sections": sections_json,
        "subjects": subjects_json,
        "teachers": teachers_json,
        "enrolled": enrolled_json,
        # Age/Sex panel over the same learners, deduplicated by student.
        "demographics": {
            "kind": "student",
            "label": "Learner",
            "rows": demo_rows,
        },
        # Learning Material + Assessment Repository backing rows. The
        # Coordinator supervises teaching and learning, so it gets the same
        # read-only repositories the Principal has. Without these keys the
        # Academic Coordinator's two repository views could only ever render
        # their empty state.
        "materials": _repository_materials_json(),
        "assessments": _repository_assessments_json(),
        # Table rows behind Performance, Attendance and Announcements. The front
        # end reads `performance`, `attendanceRows` and `announcements` and
        # renders TASS.empty states when a key is missing or a list is empty, so
        # shipping them here is what turns those three panels from permanently
        # blank into real data.
        "performance": _coordinator_performance_rows(enrolled, current),
        # Per-class rows for the Analytics & Reports overview. The Student
        # Performance table above is per-learner, so the shared Principal-style
        # reports table needs its own class-level series.
        "reportsPerformance": _performance_class_rows(vcs, current),
        "attendanceRows": _coordinator_class_rows(vcs),
        "announcements": [
            {
                "id": "s{}".format(ann.id),
                "title": ann.title
                if hasattr(ann, "title")
                else (ann.content[:80] if ann.content else "Announcement"),
                "content": ann.content if hasattr(ann, "content") else "",
                "date": timezone.localtime(ann.posted_at).strftime("%b %d, %Y")
                if getattr(ann, "posted_at", None)
                else "—",
                "updated": "—",
                "archived": False,
            }
            for ann in announcements["school"]
        ],
        "activity": [
            {
                "action": _friendly_activity_action(log.action),
                "detail": "",
                "time": timezone.localtime(log.timestamp).strftime("%b %d, %Y, %I:%M %p") if log.timestamp else "—",
            }
            for log in ActivityLog.objects.filter(user=user).select_related("user").order_by("-timestamp")[:8]
        ],
    }

    return {
        "stats": stats,
        "active_year": active_year,
        "svi_vc_tea_dash": vcs.values("teacher").distinct().count(),
        "recent_vcs": vcs.select_related("section", "subject", "teacher").order_by("-created_at")[:6].annotate(
            enrolled_count=Count(
                "enrollments",
                filter=Q(enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE),
            )
        ),
        "svi_vc_dash": vcs.count(),
        "svi_sec_dash": sections.count(),
        "svi_sub_dash": subjects.count(),
        "svi_elec_dash": _qa_guarded(
            lambda: vcs.filter(subject__subject_type=Subject.TYPE_ELECTIVE).distinct().count()
        ) or 0,
        "svi_avg_score_dash": _qa_guarded(
            lambda: (
                f"{StudentGrade.objects.aggregate(a=Avg('grade_value'))['a'] or 0:.1f}%"
            )
        ),
        "svi_avg_att_dash": _qa_guarded(
            lambda: _school_avg_attendance(current)
        ),
        "dashboard_json": dashboard_json,
    }


def student_context(user):
    profile = StudentProfile.objects.filter(user=user).first()
    if profile is None:
        return {
            "has_profile": False,
            "stats": [],
            "my_classes": [],
            "pending_assessments": [],
            "attendance": {"total": 0, "present": 0, "absent": 0, "rate": 0},
            "grades": [],
            "announcements": {"school": [], "class": []},
            "dashboard_json": {"subjects": [], "stats": {}, "assessments": [], "scores": [], "announcements": [], "schoolAnnouncements": [], "attendance": [], "notifs": []},
        }

    active_year = _active_academic_year()

    my_vcs = VirtualClassroom.objects.filter(
        enrollments__student=profile,
        enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        status=STATUS_ACTIVE,
    ).select_related("subject", "section", "teacher", "academic_year")
    if active_year:
        my_vcs = my_vcs.filter(academic_year=active_year)

    own_section = (
        StudentEnrollment.objects.filter(
            student=profile,
            academic_year=active_year,
            status=StudentEnrollment.STATUS_ENROLLED,
        )
        .select_related("section")
        .first()
    )
    own_grade = own_section.section.grade_level if own_section and own_section.section else None
    if own_grade is None and my_vcs.exists():
        own_grade = my_vcs.first().section.grade_level if my_vcs.first().section else None

    pending = []
    now = timezone.now()
    for vc in my_vcs:
        assignments = AssessmentAssignment.objects.filter(
            virtual_classroom=vc, assessment__status=STATUS_ACTIVE
        ).select_related("assessment").defer(
            "assessment__assessment_method",
            "assessment__lifecycle_stage",
            "assessment__available_at",
            "assessment__notification_settings",
        )
        for assignment in assignments:
            assessment = assignment.assessment
            if assessment.submission_deadline and assessment.submission_deadline < now:
                continue
            submitted = Submission.objects.filter(
                assessment_assignment=assignment, student=profile
            ).exists()
            pending.append(
                {
                    "title": assessment.title,
                    "vc": vc,
                    "deadline": assessment.submission_deadline,
                    "submitted": submitted,
                }
            )
    pending.sort(key=lambda p: p["deadline"] or now)

    from apps.communication.models import user_matches_target_role

    class_ann = (
        ClassAnnouncement.objects.filter(virtual_classroom__in=my_vcs, status=STATUS_ACTIVE)
        .select_related("virtual_classroom__subject", "posted_by")
        .order_by("-posted_at")
    )
    class_ann = [ca for ca in class_ann if user_matches_target_role(user, getattr(ca, "target_role", "all"))]
    school_ann = campus.filter_school_announcements(
        SchoolAnnouncement.objects.select_related("posted_by"), user
    ).filter(status=STATUS_ACTIVE).order_by("-posted_at")
    school_ann = [sa for sa in school_ann if user_matches_target_role(user, getattr(sa, "target_role", "all"))]

    grades = StudentGrade.objects.filter(student=profile).order_by("-computed_at")[:6]

    # ── Real-data payload for the new front-end dashboard ─────────────────
    def _dt(v):
        return timezone.localtime(v).strftime("%b %d, %Y · %I:%M %p") if v else None

    def _tod(v):
        return timezone.localtime(v).strftime("%b %d, %Y") if v else None

    def _iso(v):
        return v.isoformat() if v else None

    palette = ["#1e5faf", "#16a34a", "#e11d48", "#f59e0b", "#7c3aed", "#0891b2", "#14b8a6"]

    def _subj_label(subj):
        name = subj.name if subj else ""
        grade = getattr(subj, "grade_level", None)
        if not name:
            return name
        return "{} {}".format(name, grade) if grade else name

    subjects_json = []
    for idx, vc in enumerate(my_vcs.order_by("subject__name")):
        subj = vc.subject
        grade = getattr(subj, "grade_level", None)
        grade_label = ("Grade " + str(grade)) if grade else ""
        title = _subj_label(subj)
        teacher_name = vc.teacher.get_full_name() if vc.teacher else "SVI Faculty"
        subjects_json.append(
            {
                "id": vc.id,
                "teacher_id": vc.teacher_id,
                "title": title,
                "teacher": teacher_name,
                "sub": "{} – {}".format(grade_label.replace("Grade ", ""), vc.section.name if vc.section else "Class"),
                "color": vc.theme_color or palette[idx % len(palette)],
                "grade": grade or 0,
                "year": "S.Y. {}".format(vc.academic_year.label if vc.academic_year else ""),
            }
        )

    assignments_all = AssessmentAssignment.objects.filter(
        virtual_classroom__in=my_vcs, assessment__status=STATUS_ACTIVE
    ).select_related(
        "assessment", "assessment__subject", "virtual_classroom", "virtual_classroom__section", "virtual_classroom__subject"
    ).order_by("assessment__submission_deadline")

    assessments_json = []
    from apps.dashboards.student_live import ASSESSMENT_COLOURS

    for aa in assignments_all:
        a = aa.assessment
        vc = aa.virtual_classroom
        subj = a.subject or vc.subject
        grade_lbl = ("Grade " + str(getattr(subj, "grade_level", ""))).strip()
        subject_lbl = _subj_label(subj)
        submitted = Submission.objects.filter(assessment_assignment=aa, student=profile).exists()
        dl = a.submission_deadline
        if submitted:
            status = "closed"
        elif dl and dl <= now:
            status = "overdue"
        elif a.available_at and a.available_at > now:
            status = "scheduled"
        elif a.lifecycle_stage == Assessment.STAGE_COMPLETED:
            status = "closed"
        else:
            status = "open"
        assessments_json.append(
            {
                "key": "assess-{}".format(aa.id),
                "id": aa.id,
                "assignment_id": aa.id,
                # The classroom the assessment belongs to. Without this the
                # classroom's Assessments tab (which scopes by classroom id)
                # could not match a row and showed nothing, so the work only
                # appeared in the dashboard's aggregate list.
                "classroom_id": vc.id,
                "assessment_id": a.id,
                "title": a.title,
                "subject": subject_lbl.strip(),
                "type": a.get_assessment_method_display().lower() or "assignment",
                "pts": int(a.total_possible_score or 0),
                "deadline": _dt(dl) or "Open",
                "available_at": _dt(a.available_at),
                "status": status,
                "submitted": submitted,
                "color": ASSESSMENT_COLOURS.get(status, "#1e5faf"),
                "period": vc.grading_period or "Quarter 1",
                "instr": a.instructions or "",
            }
        )

    # Assessment results the student actually earned. StudentGrade is only
    # written by the periodic grade-computation job, so on a classroom where
    # work has been submitted but grades have not been rolled up it is empty and
    # Performance Tracking showed nothing. The per-assessment score on each
    # graded Submission is always available, so drive the table from those
    # (newest first) and then layer any official StudentGrade rows on top.
    scores_json = []
    seen_score_keys = set()
    graded_submissions = (
        Submission.objects.filter(
            student=profile,
            assessment_assignment__virtual_classroom__in=my_vcs,
            total_score__isnull=False,
        )
        .select_related(
            "assessment_assignment__assessment",
            "assessment_assignment__assessment__subject",
            "assessment_assignment__virtual_classroom",
        )
        .order_by("-submitted_at")
    )
    for sub in graded_submissions:
        aa = sub.assessment_assignment
        a = aa.assessment
        vc = aa.virtual_classroom
        subj = a.subject or vc.subject
        pts = int(a.total_possible_score or 0)
        score = float(sub.total_score or 0)
        pct = round(100.0 * score / pts) if pts else 0
        key = "assess-{}".format(aa.id)
        seen_score_keys.add(key)
        scores_json.append(
            {
                "key": key,
                "title": a.title,
                "subject": _subj_label(subj).strip(),
                "period": vc.grading_period or "Quarter 1",
                "score": round(score),
                "pts": pts or 100,
                "pct": pct,
                "failed": pct < 75,
            }
        )

    # Official computed grades (one per subject/period), where they exist.
    grade_lookup = {g.subject_id: g for g in grades}
    for subj_id, g in grade_lookup.items():
        subj = g.subject
        v = float(g.grade_value)
        subject_lbl = _subj_label(subj)
        key = "grade-{}".format(g.id)
        if key in seen_score_keys:
            continue
        scores_json.append(
            {
                "key": key,
                "title": "{} • {}".format(subject_lbl.strip(), g.grading_period or "Period"),
                "subject": subject_lbl.strip(),
                "period": g.grading_period or "Quarter",
                "score": round(v),
                "pts": 100,
                "pct": round(v),
                "failed": v < 75,
            }
        )

    announcements_json = [
        {
            # The classroom id is what scopes this row to one virtual classroom.
            # The subject label alone is ambiguous because the same subject runs
            # in several sections.
            "classroom_id": (ca.virtual_classroom_id if ca.virtual_classroom_id else None),
            "announcement_id": ca.pk,
            "subject": _subj_label(ca.virtual_classroom.subject) if ca.virtual_classroom.subject else "Class",
            "title": (ca.title or (ca.content[:60] if ca.content else "Class announcement")),
            "body": ca.content or "",
            "by": (ca.posted_by.get_full_name() if ca.posted_by else "Teacher"),
            "date": _tod(ca.posted_at),
        }
        for ca in class_ann
    ]

    # Announcements per virtual classroom, keyed by classroom id for the same
    # reason as materials: a student in two sections of one subject must see
    # only what each section's own teacher posted, from first paint rather than
    # only after the live refresh. Same source as GET /api/my/classrooms/<id>/.
    from apps.dashboards.student_live import announcements_for

    announcements_by_classroom = {
        str(vc.pk): announcements_for(vc, user) for vc in my_vcs
    }

    # Learning materials per virtual classroom (keyed by classroom id, not by
    # subject name, so two sections of the same subject stay separate).
    from apps.dashboards.student_live import materials_for

    materials_by_classroom = {str(vc.pk): materials_for(vc) for vc in my_vcs}

    # Answerable questions per assessment, keyed by the slugified assessment
    # title. The student quiz taker opens a card by title and needs the actual
    # questions (see quiz_banks_for); without this the modal showed "No data
    # yet" for every assessment, including teacher-published ones.
    from apps.dashboards.student_live import quiz_banks_for

    quiz_banks = {}
    for vc in my_vcs:
        quiz_banks.update(quiz_banks_for(vc, profile))

    school_ann_json = [
        {
            "id": "school-{}".format(sa.id),
            "announcement_id": sa.id,
            "title": sa.title or "School announcement",
            "content": sa.content or "",
            "date": _tod(sa.posted_at),
            "by": (sa.posted_by.get_full_name() if sa.posted_by else "Principal"),
        }
        for sa in school_ann
    ]

    attendance_json = []
    for rec in AttendanceRecord.objects.filter(student=profile).select_related(
        "session__virtual_classroom__subject"
    ).order_by("-session__session_date")[:24]:
        status_lbl = "Present" if rec.status in PRESENT_STATUSES else "Absent"
        if rec.status == "present_late":
            status_lbl = "Late"
        attendance_json.append(
            {
                "date": rec.session.session_date.isoformat(),
                "status": status_lbl,
                "remark": rec.remark or "—",
            }
        )

    notifs_json = []
    if sum(1 for p in pending if not p["submitted"]):
        notifs_json.append(
            {
                "icon": "fa-hourglass-half",
                "type": "deadline",
                "msg": "You have {} pending assessment(s) to complete.".format(sum(1 for p in pending if not p["submitted"])),
                "time": "Now",
            }
        )
    if school_ann:
        first = school_ann[0]
        notifs_json.append(
            {
                "icon": "fa-bullhorn",
                "type": "announce",
                "msg": (first.title or "New school announcement from the Principal"),
                "time": "Recently",
            }
        )
    if notifs_json:
        notifs_json[0]["unread"] = True

    att_summary = _attendance_summary(profile)
    dashboard_json = {
        "profile": _self_profile(user),
        "calendar": _calendar_payload(user),
        "subjects": subjects_json,
        "grade": own_grade,
        "stats": {
            "subjects": len(subjects_json),
            "submitted": sum(1 for p in pending if p["submitted"]),
            "pending": sum(1 for p in pending if not p["submitted"]),
            "overdue": sum(1 for a in assessments_json if a["status"] == "overdue"),
            "attendanceRate": att_summary["rate"],
        },
        "assessments": assessments_json,
        "scores": scores_json,
        "announcements": announcements_json,
        "schoolAnnouncements": school_ann_json,
        "attendance": attendance_json,
        "notifs": notifs_json,
        # Learning materials, keyed by virtual classroom id rather than by
        # subject name: a student taking the same subject in two sections must
        # not see the other section's files. Same source as
        # GET /api/my/classrooms/<id>/ so the first paint and a live refresh
        # agree. This was previously missing entirely, so students saw an
        # empty Materials tab no matter what a teacher uploaded.
        "materials_by_classroom": materials_by_classroom,
        "announcements_by_classroom": announcements_by_classroom,
        "quizBanks": quiz_banks,
    }

    return {
        "has_profile": True,
        "student_name": f"{profile.first_name} {profile.last_name}",
        "lrn": profile.lrn,
        "stats": [
            _stat("My Classes", my_vcs.count(), "fa-chalkboard-user"),
            _stat("Pending Assessments", sum(1 for p in pending if not p["submitted"]), "fa-clipboard-list"),
            _stat("Attendance Rate", f"{_attendance_summary(profile)['rate']}%", "fa-clipboard-check"),
            _stat("Subjects", my_vcs.values("subject").distinct().count(), "fa-book-open"),
        ],
        "subject_count": my_vcs.values("subject").distinct().count(),
        "submitted_count": sum(1 for p in pending if p["submitted"]),
        "pending_count": sum(1 for p in pending if not p["submitted"]),
        "missed_count": Submission.objects.filter(student=profile, is_late=True).count(),
        "my_classes": my_vcs.order_by("subject__name"),
        "pending_assessments": pending[:6],
        "attendance": _attendance_summary(profile),
        "grades": grades,
        "announcements": {"school": school_ann, "class": class_ann},
        "dashboard_json": dashboard_json,
    }


# ---------------------------------------------------------------------------
# Quality Assurance dashboard
# ---------------------------------------------------------------------------
# ACTIVITY LABELS
# ---------------------------------------------------------------------------
# ActivityLog stores terse machine codes ("user_login", "api_denied:academic_year",
# "manual_backup_completed", "viewed_classroom:214"). A user's own Recent
# Activity should read like a sentence, and because the feed is now scoped to
# the signed-in user it no longer needs to name the actor.
_ACTIVITY_LABELS = {
    "user_login": "Signed in",
    "new_device_login": "Signed in from a new device",
    "manual_backup_completed": "Completed a system backup",
    "user_logout": "Signed out",
    "password_changed": "Changed their password",
    "password_change_failed": "Failed password change attempt",
    "account_locked": "Account locked after failed sign-ins",
    "role_assigned": "Assigned a user role",
    "role_unassigned": "Removed a user role",
}


def _friendly_activity_action(action):
    """Turn a stored ActivityLog action code into readable dashboard text."""
    if not action:
        return "Activity recorded"
    code, sep, arg = action.partition(":")
    arg = arg.replace("_", " ")
    if code == "api_denied":
        return "Blocked from opening {}".format(arg) if sep else "Blocked from an action"
    if code.startswith("viewed_"):
        entity = code[len("viewed_"):].replace("_", " ")
        if not sep:
            return "Viewed {}".format(entity)
        # The stored argument is the target's primary key, not a name, so a
        # bare number is shown as a reference rather than as the object.
        return "Viewed {} #{}".format(entity, arg) if arg.isdigit() else "Viewed {}".format(arg)
    label = _ACTIVITY_LABELS.get(code)
    if label is None:
        return code.replace("_", " ").strip().capitalize()
    return "{} ({})".format(label, arg) if sep else label


def _qa_display_name(profile):
    parts = [profile.first_name or ""]
    if profile.middle_initial:
        parts.append(f"{profile.middle_initial.upper()}.")
    parts.append(profile.last_name or "")
    return " ".join(p for p in parts if p)


def _qa_grade_label(grade_level):
    return f"Grade {grade_level}"


# DepEd-style academic standing bands, highest first. ``at_risk`` is deliberately
# the same 75 cut-off the dashboards already use for "passing", so a learner
# flagged here is flagged identically in every panel instead of the two drifting.
STANDING_BANDS = [
    (95, "Outstanding"),
    (90, "Outstanding"),
    (85, "Very Good"),
    (80, "Good"),
    (75, "Fair"),
    (60, "Needs Improvement"),
]
STANDING_FAILING = "Failed"
STANDING_UNGRADED = "Ungraded"
STANDING_AT_RISK_BELOW = 75


def _standing_label(average):
    """Map a general average to its academic standing descriptor."""
    for floor, label in STANDING_BANDS:
        if average >= floor:
            return label
    return STANDING_FAILING


def _submission_standing(active_year, lookup):
    """Per-learner general averages from graded submissions.

    Stand-in for ``StudentGrade`` rows until a grading formula has been run.
    DepEd-style: each subject contributes its average score (as a percentage of
    the assessment's possible points) and the general average is the mean of the
    subject averages. Learners with no graded work are omitted so an ungraded
    learner is never reported as 0.
    """
    submissions = Submission.objects.filter(
        total_score__isnull=False, total_score__gt=0
    )
    if active_year:
        submissions = submissions.filter(
            assessment_assignment__assessment__academic_year=active_year
        )

    possible = {
        row["assessment_id"]: row["total"]
        for row in AssessmentPart.objects.values("assessment_id").annotate(total=Sum("points"))
    }
    subject_of = dict(Assessment.objects.values_list("id", "subject__name"))
    assessment_of = dict(AssessmentAssignment.objects.values_list("id", "assessment_id"))

    per_subject = {}
    for assignment_id, student_id, score in submissions.values_list(
        "assessment_assignment_id", "student_id", "total_score"
    ):
        assessment_id = assessment_of.get(assignment_id)
        subject = subject_of.get(assessment_id)
        denominator = possible.get(assessment_id) or 0
        if not subject or not denominator or student_id not in lookup:
            continue
        pct = float(score) * 100.0 / float(denominator)
        per_subject.setdefault((student_id, subject), []).append(pct)
    if not per_subject:
        return {}

    profiles = {
        profile.pk: profile
        for profile in StudentProfile.objects.filter(
            pk__in={key[0] for key in per_subject}
        ).select_related("user")
    }

    subject_averages = {}
    for (student_id, subject), values in per_subject.items():
        subject_averages.setdefault(student_id, {})[subject] = (
            sum(values) / len(values)
        )

    standing = {}
    for student_id, subject_scores in subject_averages.items():
        averages = [round(value, 2) for value in subject_scores.values()]
        standing[student_id] = {
            "profile": profiles.get(student_id),
            "average": (
                round(sum(averages) / len(averages), 2) if averages else None
            ),
            "subjects": len(averages),
        }
    return standing


def academic_standing_rows(active_year, section_lookup=None):
    """Per-learner academic standing for the active school year.

    Returns one row per enrolled learner holding at least one recorded grade::

        {"name", "lrn", "grade", "section", "average", "standing",
         "at_risk", "subjects"}

    Learners are ordered by grade level, then section, then surname, so the
    grouped table on the Principal and Quality Assurance dashboards reads in
    the same order. The averages are computed from the stored ``StudentGrade``
    rows only — nothing is estimated or filled in, so a learner with no grades
    is reported as "Ungraded" rather than being given a zero that would drag the
    section average down.
    """
    lookup = section_lookup if section_lookup is not None else {}

    grades = StudentGrade.objects.select_related("student", "student__user")
    if active_year:
        grades = grades.filter(academic_year=active_year)

    per_student = {}
    if grades.exists():
        for g in grades:
            entry = per_student.setdefault(
                g.student_id,
                {"profile": g.student, "total": 0.0, "count": 0, "subjects": 0},
            )
            entry["total"] += float(g.grade_value)
            entry["count"] += 1
            entry["subjects"] += 1
    elif active_year:
        # No GradingFormula has run for this year yet, so StudentGrade is empty
        # even though a full set of graded submissions is on file. Fall back to
        # the same submission-based averages the QA performance panels use so
        # the standing table (and the "Learners Graded" snapshot) never reads
        # as an empty school.
        fallback = _submission_standing(active_year, lookup)
        for student_id, entry in fallback.items():
            per_student[student_id] = {
                "profile": entry["profile"],
                "total": entry["average"] if entry["average"] is not None else 0.0,
                "count": 1 if entry["average"] is not None else 0,
                "subjects": entry["subjects"],
            }

    rows = []
    for student_id, entry in per_student.items():
        profile = entry["profile"]
        section = lookup.get(student_id)
        average = round(entry["total"] / entry["count"], 2) if entry["count"] else None
        rows.append(
            {
                "name": _qa_display_name(profile),
                "lrn": profile.lrn,
                "grade": section["grade"] if section else None,
                "section": section["section"] if section else "",
                "average": average,
                "standing": (
                    _standing_label(average)
                    if average is not None
                    else STANDING_UNGRADED
                ),
                "at_risk": bool(
                    average is not None and average < STANDING_AT_RISK_BELOW
                ),
                "subjects": entry["subjects"],
            }
        )

    def sort_key(row):
        # Callers key the section map by either the raw Section.grade_level
        # integer (the Principal) or a "Grade N" label (Quality Assurance), so
        # normalise to text before pulling the digits out.
        grade = "" if row["grade"] is None else str(row["grade"])
        digits = "".join(ch for ch in grade if ch.isdigit())
        return (int(digits) if digits else 0, row["section"] or "", row["name"])

    rows.sort(key=sort_key)
    return rows


def _qa_guarded(callback):
    """Run a data-gathering callback; return None on DB/schema errors so the
    dashboard renders an empty real-data section instead of crashing."""
    try:
        return callback()
    except (ProgrammingError, DatabaseError):
        return None


def quality_audit_context(user):
    """Quality Assurance dashboard context (rebuilds real data only — no
    fallback to sample data). This is the single implementation: the former
    ``tass_administrator`` (SVI Administrator) role was a duplicate of this
    one, so the old ``tass_administrator_context`` alias is gone and the
    /dashboards/tass-administrator/ route redirects here."""
    now = timezone.now()
    week_ago = now - timedelta(days=7)
    active_year = _active_academic_year()

    data = {
        "profile": _self_profile(user),
        "calendar": _calendar_payload(user),
        "academicYear": ("S.Y. {}".format(active_year.label.replace("-", "–"))) if active_year else "",
        "students": [],
        "sections": [],
        "teachers": [],
        "grades": {},
        "attendance": {},
        "assessments": [],
        "activityLog": [],
    }

    # ---- Students (from active-year student enrollments) ----
    section_lookup = {}
    data["students"] = _qa_guarded(lambda: _qa_real_students(active_year, section_lookup)) or []

    # ---- Sections (active, with adviser) ----
    data["sections"] = _qa_guarded(lambda: _qa_real_sections(active_year)) or []

    # ---- Teachers (activity + class load from real VCs) ----
    data["teachers"] = _qa_guarded(lambda: _qa_real_teachers(active_year, now, week_ago)) or []

    # ---- Grades (real StudentGrade rows grouped by section) ----
    data["grades"] = _qa_guarded(lambda: _qa_real_grades(active_year, section_lookup)) or {}

    # ---- Attendance (real attendance records grouped by section) ----
    data["attendance"] = _qa_guarded(_qa_real_attendance) or {}

    # ---- Assessments (real assignment aggregates) ----
    data["assessments"] = _qa_guarded(lambda: _qa_real_assessments(active_year)) or []

    # ---- Activity log (real) ----
    data["activityLog"] = _qa_guarded(_qa_real_logs) or []

    # ---- Academic standing per learner (drives the standing table) ----
    data["standing"] = _qa_guarded(
        lambda: academic_standing_rows(active_year, section_lookup)
    ) or []

    # Reuse the read-only roster directory so the Quality Assurance Students,
    # Sections and Subjects panels list the same rows as the Principal's.
    try:
        roster_directory, roster_students = _roster_directory(active_year)
    except (ProgrammingError, DatabaseError):
        roster_directory, roster_students = {}, []
    data["directory"] = roster_directory
    data["rosterStudents"] = roster_students

    return {
        "qa_academic_year": data["academicYear"],
        "qa_data_json": json.dumps(data),
        "qa_data": data,
        "student_count": len(data["students"]),
        "teacher_count": len(data["teachers"]),
        "section_count": len(data["sections"]),
    }


def _qa_real_students(active_year, section_lookup):
    enrollments = StudentEnrollment.objects.filter(status=StudentEnrollment.STATUS_ENROLLED)
    if active_year:
        enrollments = enrollments.filter(academic_year=active_year)
    enrollments = order_students(
        enrollments.select_related("student__user", "section"),
        prefix="student__",
    )
    real_students = []
    for enr in enrollments:
        prof = enr.student
        section_lookup.setdefault(
            prof.id, {"grade": _qa_grade_label(enr.section.grade_level), "section": enr.section.name}
        )
        real_students.append(
            {
                "last": prof.last_name,
                "first": prof.first_name,
                "middle": prof.middle_initial,
                "lrn": prof.lrn,
                "gender": "Male" if prof.sex == "M" else ("Female" if prof.sex == "F" else ""),
                "birth": prof.birthdate.isoformat(),
                "grade": _qa_grade_label(enr.section.grade_level),
                "section": enr.section.name,
                "status": "Enrolled",
            }
        )
    return real_students


def _qa_real_sections(active_year):
    sections = Section.objects.filter(status=Section.STATUS_ACTIVE).select_related("adviser")
    if active_year:
        sections = sections.filter(academic_year=active_year)
    real_sections = [
        {
            "grade": _qa_grade_label(s.grade_level),
            "name": s.name,
            "adviser": _qa_display_name(s.adviser.employee_profile)
            if s.adviser
            and getattr(s.adviser, "employee_profile", None)
            and s.adviser.status != "archived"
            else "Unassigned",
        }
        for s in sections.order_by("grade_level", "name")
    ]
    return real_sections


def _qa_real_teachers(active_year, now, week_ago):
    teachers_users = order_teachers(
        User.objects.filter(groups__name="teacher", status="active")
        .select_related("employee_profile")
    )
    real_teachers = []
    for teacher in teachers_users:
        prof = getattr(teacher, "employee_profile", None)
        name = (prof and _qa_display_name(prof)) or (teacher.get_full_name() or teacher.email)
        vcs = VirtualClassroom.objects.filter(teacher=teacher, status=STATUS_ACTIVE)
        if active_year:
            vcs = vcs.filter(academic_year=active_year)
        vc_ids = list(vcs.values_list("id", flat=True))
        subject_names = list(vcs.select_related("subject").values_list("subject__name", flat=True).distinct())
        learner_count = VirtualClassroomEnrollment.objects.filter(
            virtual_classroom__in=vcs, status=VirtualClassroomEnrollment.STATUS_ACTIVE
        ).values("student_id").distinct().count()
        uploads = LearningResourcePlacement.objects.filter(virtual_classroom__in=vcs).count()
        assessments = (
            AssessmentAssignment.objects.filter(virtual_classroom__in=vcs).values("assessment").distinct().count()
        )
        announcements = ClassAnnouncement.objects.filter(virtual_classroom__in=vcs).count()
        activity_count = ActivityLog.objects.filter(user=teacher, timestamp__gte=week_ago).count()
        activity = "high" if activity_count >= 2 else ("mid" if activity_count == 1 else "low")
        real_teachers.append(
            {
                "name": name,
                "subject": subject_names[0] if subject_names else "—",
                "gender": _sex_label(getattr(prof, "sex", None)) if prof else "",
                "age": _age_years(prof.birthdate) if prof else None,
                "tenure": 0,
                "classes": len(vc_ids),
                "students": learner_count,
                "uploads": uploads,
                "assessments": assessments,
                "announcements": announcements,
                "activity": activity,
            }
        )
    return real_teachers


def _qa_submission_grades(active_year, section_lookup):
    """Per-section, per-subject averages taken straight from graded submissions.

    ``StudentGrade`` rows only exist once a ``GradingFormula`` has been
    configured and ``compute_student_grades`` has run. Until a school sets that
    up the Performance panels had nothing to draw, even with a full set of
    graded work on file. These are the same figures without inventing a
    weighting policy: a learner's score on each subject as a percentage of the
    assessment's possible points.
    """
    submissions = Submission.objects.filter(
        total_score__isnull=False, total_score__gt=0
    )
    if active_year:
        submissions = submissions.filter(
            assessment_assignment__assessment__academic_year=active_year
        )

    # total_possible_score walks parts, so the denominators are summed once.
    possible = {
        row["assessment_id"]: row["total"]
        for row in AssessmentPart.objects.values("assessment_id").annotate(total=Sum("points"))
    }
    subject_of = dict(Assessment.objects.values_list("id", "subject__name"))
    assessment_of = dict(
        AssessmentAssignment.objects.values_list("id", "assessment_id")
    )

    per_student_subject = {}
    for assignment_id, student_id, score in submissions.values_list(
        "assessment_assignment_id", "student_id", "total_score"
    ):
        assessment_id = assessment_of.get(assignment_id)
        subject = subject_of.get(assessment_id)
        denominator = possible.get(assessment_id) or 0
        if not subject or not denominator or student_id not in section_lookup:
            continue
        pct = float(score) * 100.0 / float(denominator)
        per_student_subject.setdefault((student_id, subject), []).append(pct)
    if not per_student_subject:
        return {}

    profiles = {
        profile.pk: profile
        for profile in StudentProfile.objects.filter(
            pk__in={key[0] for key in per_student_subject}
        ).select_related("user")
    }

    groups = {}
    for (student_id, subject), values in per_student_subject.items():
        entry = section_lookup[student_id]
        key = f"{entry['grade']} - {entry['section']}"
        group = groups.setdefault(
            key, {"subjects": [], "subject_order": {}, "students": {}}
        )
        if subject not in group["subject_order"]:
            group["subject_order"][subject] = len(group["subjects"])
            group["subjects"].append(subject)
        profile = profiles.get(student_id)
        student = group["students"].setdefault(
            student_id,
            {"name": _qa_display_name(profile) if profile else "Learner", "scores": {}},
        )
        student["scores"][subject] = round(sum(values) / len(values), 2)

    return {
        key: {
            "subjects": group["subjects"],
            "students": [
                {
                    "name": student["name"],
                    "scores": [student["scores"].get(sub, 0) for sub in group["subjects"]],
                }
                for student in group["students"].values()
            ],
        }
        for key, group in groups.items()
    }


def _qa_real_grades(active_year, section_lookup):
    grades = StudentGrade.objects.select_related("student", "subject", "student__user")
    if active_year:
        grades = grades.filter(academic_year=active_year)
    if not grades.exists():
        # No grading formula has been run for this year, so fall back to the
        # per-subject averages the submissions already support.
        return _qa_submission_grades(active_year, section_lookup)
    grade_groups = {}
    for g in grades:
        sec = section_lookup.get(g.student_id)
        key = f"{sec['grade']} - {sec['section']}" if sec else "Grade — - —"
        group = grade_groups.setdefault(key, {"subjects": [], "subject_order": {}, "students": {}})
        sub = g.subject.name
        if sub not in group["subject_order"]:
            group["subject_order"][sub] = len(group["subjects"])
            group["subjects"].append(sub)
        st = group["students"].setdefault(g.student_id, {"name": _qa_display_name(g.student), "scores": {}})
        st["scores"][sub] = float(g.grade_value)
    real_grades = {}
    for key, group in grade_groups.items():
        real_grades[key] = {
            "subjects": group["subjects"],
            "students": [
                {"name": st["name"], "scores": [st["scores"].get(sub, 0) for sub in group["subjects"]]}
                for st in group["students"].values()
            ],
        }
    return real_grades


def _qa_real_attendance():
    att_groups = {}
    for rec in AttendanceRecord.objects.select_related(
        "session__virtual_classroom__section"
    ).iterator():
        section = rec.session.virtual_classroom.section
        if section is None:
            continue
        group = att_groups.setdefault(
            f"sec_{section.pk}",
            {
                "label": f"{_qa_grade_label(section.grade_level)} · {section.name}",
                "present": 0,
                "absent": 0,
                "late": 0,
            },
        )
        if rec.status in PRESENT_STATUSES:
            if rec.status == "present_late":
                group["late"] += 1
            else:
                group["present"] += 1
        else:
            group["absent"] += 1
    return dict(att_groups)


def _qa_real_assessments(active_year):
    assignments = AssessmentAssignment.objects.select_related(
        "assessment", "virtual_classroom__teacher", "virtual_classroom__section", "virtual_classroom__subject"
    )
    if active_year:
        assignments = assignments.filter(virtual_classroom__academic_year=active_year)
    real_assessments = []
    for a in assignments[:20]:
        vc = a.virtual_classroom
        subs = Submission.objects.filter(assessment_assignment=a)
        scores = [float(s.total_score) for s in subs if s.total_score is not None]
        teacher = vc.teacher.get_full_name() if vc.teacher else "—"
        class_label = f"{_qa_grade_label(vc.section.grade_level)} - {vc.section.name}" if vc.section else "—"
        real_assessments.append(
            {
                "title": a.assessment.title,
                "type": "Assessment",
                "teacher": teacher or "—",
                "class": class_label,
                "submissions": subs.count(),
                "avgScore": round(sum(scores) / len(scores), 1) if scores else 0,
                "status": "Active",
            }
        )
    return real_assessments


def _qa_real_logs():
    logs = ActivityLog.objects.select_related("user").order_by("-timestamp")[:10]
    return [
        {
            "action": log.action,
            "detail": (log.related_entity_type or "").replace("_", " ").title(),
            "time": log.timestamp.strftime("%Y-%m-%d %I:%M %p"),
        }
        for log in logs
    ]

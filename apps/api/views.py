"""
SVI LMS JSON write bridge.

Every SPA dashboard persists its state to localStorage. These endpoints give
the front-ends a real database sink that mirrors those localStorage payloads,
reconciling (upsert / soft-archive) instead of deleting, so the manuscript's
"historical records retained" rule holds everywhere.

All endpoints are login-required, POST, JSON. The front-end JS sends the same
objects it previously stored in localStorage.
"""

import json
from collections import defaultdict
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Max
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from apps.accounts.models import (
    ActivityLog,
    EmployeeProfile,
    Role,
    RoleAssignment,
    StudentProfile,
    User,
)
from apps.academic_structure.models import AcademicYear, Section, StudentEnrollment, Subject
from apps.assessments.models import Assessment, AssessmentAssignment, AssessmentPart
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.communication.models import ClassAnnouncement, SchoolAnnouncement
from apps.governance.models import RoleAccessReview
from apps.system_settings.models import BackupRecord, PasswordPolicy, SystemConfiguration
from apps.teaching_learning.models import LearningResource, LearningResourcePlacement, Topic, TopicTemplate
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

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
    "Assistant Principal": Role.Code.ASSISTANT_PRINCIPAL,
    "Academic Coordinator": Role.Code.ASSISTANT_PRINCIPAL,
    "Student": Role.Code.STUDENT,
    "SVI Administrator": Role.Code.TASS_ADMINISTRATOR,
    "TASS Admin": Role.Code.TASS_ADMINISTRATOR,
    "Quality Assurance": Role.Code.QUALITY_AUDIT,
}

MISSING_BIRTH_PLACEHOLDER = date(2005, 1, 1)


def _ok(**extra):
    return JsonResponse({"ok": True, **extra}, status=200)


def _err(message, status=400):
    return JsonResponse({"ok": False, "message": str(message)}, status=status)


def _payload(request):
    try:
        return json.loads(request.body or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _active_year():
    return AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).order_by("-start_date").first()


def _log(user, action, entity_type="", entity_id=None):
    try:
        ActivityLog.objects.create(
            user=user, action=action,
            related_entity_type=entity_type, related_entity_id=entity_id,
        )
    except Exception:
        pass


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
def accounts(request):
    year = _active_year()
    if not year:
        return _err("No active academic year configured yet.", 409)
    data = _payload(request)
    incoming = data.get("accounts") or data.get("items") or []
    if not isinstance(incoming, list):
        return _err("Expected a list under 'accounts'.")

    seen, created, updated = [], 0, 0
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
        if user is None:
            user = User.objects.create_user(email=email)  # unusable password until setup
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
            user.status = status
            user.save(update_fields=["status", "is_active"])

        role_code = ROLE_CODE_BY_LABEL.get(row.get("role") or "")
        if role_code:
            role = Role.objects.filter(code=role_code).first()
            if role:
                RoleAssignment.objects.get_or_create(user=user, role=role, academic_year=year)
        _log(request.user, f"account_saved:{email}", entity_type="User", entity_id=user.pk)

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
                RoleAssignment.objects.filter(user=user, academic_year=year).delete()
                _log(request.user, f"account_archived:{user.email}", entity_type="User", entity_id=user.pk)

    return _ok(created=created, updated=updated)


# ---------------------------------------------------------------------------
# POST /api/students/  — registrar student records reconcile
# ---------------------------------------------------------------------------
@login_required
@require_POST
def students(request):
    year = _active_year()
    if not year:
        return _err("No active academic year configured yet.", 409)
    data = _payload(request)
    incoming = data.get("students") or data.get("items") or []
    if not isinstance(incoming, list):
        return _err("Expected a list under 'students'.")

    seen, created, updated, archived = [], 0, 0, 0
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
            if not email:
                email = f"{first.lower()}.{last.lower()}@sanvicente.edu.ph"
            user = User.objects.filter(email=email).first() or User.objects.create_user(email=email)
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
            user.status = status
            user.save(update_fields=["status", "is_active"])

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
            for vc in VirtualClassroom.objects.filter(section=section, academic_year=year, status=STATUS_ACTIVE):
                if not VirtualClassroomEnrollment.objects.filter(virtual_classroom=vc, student=profile).exists():
                    VirtualClassroomEnrollment.objects.create(virtual_classroom=vc, student=profile)

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

    return _ok(created=created, updated=updated, archived=archived)


# ---------------------------------------------------------------------------
# POST /api/academic-structure/  — sections / subjects / virtual classrooms
# ---------------------------------------------------------------------------
@login_required
@require_POST
def academic_structure(request):
    year = _active_year()
    if not year:
        return _err("No active academic year configured yet.", 409)
    data = _payload(request)
    result = {"sections": 0, "subjects": 0, "topics": 0, "topics_skipped": 0, "vcs": 0, "vcs_skipped": 0}

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
        period = (row.get("period") or row.get("grading_period") or "").strip()
        if period in ("Whole Academic Year", "Whole Year", "Quarter", "Semester", "1st Quarter"):
            period = ""
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
        grade = _grade_number(row.get("grade"))
        if not subject_name or not section_name:
            result["vcs_skipped"] += 1
            continue
        subj = Subject.objects.filter(academic_year=year, name=subject_name, grade_level=grade).first()
        sec = Section.objects.filter(academic_year=year, grade_level=grade, name=section_name).first()
        if subj is None or sec is None:
            result["vcs_skipped"] += 1
            continue
        period = (row.get("gradingPeriod") or row.get("grading_period") or "").strip()
        if period in ("Trimester 1", "Trimester 2", "Trimester 3", "Quarter 1"):
            period = ""
        vc = VirtualClassroom.objects.filter(subject=subj, section=sec, academic_year=year, grading_period=period).first()
        teacher = _resolve_teacher(row.get("teacher"))
        if vc is None:
            vc = VirtualClassroom.objects.create(
                subject=subj, section=sec, academic_year=year, grading_period=period, teacher=teacher,
                theme_color=(row.get("color") or row.get("themeColor") or "#4F46E5"),
            )
        else:
            if teacher and vc.teacher_id != teacher.pk:
                vc.teacher = teacher
            color = row.get("color") or row.get("themeColor")
            if color:
                vc.theme_color = color
            active = (row.get("status") or "active") in ("active", "Active")
            vc.status = STATUS_ACTIVE if active else STATUS_ARCHIVED
            vc.save()
        result["vcs"] += 1

    return _ok(**result)


# ---------------------------------------------------------------------------
# POST /api/school-announcements/ — school-wide feed (principal)
# ---------------------------------------------------------------------------
@login_required
@require_POST
def school_announcements(request):
    data = _payload(request)
    rows = data.get("announcements")
    if rows is None:
        rows = [data]
    if not isinstance(rows, list):
        return _err("Expected a list under 'announcements'.")
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = (row.get("title") or "").strip()
        content = (row.get("content") or row.get("body") or "").strip() if title else ""
        if not title or not content:
            continue
        archived = bool(row.get("archived"))
        ann = SchoolAnnouncement.objects.filter(title=title).order_by("-posted_at").first()
        if ann is None:
            ann = SchoolAnnouncement.objects.create(
                title=title, content=content, posted_by=request.user,
                status="archived" if archived else "active",
            )
        else:
            ann.content = content
            ann.status = "archived" if archived else "active"
            ann.save()
        count += 1
        _log(request.user, f"school_announcement_saved:{title}", entity_type="SchoolAnnouncement", entity_id=ann.pk)
    return _ok(count=count)


# ---------------------------------------------------------------------------
# POST /api/class-announcements/ — classroom feed (teacher)
# ---------------------------------------------------------------------------
@login_required
@require_POST
def class_announcements(request):
    data = _payload(request)
    content = (data.get("content") or data.get("body") or "").strip()
    if not content:
        return _err("Announcement content is required.")
    vc = None
    if data.get("virtual_classroom_id"):
        vc = VirtualClassroom.objects.filter(pk=data["virtual_classroom_id"], teacher=request.user).first()
    if vc is None:
        vc = _active_vc(request.user)
    if vc is None:
        return _ok(count=0, skipped="no_virtual_classroom")
    archived = bool(data.get("archived"))
    ann = ClassAnnouncement.objects.filter(virtual_classroom=vc, content=content).first()
    if ann is None:
        ann = ClassAnnouncement.objects.create(
            virtual_classroom=vc, posted_by=request.user, content=content,
            status="archived" if archived else "active",
        )
    else:
        ann.status = "archived" if archived else "active"
        ann.save()
    _log(request.user, f"class_announcement_saved", entity_type="ClassAnnouncement", entity_id=ann.pk)
    return _ok(count=1, virtual_classroom_id=vc.pk)


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

    employee = _employee(request.user)
    if employee is not None:
        if name:
            first, mi, last = _parse_name(name)
            if first and (employee.first_name != first or employee.last_name != last):
                employee.first_name, employee.middle_initial, employee.last_name = first, mi, last
                changed = True
        if contact:
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
            if contact and student.contact_number != contact:
                student.contact_number = contact
                changed = True
            if changed:
                student.save()

    if email and email != request.user.email and not User.objects.filter(email=email).exclude(pk=request.user.pk).exists():
        request.user.email = email
        request.user.save(update_fields=["email"])
        changed = True

    _log(request.user, "profile_updated", entity_type="User", entity_id=request.user.pk)
    return _ok(changed=changed)


# ---------------------------------------------------------------------------
# POST /api/notifications/read/
# ---------------------------------------------------------------------------
@login_required
@require_POST
def notifications_read(request):
    data = _payload(request)
    from apps.assessments.models import AssessmentNotification

    profile = _student(request.user)
    qs = AssessmentNotification.objects.all()
    if profile is not None:
        qs = qs.filter(student=profile)
    if isinstance(data.get("ids"), list) and data["ids"]:
        qs = qs.filter(pk__in=[int(i) for i in data["ids"] if str(i).isdigit()])
    count = qs.filter(is_read=False).update(is_read=True)
    return _ok(count=count)


# ---------------------------------------------------------------------------
# POST /api/teacher-content/ — topics + learning resources for a classroom
# ---------------------------------------------------------------------------
@login_required
@require_POST
def teacher_content(request):
    data = _payload(request)
    vc = None
    if data.get("virtual_classroom_id"):
        vc = VirtualClassroom.objects.filter(pk=data["virtual_classroom_id"], teacher=request.user).first()
    if vc is None:
        vc = _active_vc(request.user)
    if vc is None:
        return _ok(count=0, skipped="no_virtual_classroom")

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
        resource = LearningResource.objects.filter(
            title=title, uploaded_by=request.user, external_link=link
        ).first()
        if resource is None:
            resource = LearningResource.objects.create(
                subject=vc.subject, academic_year=vc.academic_year, uploaded_by=request.user,
                title=title, description=(data.get("description") or ""),
                resource_type=rtype, external_link=link,
            )
        saved["resource_id"] = resource.pk
        if data.get("topic_id"):
            topic = Topic.objects.filter(pk=data["topic_id"], virtual_classroom=vc).first()
        if topic is None:
            topic = Topic.objects.filter(virtual_classroom=vc).first()
        if topic is not None:
            LearningResourcePlacement.objects.get_or_create(
                resource=resource, virtual_classroom=vc, topic=topic, placed_by=request.user
            )

    return _ok(**saved)


# ---------------------------------------------------------------------------
# POST /api/attendance/ — record a session + per-student statuses
# ---------------------------------------------------------------------------
@login_required
@require_POST
def attendance(request):
    data = _payload(request)
    vc = None
    if data.get("virtual_classroom_id"):
        vc = VirtualClassroom.objects.filter(pk=data["virtual_classroom_id"], teacher=request.user).first()
    if vc is None:
        vc = _active_vc(request.user)
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
        status = status_map.get(status_raw.lower(), AttendanceRecord.PRESENT_ON_TIME)
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
    _log(request.user, f"attendance_saved:{session_date}", entity_type="AttendanceSession", entity_id=session.pk)
    return _ok(count=count, skipped=skipped, virtual_classroom_id=vc.pk, session_date=str(session_date))


# ---------------------------------------------------------------------------
# POST /api/assessments/ — teacher assessment builder publish
# ---------------------------------------------------------------------------
@login_required
@require_POST
def assessments(request):
    data = _payload(request)
    title = (data.get("title") or "").strip()
    if not title:
        return _err("Assessment title is required.")
    vc = None
    if data.get("virtual_classroom_id"):
        vc = VirtualClassroom.objects.filter(pk=data["virtual_classroom_id"], teacher=request.user).first()
    if vc is None:
        vc = _active_vc(request.user)
    if vc is None:
        return _ok(count=0, skipped="no_virtual_classroom")

    method_map = {
        "Quiz": Assessment.METHOD_QUIZ,
        "Seatwork": Assessment.METHOD_SEATWORK,
        "Assignment": Assessment.METHOD_ASSIGNMENT,
        "Examination": Assessment.METHOD_EXAMINATION,
        "Project": Assessment.METHOD_PROJECT,
    }
    type_map = {
        "Quiz": Assessment.TYPE_WRITTEN_ORAL,
        "Seatwork": Assessment.TYPE_WRITTEN_ORAL,
        "Assignment": Assessment.TYPE_PRODUCT_PERFORMANCE,
        "Examination": Assessment.TYPE_SUMMATIVE_TEST,
        "Project": Assessment.TYPE_PRODUCT_PERFORMANCE,
    }
    a_label = str(data.get("type") or "Quiz")
    method = method_map.get(a_label, method_map["Quiz"])
    a_type = type_map.get(a_label, Assessment.TYPE_WRITTEN_ORAL)

    assessment = Assessment.objects.filter(subject=vc.subject, created_by=request.user, title=title).order_by("-created_at").first()
    if assessment is None:
        assessment = Assessment.objects.create(
            subject=vc.subject, academic_year=vc.academic_year, created_by=request.user, title=title,
            assessment_method=method, assessment_type=a_type,
            instructions=(data.get("instructions") or ""),
            submission_deadline=timezone.now() + timedelta(days=1),
            lifecycle_stage=Assessment.STAGE_CREATED,
        )

    AssessmentAssignment.objects.get_or_create(assessment=assessment, virtual_classroom=vc, assigned_by=request.user)

    questions = data.get("questions") or []
    if questions and not assessment.parts.exists():
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
        grouped = defaultdict(list)
        for q in questions:
            if not isinstance(q, dict):
                continue
            qtext = str(q.get("text") or q.get("question") or "").strip()
            if not qtext:
                continue
            raw_type = str(q.get("type") or "mc").lower()
            qtype = type_code.get(raw_type, AssessmentPart.TYPE_MULTIPLE_CHOICE)
            points = int(q.get("points") or q.get("point") or 1)
            options = q.get("options") or []
            item = {"question": qtext, "point": points}
            if qtype == AssessmentPart.TYPE_MULTIPLE_CHOICE and options:
                options = [str(o) for o in options if str(o).strip() != ""]
                item["choices"] = options
                correct = q.get("correct") or 0
                idx = int(correct) if str(correct).isdigit() and int(correct) < len(options) else 0
                item["correct_answer"] = options[idx] if options else qtext
            elif qtype == AssessmentPart.TYPE_TRUE_FALSE:
                item["choices"] = ["True", "False"]
                tf = q.get("tf")
                item["correct_answer"] = ("False" if tf in (False, "False", "false") else "True") if tf is not None else "True"
            elif qtype in (AssessmentPart.TYPE_IDENTIFICATION, AssessmentPart.TYPE_SHORT_ANSWER):
                answers = q.get("answers") or q.get("accepted_answers") or []
                if not answers:
                    for key in ("idAns", "saAns"):
                        if q.get(key):
                            answers = [q[key]]
                            break
                if not answers:
                    answers = [qtext]
                item["accepted_answers"] = answers
            elif qtype == AssessmentPart.TYPE_NUMERIC:
                item["correct_answer"] = str(q.get("answer") or q.get("correct_answer") or "0")
                item["tolerance"] = int(q.get("tolerance") or 0)
            elif qtype == AssessmentPart.TYPE_MATCHING:
                premises = q.get("premises") or []
                targets = q.get("targets") or []
                matches = q.get("matches") or {}
                item.update({
                    "premises": premises,
                    "targets": targets,
                    "correct_matches": matches,
                })
            else:
                item["grading_rubric"] = q.get("rubric") or []
            grouped[qtype].append((len(grouped[qtype]) + 1, item))

        for qtype, pairs in grouped.items():
            items = {f"item_{i}": item for i, item in pairs}
            try:
                AssessmentPart.objects.create(
                    assessment=assessment, question_type=qtype, order=assessment.parts.count(), items=items
                )
            except ValidationError:
                continue

    _log(request.user, f"assessment_saved:{title}", entity_type="Assessment", entity_id=assessment.pk)
    return _ok(assessment_id=assessment.pk)


# ---------------------------------------------------------------------------
# POST /api/system-settings/ — timeouts, registration, maintenance, backup prefs
# ---------------------------------------------------------------------------
@login_required
@require_POST
def system_settings(request):
    data = _payload(request)
    cfg = SystemConfiguration.get_solo()
    changed = False

    def _int(key, lo, hi):
        nonlocal changed
        if data.get(key) is not None:
            try:
                value = int(data[key])
            except (TypeError, ValueError):
                return
            if lo <= value <= hi:
                setattr(cfg, {
                    "sessionTimeout": "session_timeout_minutes",
                    "lockoutThreshold": "account_lockout_threshold",
                }.get(key, ""), value)
                changed = True

    _int("sessionTimeout", 1, 120)
    _int("lockoutThreshold", 1, 20)

    if data.get("timezone"):
        cfg.timezone = data["timezone"]
        changed = True
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


@login_required
@require_POST
def backup(request):
    data = _payload(request)
    if data.get("action") == "run":
        record = BackupRecord.objects.create(
            backup_type=BackupRecord.TYPE_MANUAL, status=BackupRecord.STATUS_COMPLETED,
            initiated_by=request.user, file_reference=data.get("file_reference") or f"auto-{timezone.now():%Y%m%d-%H%M%S}",
        )
        _log(request.user, "manual_backup_completed", entity_type="BackupRecord", entity_id=record.pk)
        return _ok(backup_id=record.pk)
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
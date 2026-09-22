import json
from django.contrib.auth.models import Group
from django.utils import timezone
from datetime import timedelta
from django.db.models import Avg, Count, Q
from django.db.utils import DatabaseError, ProgrammingError

from apps.accounts.models import ActivityLog, EmployeeProfile, LoginSession, StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section, StudentEnrollment, Subject
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment
from apps.teaching_learning.models import LearningResourcePlacement, Topic
from apps.assessments.models import (
    Assessment,
    AssessmentAssignment,
    StudentGrade,
    Submission,
)
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.communication.models import ClassAnnouncement, SchoolAnnouncement
from apps.governance.models import RoleAccessReview
from apps.system_settings.models import BackupRecord
from apps.analytics.models import GeneratedReport

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

PRESENT_STATUSES = ("present_on_time", "present_late")


def _non_archived_users():
    return User.objects.exclude(status="archived")


def _role_user_count(role_code):
    return User.objects.filter(groups__name=role_code).count()


def _active_academic_year():
    return (
        AcademicYear.objects.filter(status="ACTIVE")
        .order_by("-start_date")
        .first()
    )


def _stat(label, value, icon):
    return {"label": label, "value": value, "icon": icon}


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
            "assistant_principal", "tass_administrator", "quality_audit", "student",
        )
    }
    locked_accounts = (
        User.objects.filter(status="locked").select_related("employee_profile")[:6]
    )
    user_accounts = []
    for u in (
        User.objects.exclude(status="archived")
        .select_related("employee_profile", "student_profile")
        .order_by("date_created")
    ):
        role = "Student" if hasattr(u, "student_profile") else (
            u.role_assignments.filter(academic_year__status="ACTIVE").select_related("role").last()
        )
        role_label = role.role.get_code_display() if role and hasattr(role, "role") else (
            "Student" if hasattr(u, "student_profile") else "Unassigned"
        )
        employee = getattr(u, "employee_profile", None)
        emp_no = getattr(employee, "employee_number", None) or (
            getattr(getattr(u, "student_profile", None), "lrn", None) if hasattr(u, "student_profile") else ""
        )
        user_accounts.append({
            "user": u,
            "name": u.get_full_name(),
            "emp_no": emp_no or "",
            "email": u.email,
            "role": role_label,
            "status": u.get_status_display(),
            "last": u.password_changed_at or u.date_created,
        })
    stats = [
        _stat("Total Accounts", _non_archived_users().count(), "fa-users"),
        _stat("Students", _role_user_count("student"), "fa-user-graduate"),
        _stat("Teachers", _role_user_count("teacher"), "fa-chalkboard-user"),
        _stat("Pending Verification", User.objects.filter(status="pending_verification").count(), "fa-envelope"),
        _stat("Password Setup", User.objects.filter(status="pending_password_setup").count(), "fa-key"),
        _stat("Locked Accounts", User.objects.filter(status="locked").count(), "fa-lock"),
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
                "name": a["name"],
                "emp": a["emp_no"],
                "email": a["email"],
                "role": a["role"],
                "initials": u.get_initials(),
                "gradient": account_styles[idx % len(account_styles)],
                "status": a["status"],
                "last": last,
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

    locked_json = [
        {
            "name": lu.get_full_name() or lu.email,
            "email": lu.email,
            "reason": "Account locked by administrator",
            "when": timezone.localtime(lu.password_changed_at).strftime("%I:%M %p · %b %d, %Y") if lu.password_changed_at else "Recently locked",
            "initials": lu.get_initials(),
            "gradient": account_styles[i % len(account_styles)],
        }
        for i, lu in enumerate(locked_accounts)
    ]

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

    dashboard_json = {
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
        "sessionCount": LoginSession.objects.filter(is_current=True).count() or active_sessions.count(),
        "activity": activity_json,
        "userDist": [
            {"label": "Students", "value": role_counts["student"]},
            {"label": "Teachers", "value": role_counts["teacher"]},
            {"label": "Registrars", "value": role_counts["registrar"]},
            {"label": "Principals", "value": role_counts["principal"] + role_counts["assistant_principal"]},
            {"label": "TASS Admins", "value": role_counts["tass_administrator"] + role_counts["quality_audit"] + role_counts["system_admin"]},
        ],
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
        "academic_years": AcademicYear.objects.order_by("-start_date").values_list("label", flat=True)[:5] or ["2025-2026"],
        "active_sessions": active_sessions,
        "role_counts": role_counts,
        "locked_accounts": locked_accounts,
        "user_accounts": user_accounts,
        "user_dist": [
            {"label": "Students", "value": role_counts["student"]},
            {"label": "Teachers", "value": role_counts["teacher"]},
            {"label": "Registrars", "value": role_counts["registrar"]},
            {"label": "System Admins", "value": role_counts["system_admin"]},
            {"label": "Others", "value": role_counts["principal"] + role_counts["assistant_principal"] + role_counts["tass_administrator"] + role_counts["quality_audit"]},
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
    year_label = "S.Y. {}".format(ay_label.replace("-", "–")) if ay_label else "S.Y. 2025–2026"

    students_json = []
    for prof in StudentProfile.objects.select_related("user").prefetch_related("enrollments__section").order_by("last_name"):
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
                "grade": grade or "Grade 7",
                "section": section or "—",
                "guardian": prof.guardian_name or "",
                "email": u_email,
                "status": "Enrolled",
                "account": account,
                "acctUsername": u_email,
                "tempPass": "",
                "accountVerified": False,
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
                "ay": "S.Y. {}".format((sec.academic_year.label.replace("-", "–")) if sec.academic_year else "2025–2026"),
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
                "grade": "Grade {}".format(subj.grade_level) if subj.grade_level else "Grade 7",
                "type": "Applied" if getattr(subj, "subject_type", "") in ("CORE", "APPLIED", "core", "applied") else "Elective",
                "period": subj.grading_period or "Whole Academic Year",
                "status": "Active" if subj.status == Subject.STATUS_ACTIVE else "Archived",
                "ay": "S.Y. {}".format((subj.academic_year.label.replace("-", "–")) if subj.academic_year else "2025–2026"),
            }
        )

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
                "ay": "S.Y. {}".format((vc.academic_year.label.replace("-", "–")) if vc.academic_year else "2025–2026"),
                "students": [],
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
    dashboard_json = {
        "yearLabel": year_label,
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


def _class_counts(vc):
    topics = Topic.objects.filter(virtual_classroom=vc).count()
    resources = LearningResourcePlacement.objects.filter(virtual_classroom=vc).count()
    assessments = (
        AssessmentAssignment.objects.filter(virtual_classroom=vc).values("assessment").distinct().count()
    )
    return topics, resources, assessments


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

    class_announcements = (
        ClassAnnouncement.objects.filter(
            virtual_classroom__in=my_classes, status=STATUS_ACTIVE
        )
        .select_related("virtual_classroom")
        .order_by("-posted_at")[:5]
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
    ).order_by("-assigned_at")[:40]

    assessments_json = []
    for aa in assignments:
        a = aa.assessment
        lifecycle = (
            "completed"
            if a.lifecycle_stage == Assessment.STAGE_COMPLETED
            else ("grading" if a.lifecycle_stage == Assessment.STAGE_GRADING else None)
        )
        vc = aa.virtual_classroom
        assessments_json.append(
            {
                "id": a.id,
                "title": a.title,
                "type": a.get_assessment_method_display() or a.assessment_method,
                "openDate": _dt(a.available_at),
                "deadline": _dt(a.submission_deadline),
                "archived": a.status == STATUS_ARCHIVED,
                "subject": (a.subject.name if a.subject else (vc.subject.name if vc.subject else "")),
                "cls": vc.section.name if vc.section else "",
                "lifecycle": lifecycle,
                "hasManual": False,
            }
        )

    announcements_json = [
        {
            "id": ca.id,
            "title": (ca.content[:90] if ca.content else "Class announcement"),
            "body": ca.content or "",
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

    dashboard_json = {
        "classes": classes_json,
        "stats": {
            "classes": len(class_items),
            "students": sum(i["students_count"] for i in class_items),
            "openAssessments": open_assessments,
            "attendance": attendance_rate,
        },
        "assessments": assessments_json,
        "notifications": notifications_json,
        "announcements": announcements_json,
        "activity": activity_json,
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


def _newest_announcements():
    school = SchoolAnnouncement.objects.filter(status=STATUS_ACTIVE).order_by("-posted_at")[:3]
    class_ann = ClassAnnouncement.objects.filter(status=STATUS_ACTIVE).order_by("-posted_at")[:3]
    return {"school": school, "class": class_ann}


def principal_context(user):
    active_year = _active_academic_year()
    current = active_year.id if active_year else None
    vcs = VirtualClassroom.objects.filter(status=STATUS_ACTIVE)
    sections = Section.objects.filter(status=STATUS_ACTIVE)
    subjects = Subject.objects.filter(status=STATUS_ACTIVE)
    if current:
        vcs = vcs.filter(academic_year_id=current)
        sections = sections.filter(academic_year_id=current)
        subjects = subjects.filter(academic_year_id=current)

    records = AttendanceRecord.objects.all()
    total_records = records.count()
    present_records = records.filter(status__in=PRESENT_STATUSES).count()

    stats = [
        _stat("Students", _role_user_count("student"), "fa-user-graduate"),
        _stat("Teachers", _role_user_count("teacher"), "fa-chalkboard-user"),
        _stat("Active Sections", sections.count(), "fa-users-between-lines"),
        _stat("Active Subjects", subjects.count(), "fa-book-open"),
        _stat("Virtual Classrooms", vcs.count(), "fa-chalkboard-user"),
        _stat("Attendance Present", f"{round(present_records / total_records * 100, 1) if total_records else 0}%", "fa-clipboard-check"),
    ]
    active_assessments = Assessment.objects.filter(status=STATUS_ACTIVE)
    if current:
        active_assessments = active_assessments.filter(academic_year_id=current)
    all_submissions = Submission.objects.count()
    graded_submissions = Submission.objects.exclude(total_score=None).count()
    attendance_test = round(present_records / total_records * 100, 1) if total_records else 0
    submission_test = round(graded_submissions / all_submissions * 100, 1) if all_submissions else 0

    # ── Real-data payload for the new front-end dashboard ─────────────────
    announcements = _newest_announcements()
    palette = ["#1e5faf", "#16a34a", "#e11d48", "#f59e0b", "#7c3aed", "#0891b2", "#14b8a6", "#d97706"]
    grad_list = [
        "linear-gradient(135deg,#1e5faf,#3a86d4)",
        "linear-gradient(135deg,#16a34a,#4ade80)",
        "linear-gradient(135deg,#e11d48,#f43f5e)",
        "linear-gradient(135deg,#f59e0b,#fbbf24)",
        "linear-gradient(135deg,#7c3aed,#a78bfa)",
        "linear-gradient(135deg,#0891b2,#22d3ee)",
    ]

    teachers_qs = User.objects.filter(groups__name="teacher", status="active").select_related("employee_profile")
    teachers_json = []
    for idx, t in enumerate(teachers_qs):
        t_vcs = VirtualClassroom.objects.filter(teacher=t, status=STATUS_ACTIVE)
        if current:
            t_vcs = t_vcs.filter(academic_year_id=current)
        t_classes = t_vcs.count()
        t_students = (
            VirtualClassroomEnrollment.objects.filter(
                virtual_classroom__in=t_vcs, status=VirtualClassroomEnrollment.STATUS_ACTIVE
            ).values("student_id").distinct().count()
        )
        t_uploads = LearningResourcePlacement.objects.filter(placed_by=t).count()
        t_assess = AssessmentAssignment.objects.filter(virtual_classroom__in=t_vcs).count()
        t_ann = ClassAnnouncement.objects.filter(virtual_classroom__in=t_vcs).count()
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
        teachers_json.append(
            {
                "id": "t{}".format(t.id),
                "name": name,
                "initials": initials,
                "subject": subj or "Teacher",
                "classes": t_classes,
                "students": t_students,
                "uploads": t_uploads,
                "assessments": t_assess,
                "announcements": t_ann,
                "freq": freq,
                "grad": grad_list[idx % len(grad_list)],
                "bars": [min(b, 5) for b in bars],
            }
        )
    if not teachers_json:
        teachers_json = [
            {"id": "lm", "name": "Ms. Lovelyn C. Malanot", "initials": "LM", "subject": "Science", "classes": 4, "students": 120, "uploads": 32, "assessments": 8, "announcements": 15, "freq": "high", "grad": grad_list[0], "bars": [3, 4, 2, 5, 3, 1, 0]},
        ]

    classes_json = []
    for idx, vc in enumerate(
        vcs.select_related("subject", "section", "teacher").order_by("subject__name")[:40]
    ):
        vc_students = (
            vc.enrollments.filter(status=VirtualClassroomEnrollment.STATUS_ACTIVE)
            .values("student_id").distinct().count()
        )
        vc_assess = AssessmentAssignment.objects.filter(virtual_classroom=vc).count()
        grade_lbl = ("Grade {}".format(vc.subject.grade_level)) if vc.subject and vc.subject.grade_level else "Grade 7"
        tname = vc.teacher.get_full_name() if vc.teacher else "TASS Faculty"
        classes_json.append(
            {
                "title": "{} – {}".format(grade_lbl, vc.subject.name if vc.subject else "Class"),
                "section": vc.section.name if vc.section else "",
                "teacher": tname,
                "students": vc_students,
                "assess": vc_assess,
                "grad": grad_list[idx % len(grad_list)],
            }
        )

    type_icon = {
        "pdf": {"icon": "fa-solid fa-file-pdf", "cls": "pdf"},
        "presentation": {"icon": "fa-solid fa-file-powerpoint", "cls": "ppt"},
        "document": {"icon": "fa-solid fa-file-word", "cls": "doc"},
        "video": {"icon": "fa-solid fa-video", "cls": "vid"},
        "image": {"icon": "fa-solid fa-file-image", "cls": "img"},
        "audio": {"icon": "fa-solid fa-file-audio", "cls": "aud"},
        "link": {"icon": "fa-solid fa-link", "cls": "lnk"},
    }
    materials_json = []
    for pl in (
        LearningResourcePlacement.objects.select_related(
            "resource", "virtual_classroom__section", "placed_by"
        ).order_by("-placed_at")[:12]
    ):
        res = pl.resource
        r_type = res.resource_type if res else "document"
        m_meta = dict(res.RESOURCE_TYPE_CHOICES).get(r_type, "File") if res else "File"
        t_meta = pl.placed_by.get_full_name() if pl.placed_by else "Faculty"
        clabel = ""
        if pl.virtual_classroom and pl.virtual_classroom.section:
            s = pl.virtual_classroom.section
            clabel = "G{}-{}".format(s.grade_level, s.name)
        materials_json.append(
            {
                "type": (type_icon.get(r_type) or {}).get("cls", "doc"),
                "icon": (type_icon.get(r_type) or {}).get("icon", "fa-solid fa-file"),
                "name": res.title[:60] if res else "Learning Resource",
                "meta": "{} · {}".format(m_meta, (pl.resource.file.name.split(".")[-1].upper() if pl.resource and pl.resource.file and "." in pl.resource.file.name else "Online")),
                "teacher": "".join([p[0].upper() for p in t_meta.replace(".", " ").split() if p][:2]),
                "cls": clabel,
                "date": timezone.localtime(pl.placed_at).strftime("%b %d") if hasattr(pl, "placed_at") else "",
                "dl": 0,
            }
        )

    assessments_json = []
    for aa in (
        AssessmentAssignment.objects.select_related(
            "assessment", "virtual_classroom__section", "virtual_classroom__subject", "virtual_classroom__teacher"
        ).order_by("-assigned_at")[:12]
    ):
        a = aa.assessment
        subs = Submission.objects.filter(assessment_assignment=aa).count()
        avg = Submission.objects.filter(assessment_assignment=aa).exclude(total_score=None).aggregate(a=Avg("total_score"))["a"]
        assessments_json.append(
            {
                "title": a.title if a else "Assessment",
                "type": (a.get_assessment_method_display() if a else "Quiz"),
                "teacher": "".join([p[0].upper() for p in (aa.virtual_classroom.teacher.get_full_name() if aa.virtual_classroom and aa.virtual_classroom.teacher else "Faculty").replace(".", " ").split() if p][:2]),
                "cls": (aa.virtual_classroom.section.name if aa.virtual_classroom and aa.virtual_classroom.section else "—"),
                "deadline": timezone.localtime(a.submission_deadline).strftime("%b %d") if a and a.submission_deadline else "—",
                "subs": subs,
                "avg": round(avg, 1) if avg else 0,
                "status": "Graded" if subs and (a is None or a.lifecycle_stage == Assessment.STAGE_COMPLETED) else "Pending",
            }
        )

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
    for log in ActivityLog.objects.select_related("user").order_by("-timestamp")[:8]:
        act = log.action.lower() if log.action else ""
        if "upload" in act or "resource" in act or "topic" in act:
            ltype = "upload"
        elif "assess" in act or "grad" in act or "score" in act:
            ltype = "assess"
        elif "attend" in act:
            ltype = "attend"
        else:
            ltype = "upload"
        uname = log.user.get_full_name() if log.user else "System"
        activity_json.append(
            {
                "type": ltype,
                "teacher": uname,
                "cls": "—",
                "detail": log.action[:70] if log.action else "System event",
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

    dashboard_json = {
        "yearLabel": ("S.Y. {}".format(active_year.label)) if active_year else "S.Y. 2025-2026",
        "stats": {
            "teachers": len(teachers_json),
            "students": _role_user_count("student"),
            "materials": LearningResourcePlacement.objects.count(),
            "assessments": active_assessments.count(),
            "attendanceRate": attendance_test,
            "submissionRate": submission_test,
            "classes": vcs.count(),
            "lowActivity": User.objects.filter(groups__name="teacher", status="active").exclude(
                activity_logs__timestamp__gte=timezone.now() - timedelta(days=7)
            ).count(),
        },
        "teachers": teachers_json,
        "classes": classes_json,
        "materials": materials_json,
        "assessments": assessments_json,
        "attendance": attendance_rows_json,
        "activity": activity_json,
        "announcements": ann_json,
    }

    return {
        "stats": stats,
        "active_year": active_year,
        "announcements": announcements,
        "recent_reports": GeneratedReport.objects.order_by("-generated_at")[:5],
        "recent_activity": ActivityLog.objects.select_related("user").order_by("-timestamp")[:8],
        "teacher_count": _role_user_count("teacher"),
        "student_count": _role_user_count("student"),
        "materials_count": LearningResourcePlacement.objects.count(),
        "active_assessments": active_assessments.count(),
        "attendance_rate": attendance_test,
        "submission_rate": submission_test,
        "active_classes": vcs.count(),
        "low_activity_count": User.objects.filter(
            groups__name="teacher", status="active"
        ).exclude(
            activity_logs__timestamp__gte=timezone.now() - timedelta(days=7)
        ).count(),
        "dashboard_json": dashboard_json,
    }


def assistant_principal_context(user):
    active_year = _active_academic_year()
    current = active_year.id if active_year else None
    sections = Section.objects.filter(status=Section.STATUS_ACTIVE)
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
            ("Grade {}".format(vc.subject.grade_level)) if vc.subject and vc.subject.grade_level else "Grade 7"
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
                "teacher": vc.teacher.get_full_name() if vc.teacher else "TASS Faculty",
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
            "grade": "Grade {}".format(subj.grade_level) if subj.grade_level else "Grade 7",
            "type": "Elective" if subj.subject_type == getattr(Subject, "TYPE_ELECTIVE", "elective") else "Core",
            "vcs": VirtualClassroom.objects.filter(subject=subj, status=STATUS_ACTIVE).count(),
            "status": "active" if subj.status == Subject.STATUS_ACTIVE else "archived",
        }
        for subj in subjects.select_related("academic_year").order_by("grade_level", "name")[:60]
    ]

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
                "ay": "S.Y. {}".format(enr.virtual_classroom.academic_year.label if enr.virtual_classroom and enr.virtual_classroom.academic_year else "2025-2026"),
                "enrolledAt": timezone.localtime(enr.enrolled_at).strftime("%Y-%m-%d %H:%M") if enr.enrolled_at else "",
            }
        )

    dashboard_json = {
        "yearLabel": ("S.Y. {}".format(active_year.label)) if active_year else "S.Y. 2025-2026",
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
        "enrolled": enrolled_json,
        "activity": [
            {
                "action": log.action.title() if log.action else "System event",
                "detail": "{} performed {}".format(
                    log.user.get_full_name() if log.user else "System",
                    log.action if log.action else "an action",
                ),
                "time": timezone.localtime(log.timestamp).strftime("%b %d, %Y, %I:%M %p") if log.timestamp else "—",
            }
            for log in ActivityLog.objects.select_related("user").order_by("-timestamp")[:8]
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

    class_ann = (
        ClassAnnouncement.objects.filter(virtual_classroom__in=my_vcs, status=STATUS_ACTIVE)
        .order_by("-posted_at")[:3]
    )
    school_ann = SchoolAnnouncement.objects.filter(status=STATUS_ACTIVE).order_by("-posted_at")[:3]

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
        grade_label = ("Grade " + str(grade)) if grade else "Grade 9"
        title = _subj_label(subj)
        teacher_name = vc.teacher.get_full_name() if vc.teacher else "TASS Faculty"
        subjects_json.append(
            {
                "title": title,
                "teacher": teacher_name,
                "sub": "{} – {}".format(grade_label.replace("Grade ", ""), vc.section.name if vc.section else "Class"),
                "color": vc.theme_color or palette[idx % len(palette)],
                "grade": grade or 9,
                "year": "S.Y. {}".format(vc.academic_year.label if vc.academic_year else "2025-2026"),
            }
        )

    assignments_all = AssessmentAssignment.objects.filter(
        virtual_classroom__in=my_vcs, assessment__status=STATUS_ACTIVE
    ).select_related(
        "assessment", "assessment__subject", "virtual_classroom", "virtual_classroom__section", "virtual_classroom__subject"
    ).order_by("assessment__submission_deadline")

    assessments_json = []
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
        elif dl and dl < now:
            status = "overdue"
        elif a.lifecycle_stage == Assessment.STAGE_COMPLETED:
            status = "closed"
        elif dl:
            status = "scheduled"
        else:
            status = "open"
        assessments_json.append(
            {
                "key": "assess-{}".format(aa.id),
                "title": a.title,
                "subject": subject_lbl.strip(),
                "type": a.get_assessment_method_display().lower() or "assignment",
                "pts": int(a.total_possible_score or 0),
                "deadline": _dt(dl) or "Open",
                "status": status,
                "period": vc.grading_period or "Quarter 1",
                "instr": a.instructions or "",
            }
        )

    scores_json = []
    grade_lookup = {g.subject_id: g for g in grades}
    for subj_id, g in grade_lookup.items():
        subj = g.subject
        v = float(g.grade_value)
        subject_lbl = _subj_label(subj)
        scores_json.append(
            {
                "key": "grade-{}".format(g.id),
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
            "subject": _subj_label(ca.virtual_classroom.subject) if ca.virtual_classroom.subject else "Class",
            "title": (ca.content[:60] if ca.content else "Class announcement"),
            "body": ca.content or "",
            "by": (ca.posted_by.get_full_name() if ca.posted_by else "Teacher"),
            "date": _tod(ca.posted_at),
        }
        for ca in class_ann
    ]

    school_ann_json = [
        {
            "id": "school-{}".format(sa.id),
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
    if school_ann.exists():
        first = school_ann.first()
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
        "subjects": subjects_json,
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
def _qa_demo_data():
    return {
        "academicYear": "S.Y. 2025-2026",
        "students": [
            {"last": "Santos", "first": "Maria", "middle": "Dela Cruz", "lrn": "136100100001", "gender": "Female", "birth": "2012-05-14", "grade": "Grade 7", "section": "Section A", "status": "Enrolled"},
            {"last": "Reyes", "first": "Juan", "middle": "Ramos", "lrn": "136100100002", "gender": "Male", "birth": "2012-03-21", "grade": "Grade 7", "section": "Section A", "status": "Enrolled"},
            {"last": "Garcia", "first": "Ana", "middle": "Reyes", "lrn": "136100100003", "gender": "Female", "birth": "2011-09-08", "grade": "Grade 8", "section": "Section B", "status": "Enrolled"},
            {"last": "Mendoza", "first": "Pedro", "middle": "Santos", "lrn": "136100100004", "gender": "Male", "birth": "2011-12-02", "grade": "Grade 8", "section": "Section B", "status": "Enrolled"},
            {"last": "Lopez", "first": "Liza", "middle": "Cruz", "lrn": "136100100005", "gender": "Female", "birth": "2010-06-25", "grade": "Grade 9", "section": "Section C", "status": "Enrolled"},
            {"last": "Torres", "first": "Miguel", "middle": "Diaz", "lrn": "136100100006", "gender": "Male", "birth": "2010-01-30", "grade": "Grade 9", "section": "Section C", "status": "Enrolled"},
            {"last": "Villanueva", "first": "Sofia", "middle": "Mercado", "lrn": "136100100007", "gender": "Female", "birth": "2009-11-11", "grade": "Grade 10", "section": "Section D", "status": "Enrolled"},
            {"last": "Castro", "first": "Andres", "middle": "Bautista", "lrn": "136100100008", "gender": "Male", "birth": "2009-04-17", "grade": "Grade 10", "section": "Section D", "status": "Enrolled"},
            {"last": "Ramos", "first": "Bianca", "middle": "Soriano", "lrn": "136100100009", "gender": "Female", "birth": "2008-08-09", "grade": "Grade 11", "section": "STEM-A", "status": "Enrolled"},
            {"last": "Navarro", "first": "Carlos", "middle": "Espino", "lrn": "136100100010", "gender": "Male", "birth": "2008-02-27", "grade": "Grade 11", "section": "STEM-A", "status": "Enrolled"},
            {"last": "Aguilar", "first": "Diana", "middle": "Flores", "lrn": "136100100011", "gender": "Female", "birth": "2007-07-19", "grade": "Grade 12", "section": "ABM-A", "status": "Enrolled"},
            {"last": "Bautista", "first": "Eduardo", "middle": "Capili", "lrn": "136100100012", "gender": "Male", "birth": "2007-10-05", "grade": "Grade 12", "section": "ABM-A", "status": "Enrolled"},
            {"last": "Dela Cruz", "first": "Juan", "middle": "Ramos", "lrn": "136100100013", "gender": "Male", "birth": "2012-01-15", "grade": "Grade 7", "section": "Section A", "status": "Enrolled"},
            {"last": "Santos", "first": "Pedro", "middle": "Mendoza", "lrn": "136100100014", "gender": "Male", "birth": "2009-12-12", "grade": "Grade 10", "section": "Section D", "status": "Enrolled"},
            {"last": "Lopez", "first": "Liza", "middle": "Cruz", "lrn": "136100100015", "gender": "Female", "birth": "2010-06-25", "grade": "Grade 9", "section": "Section C", "status": "Enrolled"},
            {"last": "Reyes", "first": "Marco", "middle": "Antonio", "lrn": "136100100016", "gender": "Male", "birth": "2007-03-08", "grade": "Grade 12", "section": "ABM-A", "status": "Enrolled"},
        ],
        "sections": [
            {"grade": "Grade 7", "name": "Section A", "adviser": "Ms. Lovelyn C. Malanot"},
            {"grade": "Grade 8", "name": "Section B", "adviser": "Mr. Jose Ramirez"},
            {"grade": "Grade 9", "name": "Section C", "adviser": "Ms. Gina Aguilar"},
            {"grade": "Grade 10", "name": "Section D", "adviser": "Ms. Norma Villanueva"},
            {"grade": "Grade 11", "name": "STEM-A", "adviser": "Mr. Ben Castillo"},
            {"grade": "Grade 12", "name": "ABM-A", "adviser": "Mr. Jose Ramirez"},
        ],
        "teachers": [
            {"name": "Ms. Lovelyn C. Malanot", "subject": "Science", "gender": "Female", "age": 38, "tenure": 9, "classes": 4, "students": 120, "uploads": 32, "assessments": 8, "announcements": 15, "activity": "high"},
            {"name": "Mr. Jose Ramirez", "subject": "Mathematics", "gender": "Male", "age": 45, "tenure": 14, "classes": 5, "students": 150, "uploads": 25, "assessments": 10, "announcements": 12, "activity": "high"},
            {"name": "Ms. Gina Aguilar", "subject": "English", "gender": "Female", "age": 33, "tenure": 6, "classes": 4, "students": 125, "uploads": 18, "assessments": 6, "announcements": 9, "activity": "mid"},
            {"name": "Ms. Norma Villanueva", "subject": "Filipino", "gender": "Female", "age": 51, "tenure": 20, "classes": 3, "students": 100, "uploads": 12, "assessments": 4, "announcements": 7, "activity": "mid"},
            {"name": "Mr. Ben Castillo", "subject": "MAPEH", "gender": "Male", "age": 29, "tenure": 3, "classes": 5, "students": 160, "uploads": 20, "assessments": 5, "announcements": 11, "activity": "high"},
            {"name": "Ms. Elena Santiago", "subject": "Araling Panlipunan", "gender": "Female", "age": 41, "tenure": 11, "classes": 4, "students": 130, "uploads": 10, "assessments": 3, "announcements": 5, "activity": "low"},
        ],
        "grades": {
            "Grade 7 - Section A": {
                "subjects": ["English", "Mathematics", "Science", "Filipino", "Araling Panlipunan", "MAPEH", "ESP"],
                "students": [
                    {"name": "Maria Dela Cruz Santos", "scores": [88, 92, 90, 85, 87, 91, 89]},
                    {"name": "Juan Ramos Reyes", "scores": [76, 80, 78, 82, 75, 79, 77]},
                    {"name": "Juan Ramos Dela Cruz", "scores": [84, 86, 88, 90, 82, 85, 87]},
                ],
            },
            "Grade 8 - Section B": {
                "subjects": ["English", "Mathematics", "Science", "Filipino", "Araling Panlipunan", "MAPEH", "ESP"],
                "students": [
                    {"name": "Ana Reyes Garcia", "scores": [90, 88, 92, 89, 91, 87, 90]},
                    {"name": "Pedro Santos Mendoza", "scores": [79, 83, 80, 77, 82, 81, 78]},
                ],
            },
            "Grade 9 - Section C": {
                "subjects": ["English", "Mathematics", "Science", "Filipino", "Araling Panlipunan", "MAPEH", "ESP"],
                "students": [
                    {"name": "Liza Cruz Lopez", "scores": [92, 95, 90, 91, 93, 88, 94]},
                    {"name": "Miguel Diaz Torres", "scores": [85, 80, 82, 84, 81, 83, 86]},
                ],
            },
            "Grade 10 - Section D": {
                "subjects": ["English", "Mathematics", "Science", "Filipino", "Araling Panlipunan", "MAPEH", "ESP"],
                "students": [
                    {"name": "Sofia Mercado Villanueva", "scores": [88, 91, 89, 90, 86, 92, 88]},
                    {"name": "Andres Bautista Castro", "scores": [74, 78, 76, 75, 79, 77, 73]},
                ],
            },
            "Grade 11 - STEM-A": {
                "subjects": ["Oral Communication", "General Mathematics", "Earth Science", "21st Century Literature"],
                "students": [
                    {"name": "Bianca Soriano Ramos", "scores": [93, 90, 95, 91]},
                    {"name": "Carlos Espino Navarro", "scores": [87, 84, 85, 82]},
                ],
            },
            "Grade 12 - ABM-A": {
                "subjects": ["English for Academic Purposes", "Statistics and Probability", "Personal Development", "Work Immersion"],
                "students": [
                    {"name": "Diana Flores Aguilar", "scores": [91, 89, 93, 95]},
                    {"name": "Eduardo Capili Bautista", "scores": [85, 82, 84, 88]},
                ],
            },
        },
        "attendance": {
            "grade7_a": {"label": "Grade 7 · Section A", "present": 152, "absent": 12, "late": 8},
            "grade8_b": {"label": "Grade 8 · Section B", "present": 148, "absent": 18, "late": 10},
            "grade9_c": {"label": "Grade 9 · Section C", "present": 155, "absent": 9, "late": 6},
            "grade10_d": {"label": "Grade 10 · Section D", "present": 141, "absent": 22, "late": 14},
            "grade11_stem": {"label": "Grade 11 · STEM-A", "present": 149, "absent": 7, "late": 5},
            "grade12_abm": {"label": "Grade 12 · ABM-A", "present": 144, "absent": 15, "late": 9},
        },
        "assessments": [
            {"title": "Quarterly Exam - Mathematics", "type": "Exam", "teacher": "Mr. Jose Ramirez", "class": "Grade 10 - Section D", "submissions": 42, "avgScore": 78.5, "status": "Active"},
            {"title": "Science Investigatory Project", "type": "Performance Task", "teacher": "Ms. Lovelyn C. Malanot", "class": "Grade 7 - Section A", "submissions": 35, "avgScore": 88.2, "status": "Active"},
            {"title": "Book Report - English", "type": "Assignment", "teacher": "Ms. Gina Aguilar", "class": "Grade 9 - Section C", "submissions": 30, "avgScore": 82.4, "status": "Active"},
            {"title": "Filipino Essay Writing", "type": "Assignment", "teacher": "Ms. Norma Villanueva", "class": "Grade 10 - Section D", "submissions": 28, "avgScore": 80.1, "status": "Active"},
            {"title": "Physical Fitness Test", "type": "Performance Task", "teacher": "Mr. Ben Castillo", "class": "Grade 11 - STEM-A", "submissions": 45, "avgScore": 90.0, "status": "Active"},
        ],
        "activityLog": [
            {"action": "Enrolled student", "detail": "Maria Santos enrolled to Grade 7 - Section A", "time": "2026-04-01 09:24 AM"},
            {"action": "Created section", "detail": "Grade 12 - STEM-A created", "time": "2026-04-01 10:12 AM"},
            {"action": "Generated LMS account", "detail": "Account generated for Juan Reyes", "time": "2026-04-02 01:45 PM"},
            {"action": "Promoted student", "detail": "Ana Garcia promoted to Grade 8", "time": "2026-04-03 09:30 AM"},
            {"action": "Updated system settings", "detail": "Attendance threshold changed to 80%", "time": "2026-04-04 11:05 AM"},
        ],
    }


def _qa_display_name(profile):
    parts = [profile.first_name or ""]
    if profile.middle_initial:
        parts.append(f"{profile.middle_initial.upper()}.")
    parts.append(profile.last_name or "")
    return " ".join(p for p in parts if p)


def _qa_grade_label(grade_level):
    return f"Grade {grade_level}"


def _qa_guarded(callback):
    """Run a data-gathering callback; return None on DB/schema errors so the
    dashboard falls back to sample data instead of crashing."""
    try:
        return callback()
    except (ProgrammingError, DatabaseError):
        return None


def quality_audit_context(user):
    """Quality Assurance dashboard context (rebuilds real data, falls back to
    sample data on empty/DB errors) — shares the TASS Admin data pipeline."""
    return tass_administrator_context(user)


def tass_administrator_context(user):
    data = _qa_demo_data()
    now = timezone.now()
    week_ago = now - timedelta(days=7)
    active_year = _active_academic_year()

    # ---- Students (from active-year student enrollments) ----
    section_lookup = {}
    real_students = _qa_guarded(
        lambda: _qa_real_students(active_year, section_lookup)
    )
    if real_students:
        data["students"] = real_students

    # ---- Sections (active, with adviser) ----
    real_sections = _qa_guarded(lambda: _qa_real_sections(active_year))
    if real_sections:
        data["sections"] = real_sections

    # ---- Teachers (activity + class load from real VCs) ----
    real_teachers = _qa_guarded(lambda: _qa_real_teachers(active_year, now, week_ago))
    if real_teachers:
        data["teachers"] = real_teachers

    # ---- Grades (real StudentGrade rows grouped by section) ----
    real_grades = _qa_guarded(lambda: _qa_real_grades(active_year, section_lookup))
    if real_grades:
        data["grades"] = real_grades

    # ---- Attendance (real attendance records grouped by section) ----
    real_attendance = _qa_guarded(_qa_real_attendance)
    if real_attendance:
        data["attendance"] = real_attendance

    # ---- Assessments (real assignment aggregates) ----
    real_assessments = _qa_guarded(lambda: _qa_real_assessments(active_year))
    if real_assessments:
        data["assessments"] = real_assessments

    # ---- Activity log (real) ----
    real_logs = _qa_guarded(_qa_real_logs)
    if real_logs:
        data["activityLog"] = real_logs

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
    enrollments = enrollments.select_related(
        "student__user", "section"
    ).order_by("section__grade_level", "section__name", "student__last_name")
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
    sections = Section.objects.filter(status=STATUS_ACTIVE).select_related("adviser")
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
    teachers_users = User.objects.filter(groups__name="teacher", status="active").select_related("employee_profile")
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
                "gender": "",
                "age": 0,
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


def _qa_real_grades(active_year, section_lookup):
    grades = StudentGrade.objects.select_related("student", "subject", "student__user")
    if active_year:
        grades = grades.filter(academic_year=active_year)
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
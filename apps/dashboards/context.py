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
    }


def registrar_context(user):
    active_year = _active_academic_year()
    current = active_year.id if active_year else None
    enrolled = VirtualClassroomEnrollment.objects.filter(status=VirtualClassroomEnrollment.STATUS_ACTIVE)
    if current:
        enrolled = enrolled.filter(virtual_classroom__academic_year_id=current)
    sections_qs = Section.objects.filter(status=STATUS_ACTIVE)
    subjects_qs = Subject.objects.filter(status=STATUS_ACTIVE)
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

    return {
        "class_items": class_items,
        "my_class_count": len(class_items),
        "total_students": sum(i["students_count"] for i in class_items),
        "open_assessments": sum(i["assessments_count"] for i in class_items),
        "today_sessions": today_sessions,
        "needs_grading": needs_grading,
        "needs_grading_count": needs_grading.count(),
        "recent_announcements": class_announcements,
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
    return {
        "stats": stats,
        "active_year": active_year,
        "announcements": _newest_announcements(),
        "recent_reports": GeneratedReport.objects.order_by("-generated_at")[:5],
        "recent_activity": ActivityLog.objects.select_related("user").order_by("-timestamp")[:8],
        "teacher_count": _role_user_count("teacher"),
        "student_count": _role_user_count("student"),
        "materials_count": LearningResourcePlacement.objects.count(),
        "active_assessments": active_assessments.count(),
        "attendance_rate": round(present_records / total_records * 100, 1) if total_records else 0,
        "submission_rate": round(graded_submissions / all_submissions * 100, 1) if all_submissions else 0,
        "active_classes": vcs.count(),
        "low_activity_count": User.objects.filter(
            groups__name="teacher", status="active"
        ).exclude(
            activity_logs__timestamp__gte=timezone.now() - timedelta(days=7)
        ).count(),
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
from django.contrib.auth.models import Group
from django.utils import timezone
from datetime import timedelta

from apps.accounts.models import ActivityLog, LoginSession, StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section, StudentEnrollment, Subject
from apps.virtual_classrooms.models import VirtualClassroom
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
STATUS_ENROLLED = "ENROLLED"

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
    active_sessions = LoginSession.objects.count()
    pending_statuses = ("pending_verification", "pending_password_setup")
    pending_users = (
        User.objects.filter(status__in=pending_statuses)
        .order_by("date_created")[:6]
    )
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
        "active_sessions_count": active_sessions,
        "backup_count": BackupRecord.objects.count(),
        "pending_review_count": RoleAccessReview.objects.filter(completed_at__isnull=True).count(),
        "pending_users": pending_users,
        "recent_backups": BackupRecord.objects.order_by("-created_at")[:5],
        "recent_activity": ActivityLog.objects.select_related("user").order_by("-timestamp")[:8],
    }


def registrar_context(user):
    active_year = _active_academic_year()
    current = active_year.id if active_year else None
    enrolled = StudentEnrollment.objects.filter(status=STATUS_ENROLLED)
    if current:
        enrolled = enrolled.filter(academic_year_id=current)
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
    recent_enrollments = StudentEnrollment.objects.select_related(
        "student", "section"
    ).order_by("-enrolled_at")
    if current:
        recent_enrollments = recent_enrollments.filter(academic_year_id=current)
    recent_enrollments = recent_enrollments[:6]
    return {
        "stats": stats,
        "active_year": active_year,
        "recent_enrollments": recent_enrollments,
        "section_count": sections_qs.count(),
        "subject_count": subjects_qs.count(),
        "vcs": vcs_qs.select_related("section", "subject", "teacher").order_by("-created_at")[:6],
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
        students_count = StudentEnrollment.objects.filter(
            section=vc.section, status=STATUS_ENROLLED
        ).distinct().count()
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
        .select_related("student", "assessment_assignment__assessment")
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
    return {
        "stats": stats,
        "active_year": active_year,
        "announcements": _newest_announcements(),
        "recent_reports": GeneratedReport.objects.order_by("-generated_at")[:5],
    }


def assistant_principal_context(user):
    active_year = _active_academic_year()
    current = active_year.id if active_year else None
    sections = Section.objects.filter(status=STATUS_ACTIVE)
    subjects = Subject.objects.filter(status=STATUS_ACTIVE)
    vcs = VirtualClassroom.objects.filter(status=STATUS_ACTIVE)
    enrolled = StudentEnrollment.objects.filter(status=STATUS_ENROLLED)
    if current:
        sections = sections.filter(academic_year_id=current)
        subjects = subjects.filter(academic_year_id=current)
        vcs = vcs.filter(academic_year_id=current)
        enrolled = enrolled.filter(academic_year_id=current)
    stats = [
        _stat("Active Sections", sections.count(), "fa-users-between-lines"),
        _stat("Active Subjects", subjects.count(), "fa-book-open"),
        _stat("Virtual Classrooms", vcs.count(), "fa-chalkboard-user"),
        _stat("Enrolled Students", enrolled.count(), "fa-user-graduate"),
    ]
    return {
        "stats": stats,
        "active_year": active_year,
        "recent_vcs": vcs.select_related("section", "subject", "teacher").order_by("-created_at")[:6],
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
    enrollments = StudentEnrollment.objects.filter(
        student=profile, status=STATUS_ENROLLED
    )
    if active_year:
        enrollments = enrollments.filter(academic_year=active_year)

    my_vcs = VirtualClassroom.objects.filter(
        section__in=[e.section for e in enrollments],
        status=STATUS_ACTIVE,
    ).select_related("subject", "section", "teacher", "academic_year")

    pending = []
    now = timezone.now()
    for vc in my_vcs:
        for assignment in AssessmentAssignment.objects.filter(virtual_classroom=vc, assessment__status=STATUS_ACTIVE):
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
        "my_classes": my_vcs.order_by("subject__name"),
        "pending_assessments": pending[:6],
        "attendance": _attendance_summary(profile),
        "grades": grades,
        "announcements": {"school": school_ann, "class": class_ann},
    }
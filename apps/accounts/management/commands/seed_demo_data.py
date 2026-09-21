"""Create representative pilot data: sections, subjects, VCs, topics, assessments, attendance, and announcements.

All operations are idempotent — safe to run repeatedly. Call AFTER
``seed_official_accounts`` and ``assign_role_permissions``.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.academic_structure.models import (
    AcademicStructureVersion,
    AcademicYear,
    Section,
    StudentEnrollment,
    Subject,
)
from apps.accounts.models import User
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment


SECTIONS = [
    # (name, grade_level)
    ("Rose", 7),
    ("Sunflower", 8),
    ("STEM-A", 11),
]

SUBJECTS = [
    # (name, grade_level, subject_type, grading_period)
    ("Mathematics 7", 7, "APPLIED", ""),
    ("Science 7", 7, "APPLIED", ""),
    ("Mathematics 8", 8, "APPLIED", ""),
    ("Science 8", 8, "APPLIED", ""),
    ("General Mathematics", 11, "APPLIED", ""),
    ("Disiplines and Ideas in the Social Sciences", 11, "APPLIED", ""),
]

TOPICS_PER_SUBJECT = [
    # (name, order)
    ("Topic 1: Foundations", 1),
    ("Topic 2: Core Concepts", 2),
    ("Topic 3: Application", 3),
]

TOPIC_TEMPLATES = [
    ("Module 1: Introduction", 1),
    ("Module 2: Discussion", 2),
    ("Module 3: Reflection", 3),
]


class Command(BaseCommand):
    help = "Seed representative demo data for pilot testing"

    def handle(self, *args, **kwargs):
        ay = AcademicYear.objects.filter(status="ACTIVE").first()
        if not ay:
            self.stdout.write(self.style.ERROR("No active Academic Year. Create one first."))
            return

        # Academic Structure Version (if missing)
        AcademicStructureVersion.objects.get_or_create(
            academic_year=ay,
            defaults={"grading_period_type": "QUARTER"},
        )

        # Teacher users for VC assignment
        teachers = list(
            User.objects.filter(
                role_assignments__role__code="teacher",
                status=User.Status.ACTIVE,
            ).distinct()[:2]
        )
        teacher = teachers[0] if teachers else None

        # Registrar for VC creation
        registrar = User.objects.filter(
            role_assignments__role__code="registrar",
            status=User.Status.ACTIVE,
        ).first()

        student_user = User.objects.filter(
            role_assignments__role__code="student",
            status=User.Status.ACTIVE,
        ).first()

        # Sections
        section_map = {}
        for name, grade in SECTIONS:
            sec, _ = Section.objects.get_or_create(
                academic_year=ay, grade_level=grade, name=name,
                defaults={"status": "ACTIVE"},
            )
            section_map[(ay.pk, grade, name)] = sec
        self.stdout.write(self.style.SUCCESS(f"Sections: {len(section_map)}"))

        # Enroll student in Grade 7-Rose (if not already)
        if student_user and hasattr(student_user, "student_profile"):
            sec7 = section_map.get((ay.pk, 7, "Rose"))
            if sec7:
                existing = StudentEnrollment.objects.filter(
                    student=student_user.student_profile,
                    academic_year=ay,
                    status="ENROLLED",
                ).first()
                if existing:
                    existing.section = sec7
                    existing.save()
                else:
                    StudentEnrollment.objects.create(
                        student=student_user.student_profile,
                        academic_year=ay,
                        section=sec7,
                        status="ENROLLED",
                    )

        # Subjects (reuse existing subject by name+grade regardless of period)
        subject_map = {}
        for sname, grade, stype, period in SUBJECTS:
            subj = Subject.objects.filter(
                academic_year=ay, name=sname, grade_level=grade,
            ).first()
            if not subj:
                subj = Subject.objects.create(
                    academic_year=ay, name=sname, grade_level=grade,
                    grading_period=period,
                    subject_type=stype, status="ACTIVE",
                )
            subject_map[(ay.pk, grade, sname)] = subj

            # Topic templates (only once per subject)
            for tname, torder in TOPIC_TEMPLATES:
                from apps.teaching_learning.models import TopicTemplate
                TopicTemplate.objects.get_or_create(
                    subject=subj, order=torder,
                    defaults={"name": tname},
                )
        self.stdout.write(self.style.SUCCESS(f"Subjects: {len(subject_map)}"))

        # Virtual Classrooms
        vc_count = 0
        for sname, grade, stype, period in SUBJECTS:
            subj = subject_map[(ay.pk, grade, sname)]
            # Find matching section
            sec = None
            for (aypk, gl, nm), s in section_map.items():
                if gl == grade:
                    sec = s
                    break
            if not sec:
                continue

            # Reuse existing VC for this subject+section regardless of period
            vc = VirtualClassroom.objects.filter(
                subject=subj, section=sec, academic_year=ay,
            ).order_by("-grading_period").first()
            if vc is None:
                vc = VirtualClassroom.objects.create(
                    subject=subj, section=sec, academic_year=ay,
                    grading_period=period, teacher=teacher, status="active",
                )
                vc_count += 1
                from apps.virtual_classrooms.services import populate_classroom_defaults
                populate_classroom_defaults(vc, auto_enroll=True)

        self.stdout.write(self.style.SUCCESS(f"New VCs created: {vc_count}"))

        # Attendance session for first VC (if none exists)
        first_vc = VirtualClassroom.objects.filter(status="active").first()
        if first_vc and teacher:
            from apps.attendance.models import AttendanceSession, AttendanceRecord
            today = date.today()
            sess, created = AttendanceSession.objects.get_or_create(
                virtual_classroom=first_vc,
                session_date=today,
                defaults={"created_by": teacher},
            )
            if created and student_user and hasattr(student_user, "student_profile"):
                AttendanceRecord.objects.get_or_create(
                    session=sess,
                    student=student_user.student_profile,
                    defaults={"status": "present_on_time"},
                )

        # Assessments for first VC
        if first_vc and teacher:
            from apps.assessments.models import Assessment, AssessmentPart, AssessmentAssignment
            assessment, _ = Assessment.objects.get_or_create(
                subject=first_vc.subject,
                academic_year=ay,
                title=f"Quiz 1 - {first_vc.subject.name}",
                defaults={
                    "created_by": teacher,
                    "assessment_method": "quiz",
                    "assessment_type": "written_oral",
                    "instructions": "Answer all items.",
                    "lifecycle_stage": "available",
                    "available_at": timezone.now(),
                    "submission_deadline": timezone.now() + timedelta(days=7),
                    "status": "active",
                },
            )
            AssessmentPart.objects.get_or_create(
                assessment=assessment,
                order=1,
                defaults={
                    "question_type": "multiple_choice",
                    "question_text": "Choose the correct answer.",
                    "items": {
                        "item_1": {
                            "question": "2 + 2 = ?",
                            "choices": ["3", "4", "5", "6"],
                            "correct_answer": "4",
                            "point": 1,
                        }
                    },
                },
            )
            AssessmentAssignment.objects.get_or_create(
                assessment=assessment,
                virtual_classroom=first_vc,
                defaults={"assigned_by": teacher},
            )

        # School-wide announcement
        from apps.communication.models import SchoolAnnouncement, ClassAnnouncement
        if registrar:
            SchoolAnnouncement.objects.get_or_create(
                title="Welcome to SY " + ay.label,
                defaults={
                    "content": "Welcome to the new school year! Please check your dashboards for updates.",
                    "posted_by": registrar,
                    "status": "active",
                },
            )

        # Class announcement for first VC
        if first_vc and teacher:
            ClassAnnouncement.objects.get_or_create(
                virtual_classroom=first_vc,
                posted_by=teacher,
                defaults={
                    "content": "Welcome to this virtual classroom. Please review the topic outline.",
                    "status": "active",
                },
            )

        self.stdout.write(self.style.SUCCESS("Demo data seeded successfully."))

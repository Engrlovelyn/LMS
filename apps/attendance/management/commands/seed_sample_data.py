"""Populate the analytics charts and attendance views with representative sample data.

The dashboard and analytics charts read from real rows (attendance records,
assessment assignments, submissions, resource placements, announcements, shared
resources, generated reports). A fresh install has classrooms and enrollments but
none of those, so every chart and list renders an empty state. This command fills
them in.

What it seeds, per active VirtualClassroom:
  * AttendanceSession rows on the last N school days, plus one AttendanceRecord
    per enrolled student, weighted towards on-time presence with a realistic
    sprinkling of late / excused / unexcused absences.
  * An Assessment (with one objective part), its AssessmentAssignment, and
    graded Submissions from most enrolled students so submission-rate, score
    distribution, pass/fail and per-student performance have points to draw.
  * Topics, LearningResources and LearningResourcePlacements so the
    download-activity chart has monthly counts.

And, school-wide, once:
  * A shared LearningResource per subject (``share_to_repository=True``) so the
    Subject-Level Resource Repository is not empty for every teacher.
  * A handful of SchoolAnnouncement rows plus one ClassAnnouncement per
    classroom so the Announcements panels have content.
  * An SF2 SFReportTemplate and a few GeneratedReport rows so the Analytics &
    Reports list (System Administrator, Assistant Principal) is not empty.

Every write is idempotent. Sessions, assessments, parts, assignments, resources,
placements, announcements, templates and reports use get_or_create; records and
submissions are bulk-inserted with ``ignore_conflicts=True`` against their unique
constraints, so re-running never duplicates a row. Sample values come from a
fixed-seed RNG, so a re-run also never contradicts what the first run wrote.

Call AFTER ``seed_official_accounts`` / ``assign_role_permissions``.
Use ``--flush`` to rebuild from scratch, ``--dry-run`` to preview.
"""

import hashlib
import random
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Case, DateTimeField, Q, Value, When
from django.utils import timezone

from apps.analytics.models import GeneratedReport, SFReportTemplate
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.assessments.models import (
    Assessment,
    AssessmentAssignment,
    AssessmentPart,
    GradingComponent,
    GradingFormula,
    Submission,
)
from apps.assessments.services import compute_grades_for_classroom
from apps.communication.models import ClassAnnouncement, SchoolAnnouncement
from apps.teaching_learning.models import LearningResource
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

SEED = 20260902

QUIZ_TITLE_PREFIX = "Quiz"
SAMPLE_TOPIC_NAMES = [
    "Topic 1: Foundations",
    "Topic 2: Core Concepts",
    "Topic 3: Application",
    "Topic 4: Synthesis",
]
RESOURCE_TITLES = [
    "Lesson Handout",
    "Supplementary Worksheet",
    "Discussion Guide",
    "Sample Problem Set",
]
RESOURCE_TYPES = ["document", "pdf", "presentation", "link"]

# Shared subject-level resources (the Subject-Level Resource Repository).
SHARED_RESOURCE_TITLE_PREFIX = "Shared Lesson Pack - "

# Announcements. School-wide notices are visible to every campus; one class
# announcement is posted per classroom by its teacher.
SCHOOL_ANNOUNCEMENTS = [
    ("Enrollment for the new school year is now open", "Please coordinate with the Registrar's Office for the schedule of enrollment per grade level."),
    ("Quarterly examinations schedule released", "The examination period runs for one week. Advisers are asked to post the detailed schedule in their classrooms."),
    ("Faculty meeting for all teachers", "A faculty meeting will be held to discuss the academic calendar and assessment deadlines."),
]
CLASS_ANNOUNCEMENT_MARKER = "Reminder: bring your learning materials to the next session."

# Analytics report records so the Generated Reports list is not empty.
SF_TEMPLATE_NAME = "SF2 - Daily Attendance (Official)"
REPORT_SCOPES = [
    (GeneratedReport.TYPE_SF2, "Active Academic Year - Whole School"),
    (GeneratedReport.TYPE_SF1, "Active Academic Year - Learner Roster"),
    (GeneratedReport.TYPE_SUBMISSION_REPORT, "Active Academic Year - Submission Summary"),
]

# Weight of each attendance status.
ATTENDANCE_WEIGHTS = [
    (AttendanceRecord.PRESENT_ON_TIME, 78),
    (AttendanceRecord.PRESENT_LATE, 7),
    (AttendanceRecord.ABSENT_EXCUSED, 5),
    (AttendanceRecord.ABSENT_UNEXCUSED, 10),
]

# Excused absences always need a remark: AttendanceRecord.clean() rejects them
# without one, so a bare seeded row would fail validation on the next save.
EXCUSED_REMARKS = [
    "Medical appointment - clinic letter on file.",
    "Family emergency - adviser notified.",
    "Barangay event - official excuse received.",
    "Health condition - advised to rest at home.",
]

# Share of enrolled students who submit each assessment. Below 100 on purpose so
# the submission-rate charts have a visible gap.
SUBMIT_RATES = [88, 82, 91]

# Generated scores as a percentage of possible points.
SCORE_MEAN = 76.0
SCORE_SPREAD = 12.0

TOTAL_POINTS_CHOICES = [Decimal("20"), Decimal("25"), Decimal("30"), Decimal("40"), Decimal("50")]


class Command(BaseCommand):
    help = "Seed attendance, assessment and resource rows so analytics graphs have data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--sessions",
            type=int,
            default=6,
            help="Session dates per classroom, counting back from today (default: 6)",
        )
        parser.add_argument(
            "--assessments",
            type=int,
            default=1,
            help="Assessments per classroom, with graded submissions (default: 1)",
        )
        parser.add_argument(
            "--resources",
            type=int,
            default=2,
            help="Learning resources placed per classroom (default: 2)",
        )
        parser.add_argument(
            "--classrooms",
            type=int,
            default=0,
            help="Only seed the first N active classrooms (default: 0 = all)",
        )
        parser.add_argument(
            "--shared-resources",
            type=int,
            default=1,
            help="Shared repository resources to create per subject (default: 1)",
        )
        parser.add_argument(
            "--announcements",
            type=int,
            default=2,
            help="School-wide announcements to create (default: 2)",
        )
        parser.add_argument(
            "--reports",
            type=int,
            default=3,
            help="Generated analytics report rows to create (default: 3)",
        )
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete existing sample rows in scope first",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing",
        )

    def handle(self, *args, **options):
        # Re-enabled: the Assistant Principal's Insights panels (Attendance,
        # Student Performance, Analytics & Reports) render from real rows, so a
        # fresh install with no attendance sessions, assessments or submissions
        # shows only empty states. Run this explicitly to populate demo data.
        session_count = max(0, options["sessions"])
        assessment_count = max(0, options["assessments"])
        resource_count = max(0, options["resources"])
        classroom_limit = max(0, options["classrooms"])
        shared_resource_count = max(0, options["shared_resources"])
        announcement_count = max(0, options["announcements"])
        report_count = max(0, options["reports"])

        classrooms = list(
            VirtualClassroom.objects.filter(status=VirtualClassroom.STATUS_ACTIVE)
            .exclude(teacher=None)
            .select_related("subject", "section", "academic_year", "teacher")
            .order_by("pk")
        )
        if not classrooms:
            self.stdout.write(self.style.ERROR("No active classrooms. Import students first."))
            return
        if classroom_limit:
            classrooms = classrooms[:classroom_limit]

        rng = random.Random(SEED)
        session_dates = self._session_dates(session_count)
        student_count = VirtualClassroomEnrollment.objects.filter(
            virtual_classroom__in=classrooms,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).count()

        self.stdout.write(f"Classrooms in scope: {len(classrooms)}")
        self.stdout.write(f"Active enrollments: {student_count}")
        self.stdout.write(f"Session dates: {', '.join(str(d) for d in session_dates)}")

        if options["dry_run"]:
            per_classroom = student_count / max(1, len(classrooms))
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN - would write up to "
                    f"{len(classrooms) * len(session_dates)} attendance sessions, "
                    f"{int(len(classrooms) * len(session_dates) * per_classroom)} "
                    f"attendance records, {len(classrooms) * assessment_count} assessments, "
                    f"{len(classrooms) * resource_count} resource placements."
                )
            )
            return

        if options["flush"]:
            self._flush(classrooms)

        counts = {
            "sessions": 0,
            "records": 0,
            "assessments": 0,
            "submissions": 0,
            "topics": 0,
            "resources": 0,
            "placements": 0,
            "shared_resources": 0,
            "school_announcements": 0,
            "class_announcements": 0,
            "reports": 0,
            "templates": 0,
            "formulas": 0,
            "grades": 0,
        }

        with transaction.atomic():
            for index, classroom in enumerate(classrooms, start=1):
                counts["sessions"] += self._seed_sessions(classroom, session_dates)
                counts["records"] += self._seed_records(classroom, session_dates, rng)
                counts["assessments"] += self._seed_assessments(
                    classroom, assessment_count, rng, counts
                )
                self._seed_resources(classroom, resource_count, rng, counts)
                counts["class_announcements"] += self._seed_class_announcement(classroom)
                if index % 50 == 0 or index == len(classrooms):
                    self.stdout.write(f"  ...{index}/{len(classrooms)} classrooms processed")

            self._seed_shared_resources(classrooms, shared_resource_count, counts)
            self._seed_school_announcements(announcement_count, counts)
            self._seed_reports(classrooms, report_count, counts)
            self._seed_grades(classrooms, counts)

        labels = {
            "sessions": "Attendance sessions",
            "records": "Attendance records",
            "assessments": "Assessments",
            "submissions": "Submissions",
            "topics": "Topics",
            "resources": "Learning resources",
            "placements": "Resource placements",
            "shared_resources": "Shared repository resources",
            "school_announcements": "School announcements",
            "class_announcements": "Class announcements",
            "reports": "Generated reports",
            "templates": "SF report templates",
            "formulas": "Grading formulas",
            "grades": "Computed student grades",
        }
        for key, label in labels.items():
            self.stdout.write(self.style.SUCCESS(f"{label}: {counts[key]}"))
        self.stdout.write(self.style.SUCCESS("Sample data seeded successfully."))

    # -- attendance ---------------------------------------------------------

    def _session_dates(self, count):
        """The last `count` weekdays ending today, ascending."""
        dates = []
        cursor = timezone.localdate()
        while len(dates) < count:
            if cursor.weekday() < 5:
                dates.append(cursor)
            cursor -= timedelta(days=1)
        return list(reversed(dates))

    def _seed_sessions(self, classroom, session_dates):
        existing = set(
            AttendanceSession.objects.filter(
                virtual_classroom=classroom, session_date__in=session_dates
            ).values_list("session_date", flat=True)
        )
        missing = [d for d in session_dates if d not in existing]
        if not missing:
            return 0
        AttendanceSession.objects.bulk_create(
            [
                AttendanceSession(
                    virtual_classroom=classroom,
                    session_date=session_date,
                    created_by=classroom.teacher,
                )
                for session_date in missing
            ]
        )
        return len(missing)

    def _seed_records(self, classroom, session_dates, rng):
        sessions = dict(
            AttendanceSession.objects.filter(
                virtual_classroom=classroom, session_date__in=session_dates
            ).values_list("session_date", "pk")
        )
        if not sessions:
            return 0

        statuses = [status for status, _ in ATTENDANCE_WEIGHTS]
        weights = [weight for _, weight in ATTENDANCE_WEIGHTS]
        students = self._enrolled_students(classroom)

        rows = [
            AttendanceRecord(
                session_id=sessions[session_date],
                student_id=student_id,
                status=rng.choices(statuses, weights=weights, k=1)[0],
            )
            for session_date in session_dates
            for student_id in students
        ]
        # Give every excused absence the remark its validation rule requires.
        for row in rows:
            if row.status == AttendanceRecord.ABSENT_EXCUSED:
                row.remark = rng.choice(EXCUSED_REMARKS)

        before = AttendanceRecord.objects.filter(session_id__in=sessions.values()).count()
        AttendanceRecord.objects.bulk_create(rows, ignore_conflicts=True)
        after = AttendanceRecord.objects.filter(session_id__in=sessions.values()).count()
        return after - before

    # -- assessments --------------------------------------------------------

    def _enrolled_students(self, classroom):
        return list(
            VirtualClassroomEnrollment.objects.filter(
                virtual_classroom=classroom,
                status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            )
            .order_by("student_id")
            .values_list("student_id", flat=True)
        )

    def _seed_assessments(self, classroom, count, rng, counts):
        students = self._enrolled_students(classroom)
        if not students or count == 0:
            return 0

        created = 0
        for number in range(1, count + 1):
            title = f"{QUIZ_TITLE_PREFIX} {number} - {classroom.subject.name}"
            deadline = timezone.now() - timedelta(days=14 - number * 2)
            assessment, was_created = Assessment.objects.get_or_create(
                subject=classroom.subject,
                academic_year=classroom.academic_year,
                title=title,
                defaults={
                    "created_by": classroom.teacher,
                    "assessment_method": rng.choice(
                        [Assessment.METHOD_QUIZ, Assessment.METHOD_SEATWORK, Assessment.METHOD_ASSIGNMENT]
                    ),
                    "assessment_type": rng.choice(
                        [
                            Assessment.TYPE_WRITTEN_ORAL,
                            Assessment.TYPE_PRODUCT_PERFORMANCE,
                            Assessment.TYPE_SUMMATIVE_TEST,
                        ]
                    ),
                    "instructions": "Answer all items. Show complete work for every problem.",
                    "lifecycle_stage": Assessment.STAGE_COMPLETED,
                    "available_at": deadline - timedelta(days=7),
                    "submission_deadline": deadline,
                    "accepted_file_format": "PDF",
                    "status": Assessment.STATUS_ACTIVE,
                },
            )
            if was_created:
                created += 1

            points = rng.choice(TOTAL_POINTS_CHOICES)
            per_item = float(points) / 10.0
            part, _ = AssessmentPart.objects.get_or_create(
                assessment=assessment,
                order=1,
                defaults={
                    "question_type": "multiple_choice",
                    "question_text": "Choose the letter of the correct answer.",
                    "points": points,
                    "items": {
                        f"item_{index}": {
                            "question": f"Item {index}: 2 + 2 = ?",
                            "choices": ["3", "4", "5", "6"],
                            "correct_answer": "4",
                            "point": per_item,
                        }
                        for index in range(1, 11)
                    },
                },
            )
            # An assessment is shared by every section taking that subject, so
            # the part (and therefore its points) may already have been created
            # by an earlier classroom. Always score against what is stored.
            points = part.points

            assigned_at = deadline - timedelta(days=7)
            assignment, _ = AssessmentAssignment.objects.get_or_create(
                assessment=assessment,
                virtual_classroom=classroom,
                defaults={"assigned_by": classroom.teacher},
            )
            # assigned_at is auto_now_add, so a plain save cannot backdate it.
            AssessmentAssignment.objects.filter(pk=assignment.pk).update(
                assigned_at=assigned_at
            )
            counts["submissions"] += self._seed_submissions(
                assignment, students, points, rng, assigned_at
            )

        return created

    def _seed_submissions(self, assignment, students, points, rng, assigned_at):
        """Grade most, but not all, of the students so rates read below 100%.

        Submission is decided by a hash of (assignment, student) rather than
        the sequential RNG, so a student who did not submit on the first run
        will not quietly start submitting on the next one.
        """
        already = set(
            Submission.objects.filter(assessment_assignment=assignment).values_list(
                "student_id", flat=True
            )
        )
        rate = rng.choice(SUBMIT_RATES)
        submitters = [
            student
            for student in students
            if student not in already and self._will_submit(assignment.pk, student, rate)
        ]
        if not submitters:
            return 0

        rows = []
        for student in submitters:
            percentage = max(15.0, min(100.0, rng.gauss(SCORE_MEAN, SCORE_SPREAD)))
            rows.append(
                Submission(
                    assessment_assignment=assignment,
                    student_id=student,
                    is_late=rng.random() < 0.08,
                    total_score=(points * Decimal(str(round(percentage / 100, 4)))).quantize(
                        Decimal("0.01")
                    ),
                    grading_status=Submission.GRADING_COMPLETE,
                    teacher_remarks="Reviewed and returned to the student.",
                )
            )
        Submission.objects.bulk_create(rows, ignore_conflicts=True)

        # submitted_at is auto_now_add, so bulk_create stamps it as now. Every
        # student here had no prior row, so all matched rows are ours.
        Submission.objects.filter(
            assessment_assignment=assignment,
            student_id__in=[row.student_id for row in rows],
        ).update(
            submitted_at=Case(
                *[
                    When(
                        student_id=row.student_id,
                        then=Value(
                            assigned_at
                            + timedelta(days=1 + offset % 3, hours=rng.randint(0, 10)),
                            output_field=DateTimeField(),
                        ),
                    )
                    for offset, row in enumerate(rows)
                ]
            ),
            graded_at=assigned_at + timedelta(days=8),
        )

        # submitters already excluded everyone holding a row, so len(rows) is
        # the number actually inserted.
        return len(rows)

    @staticmethod
    def _will_submit(assignment_pk, student_id, rate):
        digest = hashlib.md5(f"{assignment_pk}:{student_id}".encode()).hexdigest()
        return int(digest[:8], 16) % 100 < rate

    # -- computed grades ----------------------------------------------------

    def _seed_grades(self, classrooms, counts):
        """Give each classroom a grading formula and roll up student grades.

        The "School Avg. Score" stat and any official grade read from
        ``StudentGrade``, which is only produced by ``compute_student_grades``
        against a per-classroom ``GradingFormula``. A fresh install has neither,
        so the card read 0.0% even after submissions existed. Here we build a
        formula whose components (one per assessment type the class actually
        uses, weights summing to 100) reflect the seeded work, then compute.
        """
        from apps.academic_structure.models import AcademicStructureVersion

        formula_count = 0
        grade_count = 0
        for classroom in classrooms:
            assignment_ids = AssessmentAssignment.objects.filter(
                virtual_classroom=classroom
            ).values_list("id", flat=True)
            used_types = list(
                Assessment.objects.filter(
                    assignments__id__in=list(assignment_ids)
                )
                .values_list("assessment_type", flat=True)
                .distinct()
            )
            if not used_types:
                continue

            structure_version = getattr(classroom.academic_year, "structure_version", None) or (
                AcademicStructureVersion.objects.filter(
                    academic_year=classroom.academic_year
                ).first()
            )
            if structure_version is None:
                structure_version = AcademicStructureVersion.objects.create(
                    academic_year=classroom.academic_year,
                    grading_period_type="QUARTER",
                )

            formula, _ = GradingFormula.objects.get_or_create(
                virtual_classroom=classroom,
                defaults={"structure_version": structure_version},
            )
            if not formula.components.exists():
                base = 100 // len(used_types)
                remainder = 100 - base * len(used_types)
                for index, comp_type in enumerate(used_types):
                    GradingComponent.objects.create(
                        formula=formula,
                        component_type=comp_type,
                        weight_percentage=base + (remainder if index == 0 else 0),
                    )
                formula_count += 1

            result = compute_grades_for_classroom(classroom, formula)
            grade_count += result["grades"]

        counts["formulas"] += formula_count
        counts["grades"] += grade_count

    # -- learning resources -------------------------------------------------

    def _seed_resources(self, classroom, count, rng, counts):
        from apps.teaching_learning.models import (
            LearningResource,
            LearningResourcePlacement,
            Topic,
        )

        if count == 0:
            return

        topics = list(Topic.objects.filter(virtual_classroom=classroom).order_by("order"))
        wanted = max(count, len(topics))
        for order, name in enumerate(SAMPLE_TOPIC_NAMES, start=1):
            if order > wanted:
                break
            if any(topic.order == order for topic in topics):
                continue
            Topic.objects.create(virtual_classroom=classroom, name=name, order=order)
            counts["topics"] += 1
        topics = list(Topic.objects.filter(virtual_classroom=classroom).order_by("order"))

        for offset, topic in enumerate(topics[:count]):
            resource, was_created = LearningResource.objects.get_or_create(
                subject=classroom.subject,
                academic_year=classroom.academic_year,
                title=f"{RESOURCE_TITLES[offset % len(RESOURCE_TITLES)]} - {topic.name}",
                defaults={
                    "uploaded_by": classroom.teacher,
                    "description": f"Class material for {topic.name}.",
                    "resource_type": RESOURCE_TYPES[offset % len(RESOURCE_TYPES)],
                    "external_link": "https://example.com/materials",
                    "status": "active",
                    "share_to_repository": False,
                },
            )
            counts["resources"] += 1 if was_created else 0

            # Spread placements across recent months so the download chart has
            # more than one month bucket. placed_at is auto_now_add, so it has
            # to be backdated with an explicit UPDATE.
            placed_at = timezone.now() - timedelta(days=30 * (offset + 1))
            _placement, was_placed = LearningResourcePlacement.objects.get_or_create(
                resource=resource,
                virtual_classroom=classroom,
                topic=topic,
                defaults={
                    "placed_by": classroom.teacher,
                    "is_original": True,
                },
            )
            LearningResourcePlacement.objects.filter(pk=_placement.pk).update(
                placed_at=placed_at
            )
            if was_placed:
                counts["placements"] += 1

    # -- shared repository resources ---------------------------------------

    def _seed_shared_resources(self, classrooms, count, counts):
        """One (or more) shared resource per distinct subject.

        The Subject-Level Resource Repository only lists resources with
        ``share_to_repository=True``; the classroom-level resources seeded above
        are intentionally private, so without these the repository stays empty.
        """
        if count == 0:
            return

        by_subject = {}
        for classroom in classrooms:
            by_subject.setdefault(classroom.subject_id, classroom)
        for index, classroom in enumerate(by_subject.values()):
            for offset in range(count):
                title = f"{SHARED_RESOURCE_TITLE_PREFIX}{classroom.subject.name}"
                if count > 1:
                    title = f"{title} ({offset + 1})"
                _resource, was_created = LearningResource.objects.get_or_create(
                    subject=classroom.subject,
                    academic_year=classroom.academic_year,
                    title=title,
                    defaults={
                        "uploaded_by": classroom.teacher,
                        "description": (
                            f"A shared lesson pack for {classroom.subject.name}, "
                            "contributed to the subject-level repository."
                        ),
                        "resource_type": RESOURCE_TYPES[index % len(RESOURCE_TYPES)],
                        "external_link": "https://example.com/shared-materials",
                        "status": LearningResource.STATUS_ACTIVE,
                        "share_to_repository": True,
                    },
                )
                counts["shared_resources"] += 1 if was_created else 0

    # -- announcements ------------------------------------------------------

    def _announcement_author(self):
        """A staff user who may author school-wide notices, else any teacher."""
        from apps.accounts.models import RoleAssignment

        order = [
            "principal",
            "assistant_principal",
            "system_admin",
            "elementary_principal",
            "registrar",
            "teacher",
        ]
        for code in order:
            assignment = (
                RoleAssignment.objects.filter(role__code=code)
                .select_related("user")
                .order_by("id")
                .first()
            )
            if assignment and assignment.user:
                return assignment.user
        return None

    def _seed_school_announcements(self, count, counts):
        if count == 0:
            return
        author = self._announcement_author()
        if author is None:
            return
        for title, content in SCHOOL_ANNOUNCEMENTS[:count]:
            _ann, was_created = SchoolAnnouncement.objects.get_or_create(
                title=title,
                posted_by=author,
                defaults={
                    "content": content,
                    "audience": "whole_school",
                    "audience_at_posted_at": "whole_school",
                    "status": SchoolAnnouncement.STATUS_ACTIVE,
                },
            )
            counts["school_announcements"] += 1 if was_created else 0

    def _seed_class_announcement(self, classroom):
        # DISABLED: one sample class announcement per classroom was seeding on
        # every run (it is not gated by --announcements). Remove the next line
        # to re-enable.
        return 0
        if classroom.teacher is None:
            return 0
        _ann, was_created = ClassAnnouncement.objects.get_or_create(
            virtual_classroom=classroom,
            posted_by=classroom.teacher,
            content=CLASS_ANNOUNCEMENT_MARKER,
            defaults={"status": ClassAnnouncement.STATUS_ACTIVE},
        )
        return 1 if was_created else 0

    # -- report records -----------------------------------------------------

    def _seed_reports(self, classrooms, count, counts):
        """An SF2 template plus a few generated report records.

        The Analytics & Reports views on the System Administrator and Assistant
        Principal dashboards list GeneratedReport rows; nothing creates them in a
        fresh install, so the list renders its empty state until someone
        generates one. This seeds representative rows.
        """
        active_year = classrooms[0].academic_year if classrooms else None
        if active_year is None:
            return

        author = self._announcement_author()
        if author is None:
            return

        _tpl, was_created = SFReportTemplate.objects.get_or_create(
            name=SF_TEMPLATE_NAME,
            form_type=SFReportTemplate.FORM_SF2,
            defaults={
                "description": "Official SF2 daily attendance form (sample).",
                "template_file": ContentFile(
                    b"School Form 2 (SF2) - Daily Attendance\nSample template seeded for the pilot.\n",
                    name="sf2_daily_attendance.txt",
                ),
                "field_mappings": {"LRN": "lrn", "Name": "full_name"},
                "is_active": True,
                "uploaded_by": author,
            },
        )
        counts["templates"] += 1 if was_created else 0

        for report_type, scope in REPORT_SCOPES[:count]:
            _report, was_created = GeneratedReport.objects.get_or_create(
                report_type=report_type,
                academic_year=active_year,
                scope_description=scope,
                defaults={
                    "parameters": {"sample": True},
                    "file": ContentFile(
                        f"{scope}\nGenerated sample report for the pilot.\n".encode(),
                        name=f"{report_type}_{active_year.label}.txt",
                    ),
                    "generated_by": author,
                },
            )
            counts["reports"] += 1 if was_created else 0

    # -- teardown -----------------------------------------------------------

    def _flush(self, classrooms):
        """Remove sample rows in scope so a re-run rebuilds them cleanly."""
        from apps.teaching_learning.models import (
            LearningResource,
            LearningResourcePlacement,
            Topic,
        )

        vc_ids = [classroom.pk for classroom in classrooms]

        records, _ = AttendanceRecord.objects.filter(
            session__virtual_classroom_id__in=vc_ids
        ).delete()
        sessions, _ = AttendanceSession.objects.filter(
            virtual_classroom_id__in=vc_ids
        ).delete()

        submissions, _ = Submission.objects.filter(
            assessment_assignment__virtual_classroom_id__in=vc_ids
        ).delete()
        assignments, _ = AssessmentAssignment.objects.filter(
            virtual_classroom_id__in=vc_ids
        ).delete()

        # Only drop assessments this command created, and only once no
        # assignment still points at them (the FK is PROTECT).
        stale = Assessment.objects.filter(
            title__startswith=QUIZ_TITLE_PREFIX,
            assignments__isnull=True,
            subject_id__in={classroom.subject_id for classroom in classrooms},
        )
        parts, _ = AssessmentPart.objects.filter(assessment__in=list(stale)).delete()
        assessment_count = stale.count()
        stale.delete()

        placements, _ = LearningResourcePlacement.objects.filter(
            virtual_classroom_id__in=vc_ids
        ).delete()
        # Only drop topics and resources matching the names this command uses,
        # so anything created by populate_classroom_defaults survives a flush.
        topics, _ = Topic.objects.filter(
            virtual_classroom_id__in=vc_ids, name__in=SAMPLE_TOPIC_NAMES
        ).delete()
        resources, _ = LearningResource.objects.filter(
            Q(subject_id__in={classroom.subject_id for classroom in classrooms})
            & (
                Q(title__startswith=RESOURCE_TITLES[0])
                | Q(title__startswith=RESOURCE_TITLES[1])
                | Q(title__startswith=RESOURCE_TITLES[2])
                | Q(title__startswith=RESOURCE_TITLES[3])
            )
        ).delete()
        shared_resources, _ = LearningResource.objects.filter(
            title__startswith=SHARED_RESOURCE_TITLE_PREFIX
        ).delete()

        class_announcements, _ = ClassAnnouncement.objects.filter(
            virtual_classroom_id__in=vc_ids, content=CLASS_ANNOUNCEMENT_MARKER
        ).delete()
        school_announcements, _ = SchoolAnnouncement.objects.filter(
            title__in=[title for title, _ in SCHOOL_ANNOUNCEMENTS]
        ).delete()

        templates, _ = SFReportTemplate.objects.filter(name=SF_TEMPLATE_NAME).delete()
        reports, _ = GeneratedReport.objects.filter(
            report_type__in=[report_type for report_type, _ in REPORT_SCOPES],
            scope_description__in=[scope for _, scope in REPORT_SCOPES],
        ).delete()

        self.stdout.write(self.style.WARNING("Flushed:"))
        self.stdout.write(f"  attendance records {records}, sessions {sessions}")
        self.stdout.write(f"  submissions {submissions}, assignments {assignments}")
        self.stdout.write(f"  assessments {assessment_count}, parts {parts}")
        self.stdout.write(f"  placements {placements}, topics {topics}, resources {resources}")
        self.stdout.write(
            f"  shared resources {shared_resources}, "
            f"class announcements {class_announcements}, "
            f"school announcements {school_announcements}"
        )
        self.stdout.write(f"  report templates {templates}, reports {reports}")
"""Give every Virtual Classroom a set of takeable example assessments.

The student dashboard reads real ``Assessment`` rows through the classroom's
``AssessmentAssignment``s. On a fresh database each classroom only carries the
single placeholder assessment created by ``seed_sample_data`` (an essay
performance task with no objective items), so a student has nothing they can
actually sit down and answer.

This command tops every active Virtual Classroom up to at least N (default 5)
takeable assessments:

  * Assessment 1 - "Quiz 1"        : multiple choice, auto-graded
  * Assessment 2 - "Quiz 2"        : identification, auto-graded
  * Assessment 3 - "Seatwork"      : true/false, auto-graded
  * Assessment 4 - "Assignment"    : short answer + essay (manual grading)

Each one is created with real ``AssessmentPart`` items (so the server-rendered
submit page, the teacher repository and the student quiz taker all see the same
questions), assigned to the classroom, marked active and made available now with
a deadline in the future. Students are notified through the bell exactly like a
teacher-published assessment.

Idempotent: re-running only creates what is missing. Use ``--per-class`` to
change the target, ``--vc`` to limit to one classroom, ``--deadline-days`` to
change how far out the deadline is, and ``--no-notify`` to skip the bell.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.academic_structure.models import AcademicYear
from apps.assessments.models import (
    Assessment,
    AssessmentAssignment,
    AssessmentPart,
)
from apps.virtual_classrooms.models import VirtualClassroom

User = get_user_model()


# ---------------------------------------------------------------------------
# Question banks. Each entry is (question text, answer key, point value).
# Kept generic and subject-agnostic so one set works for any of the 130+
# subjects in the demo, with the subject name woven into the prompt.
# ---------------------------------------------------------------------------
def _mc_items(subject):
    rows = [
        (
            "Which of the following is the best first step when working on a "
            "task in {}?".format(subject),
            [
                "Read the instructions carefully",
                "Guess the answer quickly",
                "Skip the task entirely",
                "Copy a classmate's work",
            ],
            0,
        ),
        (
            "In {}, which practice shows you are working accurately?".format(subject),
            [
                "Checking your work before passing it",
                "Rushing to finish first",
                "Leaving items blank",
                "Ignoring the given examples",
            ],
            0,
        ),
        (
            "What should you do if you do not understand a lesson in {}?".format(subject),
            [
                "Ask the teacher or a classmate",
                "Stay silent and give up",
                "Disturb the class",
                "Skip the activity",
            ],
            0,
        ),
        (
            "Which value is most important when completing a {} activity?".format(subject),
            [
                "Honesty",
                "Speed",
                "Loudness",
                "Luck",
            ],
            0,
        ),
        (
            "How should you submit your {} work?".format(subject),
            [
                "On time and complete",
                "Late and incomplete",
                "Only when reminded",
                "Never",
            ],
            0,
        ),
    ]
    items = {}
    for i, (question, choices, correct) in enumerate(rows, start=1):
        items["item_{}".format(i)] = {
            "question": question,
            "choices": choices,
            "correct_answer": choices[correct],
            "point": 1,
        }
    return items


def _identification_items(subject):
    rows = [
        (
            "I am the person who guides the lesson in {}. Who am I?".format(subject),
            ["teacher", "Teacher"],
        ),
        (
            "I am the place where a {} class is held. What am I?".format(subject),
            ["classroom", "Classroom"],
        ),
        (
            "I am the object you write your {} answers on. What am I?".format(subject),
            ["notebook", "paper", "Notebook", "Paper"],
        ),
        (
            "I am the quality of finishing {} work on time. What am I?".format(subject),
            ["punctuality", "Punctuality"],
        ),
        (
            "I am the word for working together with your classmates in {}. What am I?".format(subject),
            ["cooperation", "Cooperation", "teamwork", "Teamwork"],
        ),
    ]
    items = {}
    for i, (question, accepted) in enumerate(rows, start=1):
        items["item_{}".format(i)] = {
            "question": question,
            "accepted_answers": accepted,
            "point": 1,
        }
    return items


def _tf_items(subject):
    rows = [
        ("It is important to review your notes after a {} lesson.".format(subject), "True"),
        ("Submitting {} work late is always acceptable.".format(subject), "False"),
        ("Asking questions helps you learn {} better.".format(subject), "True"),
        ("Copying answers from a classmate is good practice in {}.".format(subject), "False"),
        ("Following {} instructions carefully leads to better results.".format(subject), "True"),
    ]
    items = {}
    for i, (question, correct) in enumerate(rows, start=1):
        items["item_{}".format(i)] = {
            "question": question,
            "choices": ["True", "False"],
            "correct_answer": correct,
            "point": 1,
        }
    return items


def _short_answer_items(subject):
    rows = [
        (
            "In one or two sentences, explain what you have learned in {} this week.".format(subject),
            ["", "student response"],
        ),
        (
            "Give one real-life example where you can apply {}.".format(subject),
            ["", "student response"],
        ),
        (
            "Describe one part of {} you find challenging and how you will improve.".format(subject),
            ["", "student response"],
        ),
    ]
    items = {}
    for i, (question, accepted) in enumerate(rows, start=1):
        items["item_{}".format(i)] = {
            "question": question,
            "accepted_answers": accepted,
            "point": 2,
        }
    return items


def _essay_items(subject):
    return {
        "item_1": {
            "question": (
                "Write a short reflection on your learning experience in {}. "
                "Discuss what you enjoyed, what was difficult, and how you plan "
                "to improve.".format(subject)
            ),
            "point": 6,
        }
    }


# (title suffix, assessment_method, assessment_type, part builder list)
# Each builder is (question_type, items_factory, section label).
ASSESSMENT_SPECS = [
    (
        "Quiz 1",
        Assessment.METHOD_QUIZ,
        Assessment.TYPE_WRITTEN_ORAL,
        [(AssessmentPart.TYPE_MULTIPLE_CHOICE, _mc_items, "Part I - Multiple Choice")],
    ),
    (
        "Quiz 2",
        Assessment.METHOD_QUIZ,
        Assessment.TYPE_WRITTEN_ORAL,
        [(AssessmentPart.TYPE_IDENTIFICATION, _identification_items, "Part I - Identification")],
    ),
    (
        "Seatwork",
        Assessment.METHOD_SEATWORK,
        Assessment.TYPE_WRITTEN_ORAL,
        [(AssessmentPart.TYPE_TRUE_FALSE, _tf_items, "Part I - True or False")],
    ),
    (
        "Assignment",
        Assessment.METHOD_ASSIGNMENT,
        Assessment.TYPE_PRODUCT_PERFORMANCE,
        [
            (AssessmentPart.TYPE_SHORT_ANSWER, _short_answer_items, "Part I - Short Answer"),
            (AssessmentPart.TYPE_ESSAY, _essay_items, "Part II - Essay"),
        ],
    ),
    (
        "Long Quiz",
        Assessment.METHOD_EXAMINATION,
        Assessment.TYPE_SUMMATIVE_TEST,
        [
            (AssessmentPart.TYPE_MULTIPLE_CHOICE, _mc_items, "Part I - Multiple Choice"),
            (AssessmentPart.TYPE_TRUE_FALSE, _tf_items, "Part II - True or False"),
        ],
    ),
]


class Command(BaseCommand):
    help = "Ensure every active Virtual Classroom has takeable example assessments."

    def add_arguments(self, parser):
        parser.add_argument(
            "--per-class",
            type=int,
            default=5,
            help="Target number of assessments per classroom (default 5).",
        )
        parser.add_argument(
            "--vc",
            type=int,
            default=None,
            help="Limit to a single Virtual Classroom id.",
        )
        parser.add_argument(
            "--deadline-days",
            type=int,
            default=30,
            help="Days from now until the submission deadline (default 30).",
        )
        parser.add_argument(
            "--no-notify",
            action="store_true",
            help="Do not create bell notifications for students.",
        )

    def handle(self, *args, **options):
        # DISABLED: sample assessment seeding is intentionally turned off so a
        # deploy or seed run cannot repopulate assessments the team is going to
        # enter themselves. Remove the next two lines to re-enable.
        self.stdout.write(
            self.style.WARNING(
                "seed_example_assessments is disabled; no assessments were created."
            )
        )
        return

        deadline_days = options["deadline_days"]
        per_class = max(options["per_class"], len(ASSESSMENT_SPECS))
        notify = not options["no_notify"]

        vcs = VirtualClassroom.objects.filter(status=VirtualClassroom.STATUS_ACTIVE)
        if options["vc"]:
            vcs = vcs.filter(pk=options["vc"])
        vcs = vcs.select_related("subject", "academic_year", "teacher")

        if not vcs:
            self.stderr.write(self.style.ERROR("No active Virtual Classrooms found."))
            return

        fallback_user = User.objects.filter(is_superuser=True).order_by("pk").first()

        now = timezone.now()
        available_at = now - timedelta(minutes=1)
        deadline = now + timedelta(days=deadline_days)

        created_total = 0
        assigned_total = 0

        for vc in vcs:
            creator = vc.teacher or fallback_user
            if creator is None:
                self.stderr.write(
                    self.style.WARNING(
                        "Classroom {} has no teacher and no superuser fallback; skipped.".format(vc.pk)
                    )
                )
                continue

            existing = AssessmentAssignment.objects.filter(virtual_classroom=vc).count()
            for suffix, method, atype, part_specs in ASSESSMENT_SPECS:
                if existing >= per_class:
                    break
                assessment, was_created = self._ensure_assessment(
                    vc, creator, suffix, method, atype, part_specs, available_at, deadline
                )
                _, was_assigned = AssessmentAssignment.objects.get_or_create(
                    assessment=assessment,
                    virtual_classroom=vc,
                    defaults={"assigned_by": creator},
                )
                if was_created:
                    created_total += 1
                if was_assigned:
                    assigned_total += 1
                    existing += 1
                    if notify:
                        try:
                            from apps.communication.notifications import (
                                notify_assessment_assignment,
                            )

                            notify_assessment_assignment(
                                AssessmentAssignment.objects.get(
                                    assessment=assessment, virtual_classroom=vc
                                )
                            )
                        except Exception as exc:  # pragma: no cover - best effort
                            self.stderr.write(
                                self.style.WARNING(
                                    "Notify failed for {}: {}".format(assessment.title, exc)
                                )
                            )

        self.stdout.write(
            self.style.SUCCESS(
                "Done. Created {} assessment(s), made {} assignment(s) across {} "
                "classroom(s).".format(created_total, assigned_total, vcs.count())
            )
        )

    def _ensure_assessment(
        self, vc, creator, suffix, method, atype, part_specs, available_at, deadline
    ):
        subject = vc.subject
        subject_name = subject.name if subject else "the subject"
        title = "{} - {}".format(suffix, subject_name)[:200]

        assessment = Assessment.objects.filter(
            subject=subject,
            academic_year=vc.academic_year,
            created_by=creator,
            title=title,
        ).first()
        if assessment is not None:
            return assessment, False

        assessment = Assessment.objects.create(
            subject=subject,
            academic_year=vc.academic_year,
            created_by=creator,
            title=title,
            assessment_method=method,
            assessment_type=atype,
            instructions="Example {} for {}. Answer all items.".format(suffix, subject_name),
            lifecycle_stage=Assessment.STAGE_SUBMISSION,
            available_at=available_at,
            submission_deadline=deadline,
            status=Assessment.STATUS_ACTIVE,
        )
        for order, (qtype, factory, label) in enumerate(part_specs, start=1):
            AssessmentPart.objects.create(
                assessment=assessment,
                question_type=qtype,
                question_text=label,
                order=order,
                items=factory(subject_name),
            )
        return assessment, True

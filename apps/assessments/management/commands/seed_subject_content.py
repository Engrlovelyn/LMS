"""Replace generic demo content with subject-specific content.

The demo seeders fill every classroom with the same placeholders -- "Topic 1:
Foundations", "Lesson Handout", and a "Quiz 1" whose items all read
"2 + 2 = ?". Inside a virtual classroom that content is correctly scoped to the
classroom's subject, but every subject looks identical, so the panelist sees a
Filipino class that looks exactly like a Chemistry class.

This command makes each subject's classrooms genuinely different:

* **Assessments** -- the five canonical assessments ("Quiz 1", "Quiz 2",
  "Seatwork", "Assignment", "Long Quiz") are re-filled with subject-specific
  questions derived from ``apps.assessments.subject_content``. Parts that
  already have graded submissions are updated *in place* (their total points
  are preserved) so existing performance-tracking data stays valid.
* **Topics** -- generic topic names are renamed to subject-specific units.
* **Materials** -- generic learning resources are retitled and re-typed per
  subject; stray placements whose resource belongs to another subject are
  removed.

It is idempotent: generic-looking content is only ever touched once, and
re-running is a no-op. Use ``--vc`` to limit to one classroom, and
``--skip-assessments`` / ``--skip-topics`` / ``--skip-materials`` to narrow the
scope.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from datetime import timedelta

from apps.academic_structure.models import AcademicYear
from apps.assessments.models import (
    Assessment,
    AssessmentAssignment,
    AssessmentPart,
    Submission,
)
from apps.assessments.subject_content import get_content
from apps.teaching_learning.models import (
    LearningResource,
    LearningResourcePlacement,
    Topic,
)
from apps.virtual_classrooms.models import VirtualClassroom

User = get_user_model()


# Generic names created by the demo seeders; only these get rewritten.
GENERIC_TOPIC_NAMES = {
    "Topic 1: Foundations",
    "Topic 2: Core Concepts",
    "Topic 3: Application",
    "Topic 4: Synthesis",
    "Unit 1: Introduction & Foundations",
    "Unit 2: Core Concepts & Practice",
    "Unit 3: Performance Task & Reflection",
}
GENERIC_RESOURCE_PREFIXES = (
    "Lesson Handout",
    "Supplementary Worksheet",
    "Discussion Guide",
    "Sample Problem Set",
)
SHARED_RESOURCE_TITLE_PREFIX = "Shared Lesson Pack - "


# ---------------------------------------------------------------------------
# Item builders. Each takes the resolved content dict and subject name and
# returns an AssessmentPart.items mapping.
# ---------------------------------------------------------------------------
def _rotate(seq, n):
    seq = list(seq)
    if not seq:
        return seq
    n %= len(seq)
    return seq[n:] + seq[:n]


def _mc_items(content, subject):
    concepts = content["concepts"]
    terms = [term for term, _ in concepts]
    items = {}
    for i in range(5):
        term, definition = concepts[i % len(concepts)]
        distractors = [other for other in terms if other != term][:3]
        choices = _rotate([term] + distractors, i)
        items["item_{}".format(i + 1)] = {
            "question": 'Which term in {} is described as: "{}"?'.format(subject, definition),
            "choices": choices,
            "correct_answer": term,
            "point": 1,
        }
    return items


def _id_items(content, subject):
    concepts = content["concepts"]
    items = {}
    for i in range(5):
        term, definition = concepts[i % len(concepts)]
        accepted = [term]
        if term.lower() != term:
            accepted.append(term.lower())
        items["item_{}".format(i + 1)] = {
            "question": 'Identify the term described: "{}"'.format(definition),
            "accepted_answers": accepted,
            "point": 1,
        }
    return items


def _tf_items(content, subject):
    concepts = content["concepts"]
    items = {}
    for i in range(5):
        term, definition = concepts[i % len(concepts)]
        if i % 2 == 0:
            text = 'In {}, "{}" means {}.'.format(subject, term, definition)
            answer = "True"
        else:
            other = concepts[(i + 1) % len(concepts)][1]
            text = 'In {}, "{}" means {}.'.format(subject, term, other)
            answer = "False"
        items["item_{}".format(i + 1)] = {
            "question": text,
            "choices": ["True", "False"],
            "correct_answer": answer,
            "point": 1,
        }
    return items


def _sa_items(content, subject):
    concepts = content["concepts"]
    items = {}
    for i in range(3):
        term = concepts[i % len(concepts)][0]
        items["item_{}".format(i + 1)] = {
            "question": (
                "In two to three sentences, explain the concept of {} in {}.".format(
                    term, subject
                )
            ),
            "accepted_answers": ["", term.lower()],
            "point": 2,
        }
    return items


def _essay_items(content, subject):
    concepts = content["concepts"]
    first = concepts[0][0]
    second = concepts[1 % len(concepts)][0]
    return {
        "item_1": {
            "question": (
                "Write a short essay explaining how {} and {} are important in {}. "
                "Support your answer with concrete examples.".format(
                    first, second, subject
                )
            ),
            "point": 6,
        }
    }


SPECS = [
    {
        "suffix": "Quiz 1",
        "method": Assessment.METHOD_QUIZ,
        "type": Assessment.TYPE_WRITTEN_ORAL,
        "parts": [(AssessmentPart.TYPE_MULTIPLE_CHOICE, _mc_items, "Part I - Multiple Choice")],
    },
    {
        "suffix": "Quiz 2",
        "method": Assessment.METHOD_QUIZ,
        "type": Assessment.TYPE_WRITTEN_ORAL,
        "parts": [(AssessmentPart.TYPE_IDENTIFICATION, _id_items, "Part I - Identification")],
    },
    {
        "suffix": "Seatwork",
        "method": Assessment.METHOD_SEATWORK,
        "type": Assessment.TYPE_WRITTEN_ORAL,
        "parts": [(AssessmentPart.TYPE_TRUE_FALSE, _tf_items, "Part I - True or False")],
    },
    {
        "suffix": "Assignment",
        "method": Assessment.METHOD_ASSIGNMENT,
        "type": Assessment.TYPE_PRODUCT_PERFORMANCE,
        "parts": [
            (AssessmentPart.TYPE_SHORT_ANSWER, _sa_items, "Part I - Short Answer"),
            (AssessmentPart.TYPE_ESSAY, _essay_items, "Part II - Essay"),
        ],
    },
    {
        "suffix": "Long Quiz",
        "method": Assessment.METHOD_EXAMINATION,
        "type": Assessment.TYPE_SUMMATIVE_TEST,
        "parts": [
            (AssessmentPart.TYPE_MULTIPLE_CHOICE, _mc_items, "Part I - Multiple Choice"),
            (AssessmentPart.TYPE_TRUE_FALSE, _tf_items, "Part II - True or False"),
        ],
    },
]


class Command(BaseCommand):
    help = "Rewrite generic demo content as subject-specific content."

    def add_arguments(self, parser):
        parser.add_argument("--vc", type=int, default=None, help="Limit to one Virtual Classroom id.")
        parser.add_argument("--skip-assessments", action="store_true")
        parser.add_argument("--skip-topics", action="store_true")
        parser.add_argument("--skip-materials", action="store_true")
        parser.add_argument("--deadline-days", type=int, default=30)

    def handle(self, *args, **options):
        # DISABLED: this command re-creates the five canonical assessments and
        # regenerates topics/materials, which would repopulate rows the team is
        # going to enter themselves. Remove these two lines to re-enable.
        self.stdout.write(
            self.style.WARNING(
                "seed_subject_content is disabled; no assessments, topics or materials were written."
            )
        )
        return

        now = timezone.now()
        deadline = now + timedelta(days=options["deadline_days"])

        vcs = VirtualClassroom.objects.filter(status=VirtualClassroom.STATUS_ACTIVE)
        if options["vc"]:
            vcs = vcs.filter(pk=options["vc"])
        vcs = list(vcs.select_related("subject", "academic_year", "teacher"))
        if not vcs:
            self.stderr.write(self.style.ERROR("No active Virtual Classrooms found."))
            return

        fallback_user = User.objects.filter(is_superuser=True).order_by("pk").first()

        # 1) De-duplicate same-named assessments in the active year so the
        #    quiz-bank title key no longer collides.
        removed = self._dedupe_assessments(vcs)

        # 2) Make sure every classroom has its five canonical assessments and
        #    collect the (assessment, spec, content) jobs to sync.
        jobs = {}
        assigned = 0
        created = 0
        if not options["skip_assessments"]:
            for vc in vcs:
                subject_name = vc.subject.name if vc.subject else "the subject"
                content = get_content(subject_name)
                creator = vc.teacher or fallback_user
                if creator is None:
                    continue
                for spec in SPECS:
                    assessment, was_created = self._ensure_assessment(
                        vc, creator, spec, content, now, deadline
                    )
                    if was_created:
                        created += 1
                    _, was_assigned = AssessmentAssignment.objects.get_or_create(
                        assessment=assessment,
                        virtual_classroom=vc,
                        defaults={"assigned_by": creator},
                    )
                    if was_assigned:
                        assigned += 1
                    jobs.setdefault(assessment.pk, (assessment, spec, content))

            # 3) Sync parts (subject-specific questions), preserving points on
            #    parts that already carry graded submissions.
            for assessment, spec, content in jobs.values():
                self._sync_parts(assessment, spec, content)

        # 4) Topics and materials.
        topics_changed = 0
        if not options["skip_topics"]:
            for vc in vcs:
                topics_changed += self._sync_topics(vc)

        materials_changed = 0
        cleaned_placements = 0
        if not options["skip_materials"]:
            cleaned_placements = self._clean_cross_subject_placements(vcs)
            seen = set()
            for vc in vcs:
                if not vc.subject_id:
                    continue
                key = (vc.subject_id, vc.academic_year_id)
                if key in seen:
                    continue
                seen.add(key)
                materials_changed += self._sync_materials(vc)

        self.stdout.write(
            self.style.SUCCESS(
                "Subject content applied across {} classroom(s): {} assessment(s) "
                "created, {} assignment(s) added, {} duplicate(s) removed, "
                "{} topic(s) renamed, {} material(s) retitled, "
                "{} stray placement(s) removed.".format(
                    len(vcs),
                    created,
                    assigned,
                    removed,
                    topics_changed,
                    materials_changed,
                    cleaned_placements,
                )
            )
        )

    # -- assessments --------------------------------------------------------
    def _dedupe_assessments(self, vcs):
        """Collapse same-named assessments in the subjects/years in scope.

        Keeps the row with the most submissions (tie-break: most assignments,
        then lowest pk) and migrates the others' classroom assignments onto it.
        A duplicate that already has submissions is left alone.
        """
        subject_ids = {vc.subject_id for vc in vcs if vc.subject_id}
        year_ids = {vc.academic_year_id for vc in vcs if vc.academic_year_id}
        qs = (
            Assessment.objects.filter(
                subject_id__in=subject_ids, academic_year_id__in=year_ids
            )
            .prefetch_related("assignments")
            .order_by("pk")
        )
        groups = {}
        for assessment in qs:
            groups.setdefault(
                (assessment.subject_id, assessment.academic_year_id, assessment.title),
                [],
            ).append(assessment)

        removed = 0
        for candidates in groups.values():
            if len(candidates) < 2:
                continue
            ranked = sorted(
                candidates,
                key=lambda a: (
                    Submission.objects.filter(
                        assessment_assignment__assessment=a
                    ).count(),
                    a.assignments.count(),
                    -a.pk,
                ),
                reverse=True,
            )
            survivor = ranked[0]
            for other in ranked[1:]:
                if Submission.objects.filter(
                    assessment_assignment__assessment=other
                ).exists():
                    continue
                for assignment in other.assignments.all():
                    AssessmentAssignment.objects.get_or_create(
                        assessment=survivor,
                        virtual_classroom=assignment.virtual_classroom,
                        defaults={"assigned_by": assignment.assigned_by},
                    )
                other.delete()
                removed += 1
        return removed

    def _ensure_assessment(self, vc, creator, spec, content, now, deadline):
        subject_name = content["subject"]
        title = "{} - {}".format(spec["suffix"], subject_name)[:200]
        assessment = Assessment.objects.filter(
            subject=vc.subject,
            academic_year=vc.academic_year,
            title=title,
        ).order_by("pk").first()
        if assessment is not None:
            return assessment, False

        # Fresh assessment: make it takeable now.
        available_at = now - timedelta(minutes=1)
        assessment = Assessment.objects.create(
            subject=vc.subject,
            academic_year=vc.academic_year,
            created_by=creator,
            title=title,
            assessment_method=spec["method"],
            assessment_type=spec["type"],
            instructions="Answer all items for {}.".format(subject_name),
            lifecycle_stage=Assessment.STAGE_SUBMISSION,
            available_at=available_at,
            submission_deadline=deadline,
            status=Assessment.STATUS_ACTIVE,
        )
        return assessment, True

    def _sync_parts(self, assessment, spec, content):
        subject_name = content["subject"]
        existing = {part.question_type: part for part in assessment.parts.all()}
        wanted_types = set()

        for order, (qtype, factory, label) in enumerate(spec["parts"], start=1):
            wanted_types.add(qtype)
            items = factory(content, subject_name)
            part = existing.get(qtype)
            if part is None:
                AssessmentPart.objects.create(
                    assessment=assessment,
                    question_type=qtype,
                    question_text=label,
                    order=order,
                    items=items,
                )
                continue
            if part.responses.exists():
                items = self._redistribute_points(items, part.points)
            part.question_text = label
            part.order = order
            part.items = items
            part.save()

        # Drop leftover parts that are no longer part of the spec, when safe.
        for qtype, part in existing.items():
            if qtype in wanted_types:
                continue
            if not part.responses.exists():
                part.delete()

    @staticmethod
    def _redistribute_points(items, total):
        """Spread an existing part's total points across the new item bank."""
        total = Decimal(str(total or 0))
        count = len(items)
        if count == 0:
            return items
        base = (total / count).quantize(Decimal("0.01"))
        allocated = [base] * count
        allocated[-1] += total - (base * count)
        for item, point in zip(items.values(), allocated):
            item["point"] = (
                int(point) if point == point.to_integral_value() else float(point)
            )
        return items

    # -- topics -------------------------------------------------------------
    def _sync_topics(self, vc):
        content = get_content(vc.subject.name if vc.subject else "")
        desired = content["topics"]
        concepts = content["concepts"]

        topics = list(vc.topics.order_by("order", "id"))
        generic = [topic for topic in topics if topic.name in GENERIC_TOPIC_NAMES]

        if not generic:
            if not topics:
                for order, name in enumerate(desired, start=1):
                    Topic.objects.create(
                        virtual_classroom=vc, name=name, order=order
                    )
                return len(desired)
            return 0

        changed = 0
        for index, topic in enumerate(generic):
            if index < len(desired):
                name = desired[index]
            else:
                concept = concepts[index % len(concepts)][0]
                name = "Unit {}: {}".format(index + 1, concept)
            new_order = index + 1
            if topic.name != name or topic.order != new_order:
                topic.name = name
                topic.order = new_order
                topic.save(update_fields=["name", "order"])
                changed += 1
        return changed

    # -- materials ----------------------------------------------------------
    def _clean_cross_subject_placements(self, vcs):
        removed = 0
        placements = LearningResourcePlacement.objects.select_related(
            "resource", "virtual_classroom"
        )
        for placement in placements:
            vc = placement.virtual_classroom
            if placement.resource.subject_id != vc.subject_id:
                placement.delete()
                removed += 1
        return removed

    def _sync_materials(self, vc):
        subject = vc.subject
        year = vc.academic_year
        content = get_content(subject.name)
        materials = content["materials"]
        concepts = content["concepts"]

        changed = 0
        generic = [
            resource
            for resource in LearningResource.objects.filter(
                subject=subject, academic_year=year
            ).order_by("title")
            if resource.title.startswith(GENERIC_RESOURCE_PREFIXES)
        ]
        for index, resource in enumerate(generic):
            if index < len(materials):
                title, resource_type = materials[index]
            else:
                concept = concepts[index % len(concepts)][0]
                title = "{} Supplementary Material: {}".format(subject.name, concept)
                resource_type = "document"
            if (
                LearningResource.objects.filter(
                    subject=subject, academic_year=year, title=title
                )
                .exclude(pk=resource.pk)
                .exists()
            ):
                title = "{} ({})".format(title, resource.pk)
            resource.title = title[:200]
            resource.resource_type = resource_type
            resource.description = "Subject-specific material for {}.".format(subject.name)
            resource.save()
            changed += 1

        # Retitle the shared repository pack so it does not read generically.
        shared = LearningResource.objects.filter(
            subject=subject,
            academic_year=year,
            title__startswith=SHARED_RESOURCE_TITLE_PREFIX,
        )
        for resource in shared:
            resource.description = (
                "A shared lesson pack for {}, contributed to the subject-level "
                "repository.".format(subject.name)
            )
            resource.save(update_fields=["description"])
        return changed

"""Attach example My Class content to a teacher's Virtual Classrooms.

The teacher dashboard's "My Class" tabs read real rows from the classroom
payload: Teaching & Learning Activities come from Topics + placed
LearningResources, and Submissions come from the assessment assignments'
submissions. ``seed_sample_data`` already gives every classroom topics,
resources and graded submissions, but it only ever writes URLs (every
LearningResource has an ``external_link`` and no ``file``), so the TLA tab has
nothing to "upload" or download and no assessment carries an attached file.

This command adds, for each of the teacher's active classrooms (default:
``lovelynmalanot@gmail.com``):

  * Three named topics.
  * A presentation, a PDF, an image, a document and a link, placed across the
    topics, so every material icon type is represented.
  * A file attached to the classroom's assessment (the "uploaded assessment
    file" the Assessment tab shows).

Every write is idempotent: topics, resources and placements use
``get_or_create`` and a part file is only attached when empty, so re-running
never duplicates a row. Pass ``--teacher`` to target another teacher and
``--vc`` to limit to a single Virtual Classroom.
"""

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from apps.assessments.models import AssessmentPart
from apps.accounts.models import User
from apps.teaching_learning.models import (
    LearningResource,
    LearningResourcePlacement,
    Topic,
)
from apps.virtual_classrooms.models import VirtualClassroom


TOPIC_NAMES = [
    "Unit 1: Introduction & Foundations",
    "Unit 2: Core Concepts & Practice",
    "Unit 3: Performance Task & Reflection",
]

# (title suffix, resource_type, topic index, file extension)
RESOURCE_SPECS = [
    ("Lesson Presentation", "presentation", 0, "pptx"),
    ("Reading Material", "pdf", 0, "pdf"),
    ("Concept Diagram", "image", 0, "png"),
    ("Activity Sheet", "document", 1, "docx"),
]

PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n%%EOF\n"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
OOXML_BYTES = b"PK\x03\x04\x14\x00\x00\x00\x00\x00" + b"\x00" * 48


def _file_bytes(ext):
    if ext == "pdf":
        return PDF_BYTES
    if ext == "png":
        return PNG_BYTES
    # pptx and docx are both OOXML zip containers; the bytes only need to be a
    # downloadable placeholder for the demo.
    return OOXML_BYTES


class Command(BaseCommand):
    help = "Seed My Class topics, resource files and an assessment file for a teacher."

    def add_arguments(self, parser):
        parser.add_argument(
            "--teacher",
            default="lovelynmalanot@gmail.com",
            help="Teacher email whose classrooms to seed.",
        )
        parser.add_argument(
            "--vc",
            type=int,
            default=None,
            help="Limit to a single Virtual Classroom id.",
        )

    def handle(self, *args, **options):
        # DISABLED: this command seeds topics, materials and placements for a
        # teacher's classrooms, which would repopulate TLA rows the team is
        # going to enter themselves. Remove these two lines to re-enable.
        self.stdout.write(
            self.style.WARNING(
                "seed_teacher_demo is disabled; no topics or materials were created."
            )
        )
        return

        teacher = User.objects.filter(email=options["teacher"]).first()
        if teacher is None:
            self.stderr.write(self.style.ERROR("Teacher not found."))
            return

        vcs = VirtualClassroom.objects.filter(teacher=teacher, status="active")
        if options["vc"]:
            vcs = vcs.filter(pk=options["vc"])
        vcs = vcs.select_related("subject", "academic_year")

        if not vcs:
            self.stderr.write(self.style.ERROR("No active Virtual Classrooms for this teacher."))
            return

        for vc in vcs:
            self._seed_vc(teacher, vc)
            self.stdout.write(self.style.SUCCESS("Seeded classroom {} ({})".format(vc.pk, vc.subject)))

        self.stdout.write(self.style.SUCCESS("Done."))

    def _seed_vc(self, teacher, vc):
        subject_name = vc.subject.name if vc.subject else "Lesson"

        topics = []
        for order, name in enumerate(TOPIC_NAMES, start=1):
            topic, _ = Topic.objects.get_or_create(
                virtual_classroom=vc, name=name, defaults={"order": order}
            )
            topics.append(topic)

        for suffix, rtype, topic_idx, ext in RESOURCE_SPECS:
            title = "{} – {}".format(suffix, subject_name)
            resource = LearningResource.objects.filter(
                subject=vc.subject, uploaded_by=teacher, title=title
            ).first()
            if resource is None:
                resource = LearningResource(
                    subject=vc.subject,
                    academic_year=vc.academic_year,
                    uploaded_by=teacher,
                    title=title,
                    description="Example {} for {}.".format(rtype, subject_name),
                    resource_type=rtype,
                    share_to_repository=True,
                )
                resource.file.save(
                    "demo_{}_{}.{}".format(vc.pk, rtype, ext),
                    ContentFile(_file_bytes(ext)),
                    save=False,
                )
                resource.save()
            LearningResourcePlacement.objects.get_or_create(
                resource=resource,
                virtual_classroom=vc,
                topic=topics[topic_idx],
                defaults={"placed_by": teacher},
            )

        # Reference link resource.
        link_title = "Reference Video – {}".format(subject_name)
        link = LearningResource.objects.filter(
            subject=vc.subject, uploaded_by=teacher, title=link_title
        ).first()
        if link is None:
            link = LearningResource.objects.create(
                subject=vc.subject,
                academic_year=vc.academic_year,
                uploaded_by=teacher,
                title=link_title,
                description="Example external link for {}".format(subject_name),
                resource_type="link",
                external_link="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                share_to_repository=True,
            )
        LearningResourcePlacement.objects.get_or_create(
            resource=link,
            virtual_classroom=vc,
            topic=topics[1],
            defaults={"placed_by": teacher},
        )

        # Attach a file to the classroom's assessment, if it has none.
        assignment = (
            vc.assessment_assignments.select_related("assessment").order_by("pk").first()
        )
        if assignment is None:
            return
        assessment = assignment.assessment
        part = assessment.parts.order_by("order").first()
        if part is None:
            part = AssessmentPart.objects.create(
                assessment=assessment,
                question_type=AssessmentPart.TYPE_ESSAY,
                question_text="Performance Task",
                order=1,
                items={
                    "item_1": {
                        "question": "Submit your completed performance task.",
                        "point": 10,
                    }
                },
            )
        if not part.attached_file:
            part.attached_file.save(
                "assessment_{}_instructions.pdf".format(assessment.pk),
                ContentFile(PDF_BYTES),
                save=True,
            )

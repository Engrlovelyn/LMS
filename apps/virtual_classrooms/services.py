"""Helpers that bring a newly created Virtual Classroom up to spec:
- pre-populate its Topic outline from the subject's TopicTemplate(s)
- auto-enroll the section's enrolled students for core (applied) subjects
"""

from apps.academic_structure.models import StudentEnrollment, Subject

from .models import VirtualClassroomEnrollment


def populate_classroom_defaults(classroom, auto_enroll=True):
    """Populate topics and (optionally) auto-enroll students for a new VC.

    Both operations are idempotent — safe to call on an existing classroom.
    """
    from apps.teaching_learning.models import Topic

    if not classroom.topics.exists():
        templates = list(classroom.subject.topic_templates.all())
        if templates:
            Topic.objects.bulk_create(
                [
                    Topic(
                        virtual_classroom=classroom,
                        name=template.name,
                        order=template.order,
                    )
                    for template in templates
                ]
            )

    if auto_enroll and classroom.subject.subject_type == Subject.TYPE_CORE:
        enrolled_ids = set(
            StudentEnrollment.objects.filter(
                section=classroom.section,
                academic_year=classroom.academic_year,
                status=StudentEnrollment.STATUS_ENROLLED,
            ).values_list("student_id", flat=True)
        )
        existing_ids = set(
            VirtualClassroomEnrollment.objects.filter(
                virtual_classroom=classroom
            ).values_list("student_id", flat=True)
        )
        new_ids = enrolled_ids - existing_ids
        if new_ids:
            VirtualClassroomEnrollment.objects.bulk_create(
                [
                    VirtualClassroomEnrollment(
                        virtual_classroom=classroom, student_id=student_id
                    )
                    for student_id in new_ids
                ]
            )
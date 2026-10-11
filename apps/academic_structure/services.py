"""Academic-year rollover logic.

Both entry points call `rollover_year` so they cannot drift apart:
- the Django page `academic_structure.views.year_rollover`
- the JSON bridge `POST /api/academic-year/` with `action=rollover`
  (used by the System Administrator dashboard's School Year view)

Direction is always "copy out of `source_year`, into `target_year`", which is
what the System Administrator needs: roll last year's sections, subjects and
classes into the current active year.
"""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.virtual_classrooms.models import VirtualClassroom
from apps.virtual_classrooms.services import pick_section_teacher


@dataclass
class RolloverReport:
    sections: int = 0
    subjects: int = 0
    classes: int = 0
    skipped: list = field(default_factory=list)

    @property
    def copied(self):
        return self.sections + self.subjects + self.classes

    def summary(self):
        return (
            f"Copied {self.sections} sections, {self.subjects} subjects "
            f"and {self.classes} virtual classrooms."
        )


def _skip(kind, label, exc):
    messages = getattr(exc, "messages", None)
    reason = "; ".join(messages) if messages else "already present in the target year"
    return f'{kind} "{label}" skipped: {reason}'


def rollover_year(source_year, target_year):
    """Copy the active structure of `source_year` into `target_year`.

    Each item is written inside its own savepoint, so a unique-constraint
    collision (the item already exists in the target year) is recorded in
    `RolloverReport.skipped` instead of aborting the whole rollover.

    Raises ValidationError for combinations that make no sense.
    """
    if source_year is None or target_year is None:
        raise ValidationError("Both a source and a target academic year are required.")
    if source_year.pk == target_year.pk:
        raise ValidationError("The source and target academic year must be different.")
    if target_year.status == AcademicYear.STATUS_ARCHIVED:
        raise ValidationError(
            f"{target_year.label} is archived and cannot receive rolled-over data. "
            "Activate it first."
        )

    report = RolloverReport()
    section_map = {}
    subject_map = {}

    with transaction.atomic():
        for section in Section.objects.filter(
            academic_year=source_year, status=Section.STATUS_ACTIVE
        ):
            try:
                with transaction.atomic():
                    section_map[section.pk] = Section.objects.create(
                        academic_year=target_year,
                        name=section.name,
                        grade_level=section.grade_level,
                        adviser=section.adviser,
                        status=Section.STATUS_ACTIVE,
                    )
                report.sections += 1
            except (IntegrityError, ValidationError) as exc:
                report.skipped.append(_skip("Section", str(section), exc))

        for subject in Subject.objects.filter(
            academic_year=source_year, status=Subject.STATUS_ACTIVE
        ):
            try:
                with transaction.atomic():
                    subject_map[subject.pk] = Subject.objects.create(
                        academic_year=target_year,
                        name=subject.name,
                        grade_level=subject.grade_level,
                        subject_type=subject.subject_type,
                        grading_period=subject.grading_period,
                        status=Subject.STATUS_ACTIVE,
                    )
                report.subjects += 1
            except (IntegrityError, ValidationError) as exc:
                report.skipped.append(_skip("Subject", str(subject), exc))

        for classroom in VirtualClassroom.objects.filter(
            academic_year=source_year, status=VirtualClassroom.STATUS_ACTIVE
        ).select_related("section", "subject"):
            target_section = section_map.get(classroom.section_id)
            target_subject = subject_map.get(classroom.subject_id)
            if not target_section or not target_subject:
                report.skipped.append(
                    'Virtual classroom "{}" skipped: its section or subject was not copied.'
                    .format(classroom)
                )
                continue
            # Carry the same subject teacher into the new year when that person
            # is free in the target section; otherwise hand it to whoever is.
            teacher = pick_section_teacher(
                target_section, preferred=classroom.teacher
            )
            try:
                with transaction.atomic():
                    created = VirtualClassroom.objects.create(
                        academic_year=target_year,
                        section=target_section,
                        subject=target_subject,
                        teacher=teacher,
                        grading_period=classroom.grading_period,
                        status=VirtualClassroom.STATUS_ACTIVE,
                    )
                from apps.virtual_classrooms.services import populate_classroom_defaults

                populate_classroom_defaults(created)
                report.classes += 1
            except (IntegrityError, ValidationError) as exc:
                report.skipped.append(_skip("Virtual classroom", str(classroom), exc))

    return report

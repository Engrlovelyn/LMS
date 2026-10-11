"""Import the official student list and subject assignments from a Word document.

The source document (``STUDENT_LIST_AND_SUBJECTS_2026-2027.docx``) is laid out as:

* Table 0            - class summary (grade, section, strand, adviser, count)
* Tables 1, 2, ...   - per class: a Subjects table, then a Students table

This command creates Users, EmployeeProfiles, StudentProfiles, Sections,
Subjects, VirtualClassrooms and StudentEnrollments from that document.

All operations are idempotent — safe to run repeatedly. Use ``--dry-run`` first.
"""

import random
import re
from contextlib import nullcontext
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.academic_structure.models import (
    Section,
    StudentEnrollment,
    Subject,
)
from apps.accounts.models import (
    EmployeeProfile,
    Role,
    RoleAssignment,
    StudentProfile,
    User,
)
from apps.accounts.demo_passwords import default_password_for
from apps.virtual_classrooms.models import VirtualClassroom

# Document "Type" values mapped onto Subject.SUBJECT_TYPE_CHOICES.
# Subject.TYPE_CORE is stored as "APPLIED" (see Subject.TYPE_CORE), so Core and
# Specialized subjects both map to it and are auto-enrolled by
# populate_classroom_defaults(). Only genuine electives stay un-enrolled.
TYPE_MAP = {
    "core": Subject.TYPE_CORE,
    "specialized": Subject.TYPE_CORE,
    "applied": Subject.TYPE_CORE,
    "elective": Subject.TYPE_ELECTIVE,
}

EMAIL_DOMAIN = "gmail.com"
EMPLOYEE_NUMBER_START = 1001
SCHOOL_YEAR_END = 2027


def slugify_name(value):
    value = re.sub(r"[^a-z0-9]+", ".", value.strip().lower())
    return value.strip(".")


def parse_document(path):
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise CommandError(
            "python-docx is required to read the source document. "
            "Install it with: pip install python-docx"
        ) from exc

    document = docx.Document(path)
    tables = document.tables
    if len(tables) < 2:
        raise CommandError("Document does not contain the expected tables.")

    def rows(table):
        return [[cell.text.strip() for cell in row.cells] for row in table.rows]

    summary = rows(tables[0])[1:]
    classes = []
    index = 1
    while index < len(tables) - 1:
        subjects = rows(tables[index])[1:]
        students = rows(tables[index + 1])[1:]
        classes.append({"subjects": subjects, "students": students})
        index += 2
    return summary, classes


def grade_to_level(label):
    """'Grade 7' -> 7. Returns None for levels the schema cannot store."""
    match = re.search(r"(\d+)", label)
    return int(match.group(1)) if match else None


class Command(BaseCommand):
    help = "Import students, sections and subject assignments from a Word document"

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the .docx student list")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing anything",
        )
        parser.add_argument(
            "--skip-grades",
            default="",
            help="Comma separated grade labels to skip, e.g. 'Kinder'",
        )

    def handle(self, *args, **options):
        path = options["path"]
        dry_run = options["dry_run"]
        skip_grades = {g.strip() for g in options["skip_grades"].split(",") if g.strip()}

        summary, classes = parse_document(path)

        academic_year = self._academic_year()
        teacher_role = self._role("teacher")
        student_role = self._role("student")

        self.stdout.write(f"Academic year : {academic_year.label}")
        self.stdout.write(f"Source        : {path}")
        self.stdout.write(f"Classes found : {len(summary)}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - no changes will be written"))

        stats = {
            "sections": 0, "subjects": 0, "classrooms": 0,
            "students": 0, "teachers": 0, "skipped": 0,
        }
        seen_subjects = set()

        rng = random.Random(20262027)
        context = {
            "academic_year": academic_year,
            "teacher_role": teacher_role,
            "student_role": student_role,
            "dry_run": dry_run,
            "stats": stats,
            "seen_subjects": seen_subjects,
            "rng": rng,
        }

        wrapper = nullcontext() if dry_run else transaction.atomic()
        with wrapper:
            for row, payload in zip(summary, classes):
                grade_label = row[1]
                section_name = row[2]
                adviser_name = row[4]

                if grade_label in skip_grades:
                    stats["skipped"] += 1
                    self.stdout.write(
                        self.style.WARNING(f"  SKIP {grade_label} - {section_name}")
                    )
                    continue

                level = grade_to_level(grade_label)
                if level is None or not 1 <= level <= 12:
                    stats["skipped"] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"  SKIP {grade_label} - {section_name} "
                            "(no valid grade_level 1-12)"
                        )
                    )
                    continue

                self.stdout.write(f"  {grade_label} - {section_name}  ({adviser_name})")
                adviser = self._adviser(adviser_name, context)
                section = self._section(academic_year, level, section_name, adviser, context)

                # Enrol students BEFORE creating classrooms: auto-enrolment reads
                # StudentEnrollment, so classrooms built first would enrol nobody.
                for student_row in payload["students"]:
                    self._student(academic_year, section, level, student_row, context)

                for subject_row in payload["subjects"]:
                    subject = self._subject(academic_year, level, subject_row, context)
                    if context["dry_run"]:
                        self._classroom(academic_year, section, None, adviser, context)
                    elif subject is not None:
                        self._classroom(academic_year, section, subject, adviser, context)

        self._report(stats, dry_run)

    # ------------------------------------------------------------------ helpers

    def _academic_year(self):
        from apps.academic_structure.models import AcademicYear

        academic_year = AcademicYear.objects.filter(status="ACTIVE").first()
        if academic_year is None:
            raise CommandError("No ACTIVE AcademicYear. Create one first.")
        return academic_year

    def _role(self, code):
        role = Role.objects.filter(code=code).first()
        if role is None:
            raise CommandError(f"Role '{code}' is missing. Run seed_official_accounts.")
        return role

    def _adviser(self, full_name, context):
        parts = [p for p in re.split(r"\s+", full_name.strip()) if p]
        if not parts:
            raise CommandError(f"Unparseable adviser name: '{full_name}'")

        first_name = parts[0]
        last_name = parts[-1] if len(parts) > 1 else ""
        middle_initial = parts[1][0] if len(parts) > 2 else ""

        # Idempotency: match on the existing employee record before minting an
        # email, so re-running never produces a second adviser for one person.
        existing = User.objects.filter(
            employee_profile__first_name=first_name,
            employee_profile__last_name=last_name,
        ).first()
        if existing is not None:
            RoleAssignment.objects.get_or_create(
                user=existing,
                role=context["teacher_role"],
                academic_year=context["academic_year"],
                defaults={"date_assigned": timezone.now()},
            )
            return existing

        base = slugify_name(" ".join(parts))
        email = f"{base}@{EMAIL_DOMAIN}"
        counter = 1
        while User.objects.filter(email=email).exists():
            counter += 1
            email = f"{base}{counter}@{EMAIL_DOMAIN}"

        if context["dry_run"]:
            self.stdout.write(f"      adviser {email}")
            context["stats"]["teachers"] += 1
            return None

        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "status": User.Status.ACTIVE,
                "is_active": True,
                "password_changed_at": timezone.now(),
            },
        )
        if created:
            user.set_password(default_password_for(prof.first_name))
            user.save(update_fields=["password"])
            context["stats"]["teachers"] += 1

        if not EmployeeProfile.objects.filter(user=user).exists():
            number = EMPLOYEE_NUMBER_START + context["stats"]["teachers"]
            while EmployeeProfile.objects.filter(employee_number=f"EMP-{number:04d}").exists():
                number += 1
            EmployeeProfile.objects.create(
                user=user,
                employee_number=f"EMP-{number:04d}",
                first_name=first_name,
                last_name=last_name,
                middle_initial=middle_initial,
                suffix="",
                contact_number="",
            )

        RoleAssignment.objects.get_or_create(
            user=user,
            role=context["teacher_role"],
            academic_year=context["academic_year"],
            defaults={"date_assigned": timezone.now()},
        )
        return user

    def _section(self, academic_year, level, name, adviser, context):
        if context["dry_run"]:
            context["stats"]["sections"] += 1
            return None
        section, created = Section.objects.get_or_create(
            academic_year=academic_year,
            grade_level=level,
            name=name,
            defaults={"adviser": adviser, "status": Section.STATUS_ACTIVE},
        )
        if created:
            context["stats"]["sections"] += 1
        elif adviser and section.adviser_id != adviser.id:
            section.adviser = adviser
            section.save(update_fields=["adviser"])
        return section

    def _subject(self, academic_year, level, row, context):
        name = row[1]
        raw_type = row[2] if len(row) > 2 else "Core"
        subject_type = TYPE_MAP.get(raw_type.strip().lower(), Subject.TYPE_CORE)

        if context["dry_run"]:
            self.stdout.write(f"      subject {name} ({subject_type})")
            key = (level, name)
            if key not in context["seen_subjects"]:
                context["seen_subjects"].add(key)
                context["stats"]["subjects"] += 1
            return None

        subject, created = Subject.objects.get_or_create(
            academic_year=academic_year,
            name=name,
            grade_level=level,
            grading_period="",
            defaults={"subject_type": subject_type, "status": Subject.STATUS_ACTIVE},
        )
        if created:
            context["stats"]["subjects"] += 1
        return subject

    def _classroom(self, academic_year, section, subject, adviser, context):
        if context["dry_run"]:
            context["stats"]["classrooms"] += 1
            return
        classroom, created = VirtualClassroom.objects.get_or_create(
            academic_year=academic_year,
            section=section,
            subject=subject,
            grading_period="",
            defaults={
                "teacher": pick_section_teacher(section, preferred=adviser),
                "status": VirtualClassroom.STATUS_ACTIVE,
            },
        )
        if created:
            context["stats"]["classrooms"] += 1
        # Safe to call on existing classrooms - it is idempotent - and needed to
        # backfill enrolments when students were imported after the classroom.
        from apps.virtual_classrooms.services import (
            pick_section_teacher,
            populate_classroom_defaults,
        )
        populate_classroom_defaults(classroom, auto_enroll=True)

    def _student(self, academic_year, section, level, row, context):
        # Columns: No. | Student ID | First Name | Last Name | Sex | Email | Password
        cells = list(row) + [""] * 7
        lrn, first_name, last_name, sex, bare_email, password = cells[1:7]

        if not lrn or not bare_email:
            return

        email = bare_email if "@" in bare_email else f"{bare_email}@{EMAIL_DOMAIN}"

        if context["dry_run"]:
            self.stdout.write(f"      student {lrn} {first_name} {last_name} <{email}>")
            context["stats"]["students"] += 1
            return

        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "status": User.Status.ACTIVE,
                "is_active": True,
                "password_changed_at": timezone.now(),
            },
        )
        if created:
            user.set_password(password or f"{email}12345")
            user.save(update_fields=["password"])
            context["stats"]["students"] += 1

        profile, _ = StudentProfile.objects.get_or_create(
            user=user,
            defaults={
                "lrn": lrn,
                "first_name": first_name,
                "last_name": last_name,
                "middle_initial": "",
                "suffix": "",
                "sex": "M" if sex.lower().startswith("m") else "F",
                "birthdate": self._birthdate(level, context["rng"]),
                "contact_number": "",
                "address": "",
                "guardian_name": "",
                "guardian_email": "",
                "guardian_contact_number": "",
            },
        )

        RoleAssignment.objects.get_or_create(
            user=user,
            role=context["student_role"],
            academic_year=academic_year,
            defaults={"date_assigned": timezone.now()},
        )

        StudentEnrollment.objects.get_or_create(
            student=profile,
            academic_year=academic_year,
            defaults={"section": section, "status": StudentEnrollment.STATUS_ENROLLED},
        )

    def _birthdate(self, level, rng):
        age = 5 + level
        year = SCHOOL_YEAR_END - age
        return date(year, rng.randint(1, 12), rng.randint(1, 28))

    def _report(self, stats, dry_run):
        prefix = "Would create" if dry_run else "Created"
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "{prefix}: {sections} sections, {subjects} subjects, "
                "{classrooms} classrooms, {students} students, "
                "{teachers} teachers ({skipped} classes skipped)".format(
                    prefix=prefix, **stats
                )
            )
        )

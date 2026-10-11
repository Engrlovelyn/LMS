"""Import students (and section subjects) from ``TASS_Student_Roster.xlsx``.

Workbook layout expected by this command:

* ``Students``        - Student ID | Last Name | First Name | M.I. | Grade Level |
                        Section | Adviser | Email | Password
* ``Class_Subjects``  - Grade Level | Section | Subject 1..5 | Elective (SHS only)

The roster ships a ``Student ID`` of the form ``TASS-1001`` and no birthdate or
sex column, so this command fills those in:

* **LRN** - 12-digit numbers are generated sequentially (``--lrn-start``,
  default ``202700000001``) because ``StudentProfile.lrn`` is validated as
  exactly 12 digits. The source ``Student ID`` is not storable in that field.
* **Email** - the source address is kept, but suffixed (``jose@gmail.com`` ->
  ``jose2@gmail.com``) whenever it is already taken by another account, since
  ``User.email`` is unique and is the login identity.
* **Password** - taken from the file and the account is activated, so no
  verification mail is sent.
* **Birthdate** - required by the model but absent from the file, so a
  plausible one is generated for the grade level, seeded from the source
  ``Student ID`` so repeated runs agree.
* **Sex** - left blank; the file does not carry it.

Sections are matched to the existing ``Section`` rows case-insensitively and
with any trailing strand label (``St. Francis Xavier (STEM)``) stripped, so the
import never creates duplicate sections. Subjects and virtual classrooms are
created only where missing, so importing is additive.

Every operation is idempotent - safe to run repeatedly. Use ``--dry-run`` first.
"""

import os
import random
import re
from contextlib import nullcontext
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.academic_structure.models import AcademicYear, Section, StudentEnrollment, Subject
from apps.accounts.models import (
    PasswordHistory,
    Role,
    RoleAssignment,
    StudentProfile,
    User,
)
from apps.virtual_classrooms.models import VirtualClassroom
from apps.virtual_classrooms.services import (
    pick_section_teacher,
    populate_classroom_defaults,
)

EMAIL_DOMAIN = "gmail.com"
LRN_DIGITS = 12

# Section names in the roster that differ from the Section rows already in the
# database by more than a strand label or punctuation.
SECTION_ALIASES = {
    "roch": "st roch",
}


def normalise_section(name):
    """Fold a section label down to a comparable key.

    Lowercases, drops a trailing ``(STRAND)`` label and all punctuation, so
    ``St. Francis Xavier (STEM)`` and ``St. Francis Xavier`` both become
    ``st francis xavier``.
    """
    value = str(name or "").lower().strip()
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = " ".join(value.split())
    return SECTION_ALIASES.get(value, value)


def grade_to_level(label):
    """``"Grade 7"`` -> ``7``."""
    match = re.search(r"(\d+)", str(label or ""))
    return int(match.group(1)) if match else None


def age_bracket(level, reference_year):
    """Return the plausible (min_age, max_age) for a grade level.

    Kindergarten is age 5 in the Philippines, so Grade N sits around ``5 + N``.
    A one-year band either side keeps a whole section from sharing a birth year.
    """
    nominal = 5 + level
    return reference_year - nominal - 1, reference_year - nominal


class Command(BaseCommand):
    help = "Import students and section subjects from TASS_Student_Roster.xlsx"

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            nargs="?",
            default=r"C:\Users\HP\Downloads\TASS_Student_Roster.xlsx",
            help="Path to the .xlsx roster",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written without touching the database",
        )
        parser.add_argument(
            "--lrn-start",
            type=int,
            default=202700000001,
            help="First generated 12-digit LRN (default: 202700000001)",
        )
        parser.add_argument(
            "--skip-subjects",
            action="store_true",
            help="Only import students and enrolments",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print one line per student",
        )

    def handle(self, *args, **options):
        path = options["path"]
        dry_run = options["dry_run"]
        verbose = options["verbose"]

        if not os.path.exists(path):
            raise CommandError(f"Roster not found: {path}")

        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover
            raise CommandError(
                "openpyxl is required to read .xlsx rosters. Install it with: pip install openpyxl"
            ) from exc

        workbook = openpyxl.load_workbook(path, data_only=True)
        if "Students" not in workbook.sheetnames:
            raise CommandError("Workbook has no 'Students' sheet")

        academic_year = AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).first()
        if academic_year is None:
            raise CommandError("No ACTIVE AcademicYear. Create one first.")

        student_role = Role.objects.filter(code="student").first()
        if student_role is None:
            raise CommandError("Role 'student' is missing. Run seed_official_accounts.")

        self.stdout.write(f"Roster      : {path}")
        self.stdout.write(f"Sheets      : {', '.join(workbook.sheetnames)}")
        self.stdout.write(f"Academic year: {academic_year.label}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - nothing will be written"))
        self.stdout.write("")

        sections = self._load_sections(academic_year)
        students = self._read_students(workbook["Students"])
        roster_sections = {(row["level"], normalise_section(row["section"])) for row in students}
        self.stdout.write(f"Students    : {len(students)} rows, {len(roster_sections)} distinct sections")
        self.stdout.write("")

        stats = {
            "students_created": 0,
            "students_updated": 0,
            "enrollments_created": 0,
            "subjects_created": 0,
            "classrooms_created": 0,
            "skipped": 0,
        }
        renamed = []
        unmapped = {}
        collisions = 0

        taken_lrns = set(StudentProfile.objects.values_list("lrn", flat=True))
        taken_emails = set(User.objects.values_list("email", flat=True))
        lrn_cursor = options["lrn_start"]
        reference_year = academic_year.end_date.year

        # Every address the roster declares, so that renaming one student never
        # steals the address another roster row already claims for itself (the
        # roster disambiguates duplicate first names with its own "2" suffix).
        declared_emails = {
            (row["email"] or "").strip().lower() for row in students if row["email"]
        }
        claimed_emails = set()

        # Rows this command created on a previous run, keyed by natural key, so
        # a re-run adopts them instead of allocating yet another address. Only
        # rows whose LRN sits in this command's generated 12-digit block are
        # claimed, so pre-existing students are never hijacked by a name match.
        lrn_block = str(options["lrn_start"])[:4]
        imported = {}
        for enrollment in (
            StudentEnrollment.objects.filter(
                academic_year=academic_year,
                status=StudentEnrollment.STATUS_ENROLLED,
            )
            .select_related("student", "student__user", "section")
        ):
            profile = enrollment.student
            lrn = profile.lrn or ""
            if not (lrn.isdigit() and len(lrn) == LRN_DIGITS and lrn.startswith(lrn_block)):
                continue
            key = (
                (profile.first_name or "").strip().lower(),
                (profile.last_name or "").strip().lower(),
                (profile.middle_initial or "").strip().lower(),
                enrollment.section.grade_level,
                normalise_section(enrollment.section.name),
            )
            imported.setdefault(key, profile)

        def natural_key(row, section):
            return (
                row["first_name"].strip().lower(),
                row["last_name"].strip().lower(),
                row["middle_initial"].strip().lower(),
                section.grade_level,
                normalise_section(section.name),
            )

        def allocate_lrn():
            nonlocal lrn_cursor
            while str(lrn_cursor) in taken_lrns:
                lrn_cursor += 1
            value = str(lrn_cursor)
            taken_lrns.add(value)
            lrn_cursor += 1
            return value

        def allocate_email(preferred, seed):
            """Return (email, renamed_bool) keeping User.email unique.

            ``preferred`` is kept when nothing else holds it. Otherwise a counter
            is appended to the local part, skipping both addresses already in the
            database and addresses the roster has declared for another row.
            """
            nonlocal collisions
            preferred = (preferred or "").strip().lower()
            if not preferred:
                preferred = f"{seed}{EMAIL_DOMAIN}"
            if "@" not in preferred:
                preferred = f"{preferred}@{EMAIL_DOMAIN}"

            if preferred not in taken_emails and preferred not in claimed_emails:
                claimed_emails.add(preferred)
                return preferred, False

            local, domain = preferred.split("@", 1)
            for counter in range(2, 1000):
                candidate = f"{local}{counter}@{domain}"
                if candidate in taken_emails or candidate in declared_emails:
                    continue
                if candidate in claimed_emails:
                    continue
                claimed_emails.add(candidate)
                collisions += 1
                return candidate, True
            raise CommandError(f"Could not find a free address for {preferred}")

        wrapper = nullcontext() if dry_run else transaction.atomic()
        with wrapper:
            # Students are enrolled BEFORE classrooms are created:
            # populate_classroom_defaults() reads StudentEnrollment to decide who
            # to auto-enrol, so classrooms built first would enrol nobody.
            for row in students:
                student_id = row["student_id"]
                level = row["level"]
                section = sections.get((level, normalise_section(row["section"])))
                if section is None:
                    stats["skipped"] += 1
                    key = (level, row["section"])
                    unmapped[key] = unmapped.get(key, 0) + 1
                    continue

                profile = imported.get(natural_key(row, section))
                if profile is not None:
                    # Already imported by an earlier run - reuse it as-is so the
                    # LRN, address and login stay stable. Checked before an
                    # address is allocated, since a re-run must not burn a
                    # fresh address it will only throw away.
                    user = profile.user
                    stats["students_updated"] += 1
                    if verbose:
                        self.stdout.write(
                            f"  G{level:<3}{section.name:<34}{row['last_name']}, {row['first_name']:<14}"
                            f"{profile.lrn}  {user.email}  (existing)"
                        )
                    if dry_run:
                        continue
                    user.status = User.Status.ACTIVE
                    user.save(update_fields=["status", "is_active"])
                    self._sync_profile(profile, row, student_id, level, reference_year)
                    self._assign_student_role(user, student_role, academic_year)
                    self._enrol(profile, academic_year, section, stats)
                    continue

                email, was_renamed = allocate_email(row["email"], student_id)
                if was_renamed:
                    renamed.append((student_id, row["email"], email))

                user = User.objects.filter(email=email).first()
                lrn = allocate_lrn()

                if verbose:
                    self.stdout.write(
                        f"  G{level:<3}{section.name:<34}{row['last_name']}, {row['first_name']:<14}"
                        f"{lrn}  {email}{'  (renamed)' if was_renamed else ''}"
                    )

                if dry_run:
                    stats["students_created"] += 1
                    stats["enrollments_created"] += 1
                    continue

                if user is None:
                    user = User(email=email, status=User.Status.ACTIVE)
                    user.set_password(row["password"] or f"{row['first_name'].lower()}12345")
                    user.password_changed_at = timezone.now()
                    user.save()
                    PasswordHistory.objects.create(user=user, hashed_password=user.password)
                    stats["students_created"] += 1
                elif user.status != User.Status.ACTIVE:
                    user.status = User.Status.ACTIVE
                    user.save(update_fields=["status", "is_active"])

                profile = getattr(user, "student_profile", None)
                if profile is None:
                    profile = StudentProfile(
                        user=user,
                        lrn=lrn,
                        first_name=row["first_name"],
                        last_name=row["last_name"],
                        middle_initial=row["middle_initial"],
                        suffix="",
                        birthdate=self._birthdate(student_id, level, reference_year),
                        sex="",
                        contact_number="",
                        address="",
                        guardian_name="",
                        guardian_email="",
                        guardian_contact_number="",
                    )
                    profile.save()
                    imported[natural_key(row, section)] = profile
                else:
                    self._sync_profile(profile, row, student_id, level, reference_year)
                    stats["students_updated"] += 1

                self._assign_student_role(user, student_role, academic_year)
                self._enrol(profile, academic_year, section, stats)

            if not options["skip_subjects"] and "Class_Subjects" in workbook.sheetnames:
                self._import_subjects(
                    workbook["Class_Subjects"], academic_year, sections, stats, dry_run
                )

        self._report(stats, renamed, unmapped, collisions, dry_run)
        if unmapped:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Some sections in the roster have no matching Section row and were skipped:"
                )
            )
            for (level, name), count in sorted(unmapped.items()):
                self.stdout.write(self.style.WARNING(f"    Grade {level} '{name}' - {count} students"))

    # ------------------------------------------------------------------ helpers

    def _birthdate(self, student_id, level, reference_year):
        """Deterministic, age-appropriate birthdate.

        Seeded from the roster's Student ID so repeated runs of this command
        produce the same date for the same student.
        """
        rng = random.Random(f"{student_id}|{level}|TASS")
        low, high = age_bracket(level, reference_year)
        return date(rng.randint(low, high), rng.randint(1, 12), rng.randint(1, 28))

    def _sync_profile(self, profile, row, student_id, level, reference_year):
        """Copy the roster's name data onto an existing profile."""
        profile.first_name = row["first_name"]
        profile.last_name = row["last_name"]
        if row["middle_initial"]:
            profile.middle_initial = row["middle_initial"]
        if profile.birthdate is None:
            profile.birthdate = self._birthdate(student_id, level, reference_year)
        profile.save()

    def _assign_student_role(self, user, student_role, academic_year):
        RoleAssignment.objects.get_or_create(
            user=user,
            role=student_role,
            academic_year=academic_year,
            defaults={"date_assigned": timezone.now()},
        )

    def _enrol(self, profile, academic_year, section, stats):
        enrollment, created = StudentEnrollment.objects.get_or_create(
            student=profile,
            academic_year=academic_year,
            status=StudentEnrollment.STATUS_ENROLLED,
            defaults={"section": section},
        )
        if created:
            stats["enrollments_created"] += 1
        elif enrollment.section_id != section.pk:
            enrollment.section = section
            enrollment.save(update_fields=["section"])
        return enrollment

    # ------------------------------------------------------------------ readers

    def _load_sections(self, academic_year):
        """Map (grade_level, normalised name) -> Section for the active year."""
        index = {}
        for section in Section.objects.filter(academic_year=academic_year):
            index.setdefault((section.grade_level, normalise_section(section.name)), section)
        return index

    def _read_students(self, worksheet):
        rows = worksheet.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            raise CommandError("'Students' sheet is empty")

        header = [str(cell or "").strip().lower() for cell in header_row]
        required = ["last name", "first name", "grade level", "section"]
        missing = [column for column in required if column not in header]
        if missing:
            raise CommandError(
                "'Students' sheet is missing column(s): "
                + ", ".join(missing)
                + f". Found: {', '.join(header)}"
            )

        def column(*names, default=None):
            for name in names:
                if name in header:
                    return header.index(name)
            return default

        student_id_at = column("student id", "id", "lrn", default=0)
        last_at = column("last name", "lastname", default=1)
        first_at = column("first name", "firstname", default=2)
        mi_at = column("m.i.", "mi", "middle initial", "middle initial.")
        grade_at = column("grade level", "grade", default=4)
        section_at = column("section", default=5)
        email_at = column("email", default=7)
        password_at = column("password", default=8)

        def cell(cells, index):
            if index is None or index >= len(cells):
                return ""
            value = cells[index]
            return "" if value is None else str(value).strip()

        parsed = []
        for cells in rows:
            if all(value is None or str(value).strip() == "" for value in cells):
                continue
            level = grade_to_level(cell(cells, grade_at))
            parsed.append(
                {
                    "student_id": cell(cells, student_id_at),
                    "last_name": cell(cells, last_at),
                    "first_name": cell(cells, first_at),
                    "middle_initial": cell(cells, mi_at),
                    "level": level,
                    "section": cell(cells, section_at),
                    "email": cell(cells, email_at),
                    "password": cell(cells, password_at),
                }
            )
        return parsed

    def _read_subject_rows(self, worksheet):
        """Return [(level, section_name, [(subject_name, is_elective), ...]), ...].

        The elective column is identified from the header rather than assumed to
        be last, so a roster without it still imports correctly.
        """
        rows = worksheet.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            return []

        header = [str(cell or "").strip().lower() for cell in header_row]
        grade_at = header.index("grade level") if "grade level" in header else 0
        section_at = header.index("section") if "section" in header else 1
        subject_columns = [
            index
            for index, label in enumerate(header)
            if index not in (grade_at, section_at) and label
        ]

        def cell(cells, index):
            if index is None or index >= len(cells):
                return ""
            value = cells[index]
            return "" if value is None else str(value).strip()

        parsed = []
        for cells in rows:
            if all(value is None or str(value).strip() == "" for value in cells):
                continue
            level = grade_to_level(cell(cells, grade_at))
            if level is None:
                continue
            subjects = []
            for index in subject_columns:
                name = cell(cells, index)
                if not name:
                    continue
                is_elective = "elective" in header[index]
                subjects.append((name, is_elective))
            parsed.append((level, cell(cells, section_at), subjects))
        return parsed

    # ------------------------------------------------------------------ writers

    def _import_subjects(self, worksheet, academic_year, sections, stats, dry_run):
        self.stdout.write("Subjects and virtual classrooms")
        for level, section_name, subjects in self._read_subject_rows(worksheet):
            section = sections.get((level, normalise_section(section_name)))
            if section is None:
                self.stdout.write(
                    self.style.WARNING(f"  ! no Section row for Grade {level} '{section_name}' - skipped")
                )
                continue

            created_subjects, created_classrooms = 0, 0
            for subject_name, is_elective in subjects:
                subject_type = Subject.TYPE_ELECTIVE if is_elective else Subject.TYPE_CORE
                if dry_run:
                    subject = Subject(
                        academic_year=academic_year,
                        name=subject_name,
                        grade_level=level,
                        grading_period="",
                        subject_type=subject_type,
                    )
                else:
                    subject, created = Subject.objects.get_or_create(
                        academic_year=academic_year,
                        name=subject_name,
                        grade_level=level,
                        grading_period="",
                        defaults={
                            "subject_type": subject_type,
                            "status": Subject.STATUS_ACTIVE,
                        },
                    )
                    if created:
                        created_subjects += 1

                if dry_run:
                    existing = VirtualClassroom.objects.filter(
                        academic_year=academic_year,
                        section=section,
                        subject__name=subject_name,
                        grading_period="",
                    ).exists()
                    if not existing:
                        created_classrooms += 1
                    continue

                classroom, created = VirtualClassroom.objects.get_or_create(
                    academic_year=academic_year,
                    section=section,
                    subject=subject,
                    grading_period="",
                    defaults={
                        "teacher": pick_section_teacher(
                            section, preferred=section.adviser
                        ),
                        "status": VirtualClassroom.STATUS_ACTIVE,
                    },
                )
                if created:
                    created_classrooms += 1
                # Idempotent, and needed to backfill enrolments when the students
                # for this section are imported after the classroom is created.
                populate_classroom_defaults(classroom, auto_enroll=True)

            stats["subjects_created"] += created_subjects
            stats["classrooms_created"] += created_classrooms
            detail = []
            if created_subjects:
                detail.append(f"{created_subjects} subjects")
            if created_classrooms:
                detail.append(f"{created_classrooms} classrooms")
            suffix = ", ".join(detail) if detail else "already up to date"
            self.stdout.write(f"  Grade {level:<3}{section.name:<36}{len(subjects)} subjects - {suffix}")
        self.stdout.write("")

    def _report(self, stats, renamed, unmapped, collisions, dry_run):
        verb = "Would write" if dry_run else "Wrote"
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "{verb}: {students_created} students, {students_updated} updated, "
                "{enrollments_created} enrolments, {subjects_created} subjects, "
                "{classrooms_created} classrooms, {skipped} skipped".format(verb=verb, **stats)
            )
        )
        self.stdout.write(f"Email collisions resolved by suffixing: {collisions}")
        for student_id, original, replacement in renamed:
            self.stdout.write(f"    {student_id:<12} {original} -> {replacement}")

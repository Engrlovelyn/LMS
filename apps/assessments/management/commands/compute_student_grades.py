from django.core.management.base import BaseCommand

from apps.assessments.models import GradingFormula
from apps.assessments.services import compute_grades_for_classroom


class Command(BaseCommand):
    help = (
        "Compute StudentGrade records for every virtual classroom that has a "
        "configured GradingFormula, or for one classroom via --classroom."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--classroom",
            type=int,
            default=None,
            help="Only compute grades for a specific virtual classroom pk.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report classrooms/students without writing grades.",
        )

    def handle(self, *args, **options):
        formulas = GradingFormula.objects.select_related(
            "virtual_classroom", "virtual_classroom__subject", "virtual_classroom__section"
        )
        if options["classroom"]:
            formulas = formulas.filter(virtual_classroom_id=options["classroom"])

        total_students = 0
        total_grades = 0
        for formula in formulas:
            result = compute_grades_for_classroom(
                formula.virtual_classroom, formula, commit=not options["dry_run"]
            )
            total_students += result["students"]
            total_grades += result["grades"]
            verb = "would compute" if options["dry_run"] else "computed"
            self.stdout.write(
                f"  {formula.virtual_classroom}: {result['grades']} grade(s) "
                f"{verb} for {result['students']} student(s)."
            )
        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"[dry-run] {total_grades} grade record(s) would be computed "
                    f"across {total_students} student(s)."
                )
            )
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"{total_grades} grade record(s) computed across "
                f"{total_students} student(s)."
            )
        )
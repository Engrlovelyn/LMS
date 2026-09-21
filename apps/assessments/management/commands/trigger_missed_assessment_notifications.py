from django.core.management.base import BaseCommand

from apps.assessments.models import Assessment
from apps.assessments.services import run_assessment_notifications


class Command(BaseCommand):
    help = (
        "Run all rule-(j) assessment notification producers (availability, deadline "
        "upcoming/nearing, and missed) for active assessments."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--assessment",
            type=int,
            default=None,
            help="Only process a specific assessment pk.",
        )

    def handle(self, *args, **options):
        assessment = None
        if options["assessment"]:
            assessment = Assessment.objects.get(pk=options["assessment"])

        summary = run_assessment_notifications(assessment)
        self.stdout.write(
            self.style.SUCCESS(
                "Notifications processed: "
                f"availability {summary['availability']['created']} created / "
                f"{summary['availability']['already']} existing; "
                f"deadline {summary['deadline']['created']} created / "
                f"{summary['deadline']['already']} existing; "
                f"missed {summary['missed']['created']} created / "
                f"{summary['missed']['already']} existing."
            )
        )
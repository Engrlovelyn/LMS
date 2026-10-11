from collections import defaultdict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import EmployeeProfile
from apps.virtual_classrooms.models import VirtualClassroom
from apps.virtual_classrooms.services import assignable_teachers


class Command(BaseCommand):
    help = (
        "Re-assign each virtual classroom a unique teacher per section, so a "
        "student never sees the same teacher twice in their classroom list."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Report what would change without saving.",
        )
        parser.add_argument(
            "--academic-year",
            type=str,
            help="Limit to one academic year (code or name).",
        )
        parser.add_argument(
            "--section-id",
            type=int,
            help="Limit to a single section.",
        )
        parser.add_argument(
            "--rebalance",
            action="store_true",
            default=False,
            help=(
                "Re-evaluate every classroom instead of only conflicting ones. "
                "Use this to evict a teacher who is no longer assignable."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        qs = VirtualClassroom.objects.select_related(
            "section", "subject", "teacher", "academic_year"
        )

        year = options.get("academic_year")
        if year:
            qs = qs.filter(academic_year__code__iexact=year) | qs.filter(
                academic_year__name__iexact=year
            )

        section_id = options.get("section_id")
        if section_id:
            qs = qs.filter(section_id=section_id)

        by_section = defaultdict(list)
        for vc in qs.order_by("section_id", "subject__name", "id"):
            by_section[vc.section_id].append(vc)

        User = get_user_model()
        pool = list({u.pk: u for u in assignable_teachers()}.values())

        if not options["rebalance"]:
            # Without --rebalance, a teacher who already holds a classroom stays
            # eligible even if they are not in the "teacher" group, so this run
            # never evicts anyone.
            for holder in User.objects.filter(
                teaching_classrooms__isnull=False
            ).distinct():
                if holder.pk not in {u.pk for u in pool}:
                    pool.append(holder)

        # An account with no employee record falls back to its email in
        # get_full_name(), which students would see as their teacher's name, so
        # it is never auto-assigned.
        nameless = []
        pool_by_id = {}
        for u in pool:
            if not u.is_active:
                continue
            try:
                has_profile = u.employee_profile is not None
            except EmployeeProfile.DoesNotExist:
                has_profile = False
            if not has_profile:
                nameless.append(u.email)
                continue
            pool_by_id[u.pk] = u
        for email in nameless:
            self.stdout.write(
                self.style.WARNING(
                    f"  Skipped {email}: no employee record, would show as an email."
                )
            )

        # Running workload so the spread stays even as we hand classrooms out.
        load = defaultdict(int)
        for vc in qs.values_list("teacher_id", flat=True):
            if vc:
                load[vc] += 1

        moves = []
        skipped = []

        def ranked_free(used):
            return sorted(
                (uid for uid in pool_by_id if uid not in used),
                key=lambda uid: (load[uid], uid),
            )

        for sec_id, rooms in sorted(by_section.items()):
            section = rooms[0].section
            adviser_id = getattr(section, "adviser_id", None)

            # The adviser keeps one classroom: handle theirs first, then the
            # rest fall to whoever has the lightest load.
            rooms.sort(key=lambda v: (v.teacher_id != adviser_id, v.subject.name))

            used = set()
            for vc in rooms:
                current = vc.teacher_id
                if current and current in pool_by_id and current not in used:
                    used.add(current)
                    continue

                free = ranked_free(used)
                if not free:
                    skipped.append(f"{section} / {vc.subject}")
                    continue

                chosen = free[0]
                used.add(chosen)
                if current:
                    load[current] -= 1
                load[chosen] += 1

                old = vc.teacher.get_full_name() if vc.teacher_id else "UNASSIGNED"
                new = pool_by_id[chosen].get_full_name() or pool_by_id[chosen].email
                moves.append((vc, old, new))

                if not dry_run:
                    vc.teacher_id = chosen
                    vc.save(update_fields=["teacher"])

        label = "DRY RUN - not saved" if dry_run else "applied"
        self.stdout.write(f"Sections processed : {len(by_section)}")
        self.stdout.write(f"Classrooms examined: {sum(len(v) for v in by_section.values())}")
        self.stdout.write(f"Reassignments ({label}): {len(moves)}")

        for vc, old, new in moves:
            self.stdout.write(f"  {vc.section} / {vc.subject}: {old} -> {new}")

        for row in skipped:
            self.stdout.write(self.style.WARNING(f"  No teacher available: {row}"))

        if skipped:
            self.stdout.write(
                self.style.ERROR(
                    f"{len(skipped)} classroom(es) could not be assigned - add teachers."
                )
            )
        elif not moves:
            self.stdout.write(self.style.SUCCESS("No conflicts; every classroom has its own teacher."))

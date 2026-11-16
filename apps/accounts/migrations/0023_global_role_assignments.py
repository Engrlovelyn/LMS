from django.db import migrations, models
import django.db.models.deletion


def make_assignments_global(apps, schema_editor):
    RoleAssignment = apps.get_model("accounts", "RoleAssignment")
    AcademicYear = apps.get_model("academic_structure", "AcademicYear")
    database = schema_editor.connection.alias
    active_year = (
        AcademicYear.objects.using(database)
        .filter(status="ACTIVE")
        .order_by("pk")
        .first()
    )
    assignments_by_user = {}
    for assignment in (
        RoleAssignment.objects.using(database)
        .select_related("academic_year")
        .order_by("user_id", "-academic_year__start_date", "-pk")
        .iterator()
    ):
        assignments_by_user.setdefault(assignment.user_id, []).append(assignment)

    keep_ids = []
    for assignments in assignments_by_user.values():
        active_assignments = [
            assignment
            for assignment in assignments
            if active_year and assignment.academic_year_id == active_year.pk
        ]
        if active_assignments:
            selected = active_assignments
        else:
            latest_year_id = assignments[0].academic_year_id
            selected = [
                assignment
                for assignment in assignments
                if assignment.academic_year_id == latest_year_id
            ]
        keep_ids.extend(assignment.pk for assignment in selected)
    RoleAssignment.objects.using(database).exclude(pk__in=keep_ids).delete()
    RoleAssignment.objects.using(database).update(academic_year=None)


def restore_active_year(apps, schema_editor):
    RoleAssignment = apps.get_model("accounts", "RoleAssignment")
    AcademicYear = apps.get_model("academic_structure", "AcademicYear")
    database = schema_editor.connection.alias
    year = (
        AcademicYear.objects.using(database)
        .filter(status="ACTIVE")
        .order_by("pk")
        .first()
        or AcademicYear.objects.using(database).order_by("-start_date", "-pk").first()
    )
    if year is None and RoleAssignment.objects.using(database).exists():
        raise RuntimeError("Cannot restore role assignments without an academic year.")
    if year is not None:
        RoleAssignment.objects.using(database).update(academic_year_id=year.pk)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0022_teachersubject"),
        ("academic_structure", "0005_alter_academicyear_options_alter_section_options_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="roleassignment",
            name="unique_user_role_per_academic_year",
        ),
        migrations.AlterField(
            model_name="roleassignment",
            name="academic_year",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="role_assignments",
                to="academic_structure.academicyear",
            ),
        ),
        migrations.RunPython(make_assignments_global, restore_active_year),
        migrations.AddConstraint(
            model_name="roleassignment",
            constraint=models.UniqueConstraint(
                fields=("user", "role"),
                name="unique_user_role_globally",
            ),
        ),
    ]

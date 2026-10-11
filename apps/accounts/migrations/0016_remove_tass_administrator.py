"""Collapse the duplicate SVI Administrator role into Quality Assurance.

The ``tass_administrator`` role duplicated ``quality_audit``. Any account still
carrying it is moved to ``quality_audit`` (rather than dropped, which would
silently remove access) and the now-empty role plus its Django group are
deleted.
"""

from django.db import migrations

LEGACY_CODE = "tass_administrator"
TARGET_CODE = "quality_audit"


def forwards(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    RoleAssignment = apps.get_model("accounts", "RoleAssignment")
    Group = apps.get_model("auth", "Group")

    legacy = Role.objects.filter(code=LEGACY_CODE).first()
    if legacy is None:
        return

    target = Role.objects.filter(code=TARGET_CODE).first()

    if target is not None:
        # Move every legacy assignment across, dropping the ones that would
        # collide with an existing Quality Assurance assignment for the same
        # user + academic year (unique constraint on those three columns).
        for assignment in RoleAssignment.objects.filter(role=legacy):
            clash = RoleAssignment.objects.filter(
                user_id=assignment.user_id,
                role=target,
                academic_year_id=assignment.academic_year_id,
            ).exists()
            if clash:
                assignment.delete()
            else:
                assignment.role = target
                assignment.save(update_fields=["role"])

    legacy.delete()
    Group.objects.filter(name=LEGACY_CODE).delete()


def backwards(apps, schema_editor):
    """Recreate the role/group; reassigning users back is intentionally manual."""
    Role = apps.get_model("accounts", "Role")
    Group = apps.get_model("auth", "Group")
    Role.objects.get_or_create(code=LEGACY_CODE)
    Group.objects.get_or_create(name=LEGACY_CODE)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0015_alter_role_code"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

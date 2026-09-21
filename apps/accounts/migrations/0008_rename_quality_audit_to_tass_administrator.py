from django.db import migrations


def rename_to_tass_administrator(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Group = apps.get_model("auth", "Group")
    Role.objects.filter(code="quality_audit").update(code="tass_administrator")
    Group.objects.filter(name="quality_audit").update(name="tass_administrator")


def rename_back_to_quality_audit(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Group = apps.get_model("auth", "Group")
    Role.objects.filter(code="tass_administrator").update(code="quality_audit")
    Group.objects.filter(name="tass_administrator").update(name="quality_audit")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_alter_role_code"),
    ]

    operations = [
        migrations.RunPython(
            rename_to_tass_administrator,
            rename_back_to_quality_audit,
        ),
    ]
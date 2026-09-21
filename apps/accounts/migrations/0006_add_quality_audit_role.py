from django.db import migrations


def add_quality_audit_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.get_or_create(
        code="quality_audit",
        defaults={"code": "quality_audit"},
    )


def remove_quality_audit_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(code="quality_audit").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_alter_emailverificationtoken_token_and_more"),
    ]

    operations = [
        migrations.RunPython(add_quality_audit_role, remove_quality_audit_role),
    ]
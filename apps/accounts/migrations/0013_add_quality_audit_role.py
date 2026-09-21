from django.db import migrations, models


def add_quality_audit_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Group = apps.get_model("auth", "Group")
    Role.objects.get_or_create(code="quality_audit", defaults={"code": "quality_audit"})
    Group.objects.get_or_create(name="quality_audit")


def remove_quality_audit_role(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Group = apps.get_model("auth", "Group")
    Role.objects.filter(code="quality_audit").delete()
    Group.objects.filter(name="quality_audit").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_alter_user_options"),
    ]

    operations = [
        migrations.AlterField(
            model_name="role",
            name="code",
            field=models.CharField(
                choices=[
                    ("system_admin", "System Administrator"),
                    ("registrar", "Registrar"),
                    ("teacher", "Teacher"),
                    ("principal", "Principal"),
                    ("assistant_principal", "Assistant Principal"),
                    ("student", "Student"),
                    ("tass_administrator", "TASS Administrator"),
                    ("quality_audit", "Quality Assurance"),
                ],
                max_length=20,
                unique=True,
            ),
        ),
        migrations.RunPython(add_quality_audit_role, remove_quality_audit_role),
    ]
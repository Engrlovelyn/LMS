"""Add the Elementary Principal role.

The school runs an Elementary campus alongside the existing Junior/High School
campus. Rather than renaming the existing ``principal`` code -- which would
break every stored assignment, Django group and saved login redirect -- the
existing role is kept and only its display label changes to "High School
Principal", and a separate ``elementary_principal`` role is added.

Both the ``Role`` row and its Django ``Group`` are created here so a fresh
database gets them from ``migrate`` alone. The group is left empty on purpose:
``assign_role_permissions`` seeds it from the role matrix and is careful never
to overwrite grants an administrator has since edited.
"""

from django.db import migrations, models

NEW_CODE = "elementary_principal"


def forwards(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Group = apps.get_model("auth", "Group")
    Role.objects.get_or_create(code=NEW_CODE)
    Group.objects.get_or_create(name=NEW_CODE)


def backwards(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Group = apps.get_model("auth", "Group")
    # Assignments are PROTECTed, so drop them first or the role cannot be
    # deleted. Only rows for this role are touched.
    apps.get_model("accounts", "RoleAssignment").objects.filter(
        role__code=NEW_CODE
    ).delete()
    Role.objects.filter(code=NEW_CODE).delete()
    Group.objects.filter(name=NEW_CODE).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0018_alter_user_options"),
        ("auth", "0012_alter_user_first_name_max_length"),
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
                    ("principal", "High School Principal"),
                    ("elementary_principal", "Elementary Principal"),
                    ("assistant_principal", "Assistant Principal"),
                    ("student", "Student"),
                    ("quality_audit", "Quality Assurance"),
                ],
                max_length=20,
                unique=True,
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
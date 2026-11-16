from django.db import migrations


def grant_sf_template_permission(apps, schema_editor):
    database = schema_editor.connection.alias
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")

    content_type, _ = ContentType.objects.using(database).get_or_create(
        app_label="analytics",
        model="generatedreport",
    )
    permission, _ = Permission.objects.using(database).get_or_create(
        content_type=content_type,
        codename="manage_sf_report_templates",
        defaults={"name": "Manage SF Report Templates"},
    )
    registrar = Group.objects.using(database).filter(name="registrar").first()
    if registrar:
        registrar.permissions.add(permission)


def revoke_sf_template_permission(apps, schema_editor):
    database = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    permission = Permission.objects.using(database).filter(
        content_type__app_label="analytics",
        content_type__model="generatedreport",
        codename="manage_sf_report_templates",
    ).first()
    registrar = Group.objects.using(database).filter(name="registrar").first()
    if registrar and permission:
        registrar.permissions.remove(permission)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0023_global_role_assignments"),
        ("analytics", "0003_sfreporttemplate"),
    ]

    operations = [
        migrations.RunPython(
            grant_sf_template_permission,
            revoke_sf_template_permission,
        ),
    ]

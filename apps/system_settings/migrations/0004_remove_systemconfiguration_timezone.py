from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("system_settings", "0003_systemconfiguration_backup_settings"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="systemconfiguration",
            name="timezone",
        ),
    ]

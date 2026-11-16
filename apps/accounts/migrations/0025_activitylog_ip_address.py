from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0024_registrar_sf_template_permission"),
    ]

    operations = [
        migrations.AddField(
            model_name="activitylog",
            name="ip_address",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
    ]

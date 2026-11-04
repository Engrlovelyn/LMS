from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0009_notification_message_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="classannouncement",
            name="title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]

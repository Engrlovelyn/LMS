from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0008_schoolevent_audience"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="notification_type",
            field=models.CharField(
                choices=[
                    ("announcement", "Class Announcement"),
                    ("material", "Learning Material"),
                    ("assessment", "Assessment"),
                    ("attendance", "Attendance"),
                    ("message", "Message"),
                ],
                max_length=20,
            ),
        ),
    ]

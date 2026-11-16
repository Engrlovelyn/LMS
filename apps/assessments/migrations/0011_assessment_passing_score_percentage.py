from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assessments", "0010_assessment_time_limit_minutes"),
    ]

    operations = [
        migrations.AddField(
            model_name="assessment",
            name="passing_score_percentage",
            field=models.PositiveSmallIntegerField(default=60),
        ),
    ]

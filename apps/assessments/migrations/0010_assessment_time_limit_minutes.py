from django.db import migrations, models


def set_existing_time_limits(apps, schema_editor):
    Assessment = apps.get_model("assessments", "Assessment")
    Assessment.objects.filter(assessment_method="examination").update(
        time_limit_minutes=90
    )


class Migration(migrations.Migration):

    dependencies = [
        ("assessments", "0009_assessmentattempt"),
    ]

    operations = [
        migrations.AddField(
            model_name="assessment",
            name="time_limit_minutes",
            field=models.PositiveSmallIntegerField(default=60),
        ),
        migrations.RunPython(set_existing_time_limits, migrations.RunPython.noop),
    ]

import django.utils.timezone
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0021_alter_user_options"),
        ("assessments", "0008_assessmentproctoringevent"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssessmentAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("started_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("answers", models.JSONField(blank=True, default=dict)),
                ("revision", models.PositiveIntegerField(default=0)),
                ("last_saved_at", models.DateTimeField(blank=True, null=True)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("assignment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attempts", to="assessments.assessmentassignment")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assessment_attempts", to="accounts.studentprofile")),
            ],
        ),
        migrations.AddConstraint(
            model_name="assessmentattempt",
            constraint=models.UniqueConstraint(fields=("assignment", "student"), name="unique_assessment_attempt_per_student"),
        ),
    ]

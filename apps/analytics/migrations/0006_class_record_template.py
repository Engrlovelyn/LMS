from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("analytics", "0005_alter_sfreporttemplate_form_type")]

    operations = [
        migrations.AlterField(
            model_name="sfreporttemplate",
            name="field_mappings",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Maps template cells and learner/assessment columns to system data.",
            ),
        ),
        migrations.AlterField(
            model_name="sfreporttemplate",
            name="form_type",
            field=models.CharField(
                choices=[
                    ("sf1", "School Form 1 (SF1) - Learner Roster"),
                    ("sf2", "School Form 2 (SF2) - Daily Attendance"),
                    ("sf3", "School Form 3 (SF3) - Summary of Attendance"),
                    ("sf4", "School Form 4 (SF4) - Monthly Report"),
                    ("sf5", "School Form 5 (SF5) - Report on Promotion"),
                    ("sf9", "School Form 9 (SF9) - Learner Progress Report Card"),
                    ("sf10", "School Form 10 (SF10) - Learner's Permanent Academic Record"),
                    ("class_record", "DepEd Class Record - Assessment Completion"),
                ],
                max_length=20,
            ),
        ),
    ]

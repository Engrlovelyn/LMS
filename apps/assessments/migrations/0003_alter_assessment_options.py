# Hand-written — adds the RBAC assessment permission codenames from the
# manuscript User Role Matrix (written by hand because `makemigrations` is
# blocked by a pre-existing schema drift on assessments_assessment.assessment_method).
# Permission-codename changes are reflected in Meta options; nothing column-level.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('assessments', '0002_alter_assessment_options_and_more'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='assessment',
            options={'permissions': [('create_assessment_custom', 'Create Assessment'), ('access_assessment_repository', 'Access Assessment Repository'), ('view_assessment_custom', 'View Assessment'), ('update_assessment_custom', 'Update Assessment'), ('complete_assessment_custom', 'Complete Assessment'), ('monitor_student_submissions', 'Monitor Student Submissions'), ('manually_grade_subjective_assessment', 'Manually Grade Subjective Assessment'), ('archive_assessment_custom', 'Archive Assessment'), ('trigger_missed_assessment_notification', 'Trigger Missed Assessment Notification'), ('auto_grade_objective_assessment', 'Auto-Grade Objective Assessment'), ('compute_student_grade', 'Compute Student Grade')]},
        ),
    ]
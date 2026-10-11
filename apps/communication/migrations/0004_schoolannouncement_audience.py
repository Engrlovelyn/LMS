"""Scope a school-wide announcement to one campus or to the whole school.

``SchoolAnnouncement`` had no audience at all, so every announcement posted by
a principal was readable by every other role and by every dashboard. That made
a campus split impossible: the Elementary Principal and the High School
Principal both posted into the same single feed.

``audience`` is the explicit read filter:

``whole_school`` (the default)
    Everyone sees it -- the Principal's Office, the System Administrator, and
    both campuses. Existing rows keep this value, so nothing that was already
    published disappears.
``elementary``
    Only users whose campus scopes include Elementary.
``high_school``
    Only users whose campus scopes include High School.

Posted by is not consulted for the read filter: the audience is set at post
time from the author's own campus scope (the API refuses to author an audience
the author cannot post to), so a later change of role does not retroactively
hide or expose a notice that was correctly published at the time.

``audience_at_posted_at`` records the author's resolved scope alongside the
author, so an audit reader can tell an announcement published school-wide from
one published campus-wide without inferring it from the author's current role.
"""

from django.db import migrations, models


def forwards(apps, schema_editor):
    SchoolAnnouncement = apps.get_model("communication", "SchoolAnnouncement")
    SchoolAnnouncement.objects.filter(audience="").update(audience="whole_school")


def backwards(apps, schema_editor):
    # The prior schema had no audience column at all, so collapsing every value
    # back to school-wide is the closest faithful inverse. Rows written for one
    # campus stay visible to all, which is exactly the behaviour that existed
    # before this column did.
    SchoolAnnouncement = apps.get_model("communication", "SchoolAnnouncement")
    SchoolAnnouncement.objects.exclude(audience="whole_school").update(
        audience="whole_school"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("communication", "0003_schoolevent"),
    ]

    operations = [
        migrations.AddField(
            model_name="schoolannouncement",
            name="audience",
            field=models.CharField(
                blank=True,
                choices=[
                    ("whole_school", "Whole School"),
                    ("elementary", "Elementary"),
                    ("high_school", "High School"),
                ],
                default="whole_school",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="schoolannouncement",
            name="audience_at_posted_at",
            field=models.CharField(
                blank=True,
                choices=[
                    ("whole_school", "Whole School"),
                    ("elementary", "Elementary"),
                    ("high_school", "High School"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
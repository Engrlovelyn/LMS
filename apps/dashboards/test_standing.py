import datetime
from django.contrib.auth.models import Group
from django.urls import reverse
from django.test import TestCase
from apps.accounts.models import StudentProfile, User
from apps.academic_structure.models import (
    AcademicYear, AcademicStructureVersion, Section, Subject, StudentEnrollment,
)
from apps.assessments.models import StudentGrade
from apps.dashboards.context import academic_standing_rows, _standing_label


class AcademicStandingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2027, 5, 31),
            status="ACTIVE",
        )
        cls.version = AcademicStructureVersion.objects.create(
            academic_year=cls.year, grading_period_type="QUARTER"
        )
        cls.section = Section.objects.create(
            academic_year=cls.year, name="Rose", grade_level=7
        )
        cls.subject = Subject.objects.create(
            academic_year=cls.year, name="Mathematics", grade_level=7
        )

    def _student(self, email, first, last, scores):
        user = User.objects.create_user(email=email, password="pass12345x")
        user.status = "active"
        user.save()
        prof = StudentProfile.objects.create(
            user=user, lrn="LRN-%s" % email.split("@")[0], first_name=first,
            last_name=last, sex="M", birthdate=datetime.date(2013, 5, 4),
        )
        StudentEnrollment.objects.create(
            student=prof, academic_year=self.year, section=self.section
        )
        for i, score in enumerate(scores):
            StudentGrade.objects.create(
                student=prof, subject=self.subject, academic_year=self.year,
                structure_version=self.version, grading_period="Q%d" % (i + 1),
                grade_value=score,
            )
        return prof

    def test_standing_bands(self):
        self.assertEqual(_standing_label(96), "Outstanding")
        self.assertEqual(_standing_label(90), "Outstanding")
        self.assertEqual(_standing_label(87), "Very Good")
        self.assertEqual(_standing_label(82), "Good")
        self.assertEqual(_standing_label(78), "Fair")
        self.assertEqual(_standing_label(65), "Needs Improvement")
        self.assertEqual(_standing_label(40), "Failed")

    def test_rows_average_and_at_risk(self):
        self._student("top@t.test", "Ana", "Top", [95, 100, 92])
        self._student("risk@t.test", "Ben", "Risk", [70, 74, 68])
        rows = academic_standing_rows(self.year, {})
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["Ana Top"]["average"], 95.67)
        self.assertEqual(by_name["Ana Top"]["standing"], "Outstanding")
        self.assertFalse(by_name["Ana Top"]["at_risk"])
        self.assertTrue(by_name["Ben Risk"]["at_risk"])
        self.assertEqual(by_name["Ben Risk"]["standing"], "Needs Improvement")

    def test_ungraded_learner_is_not_zeroed(self):
        prof = self._student("none@t.test", "Cara", "None", [88])
        prof2 = User.objects.create_user(email="solo@t.test", password="pass12345x")
        prof2.status = "active"
        prof2.save()
        solo = StudentProfile.objects.create(
            user=prof2, lrn="LRN-solo", first_name="Dan", last_name="Solo", sex="F",
            birthdate=datetime.date(2013, 1, 9),
        )
        StudentEnrollment.objects.create(
            student=solo, academic_year=self.year, section=self.section
        )
        rows = {r["name"]: r for r in academic_standing_rows(self.year, {})}
        self.assertNotIn("Dan Solo", rows)
        self.assertEqual(rows["Cara None"]["average"], 88)
        self.assertEqual(rows["Cara None"]["subjects"], 1)

    def test_rows_carry_section_when_lookup_supplied(self):
        self._student("sec@t.test", "Eve", "Sec", [91])
        prof = StudentProfile.objects.get(user__email="sec@t.test")
        rows = academic_standing_rows(
            self.year,
            {prof.id: {"grade": 7, "section": "Rose"}},
        )
        self.assertEqual(rows[0]["grade"], 7)
        self.assertEqual(rows[0]["section"], "Rose")

    def test_both_dashboards_expose_standing(self):
        self._student("d1@t.test", "Frank", "Solo", [65])
        for email, role, url_name in [
            ("principal@t.test", "principal", "dashboards:principal"),
            ("qa@t.test", "quality_audit", "dashboards:quality_audit"),
        ]:
            with self.subTest(role=role):
                u = User.objects.create_user(email=email, password="pass12345x")
                u.status = "active"
                u.save()
                u.groups.add(Group.objects.get_or_create(name=role)[0])
                self.client.login(username=email, password="pass12345x")
                resp = self.client.get(reverse(url_name))
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "Academic Standing per Grade")
                self.assertContains(resp, "At-Risk")
                self.client.logout()

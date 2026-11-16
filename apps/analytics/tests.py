import datetime
import io
import os
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from django.contrib.auth.models import Group
from django.core.management import call_command
from apps.accounts.models import ActivityLog, StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.assessments.models import Assessment, AssessmentAssignment, Submission
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .models import GeneratedReport, SFReportTemplate


_TEST_MEDIA_ROOT = os.path.join(tempfile.gettempdir(), "tass-analytics-%s" % os.getpid())


@override_settings(
    MEDIA_ROOT=_TEST_MEDIA_ROOT,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class SubmissionReportGenerationTests(TestCase):
    """Generating a Submission & Assessment Report persists a downloadable CSV."""

    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2026 PA",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            status="ACTIVE",
        )
        cls.section = Section.objects.create(
            academic_year=cls.year, name="PA - A", grade_level=9
        )
        cls.subject = Subject.objects.create(
            academic_year=cls.year, name="English", grade_level=9
        )
        cls.admin = User.objects.create_superuser(
            email="rep_admin@tass.local", password="pw"
        )
        cls.teacher = User.objects.create_user(
            email="rep_teacher@tass.local", password="pw"
        )
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.section,
            subject=cls.subject,
            teacher=cls.teacher,
        )
        student_user = User.objects.create_user(
            email="rep_student@tass.local", password="pw"
        )
        cls.student = StudentProfile.objects.create(
            user=student_user,
            lrn="LRN-2026-7001",
            first_name="Mara",
            last_name="Reyes",
            birthdate=datetime.date(2009, 3, 3),
            sex="F",
        )
        VirtualClassroomEnrollment.objects.create(
            virtual_classroom=cls.vc, student=cls.student
        )
        cls.assessment = Assessment.objects.create(
            subject=cls.subject,
            academic_year=cls.year,
            created_by=cls.teacher,
            title="Reading Test",
            assessment_method=Assessment.METHOD_EXAMINATION,
            assessment_type=Assessment.TYPE_SUMMATIVE_TEST,
            status=Assessment.STATUS_ACTIVE,
            submission_deadline=timezone.now() + datetime.timedelta(days=3),
        )
        cls.assignment = AssessmentAssignment.objects.create(
            assessment=cls.assessment, virtual_classroom=cls.vc, assigned_by=cls.teacher
        )
        cls.submission = Submission.objects.create(
            assessment_assignment=cls.assignment, student=cls.student
        )

    def setUp(self):
        self.client.force_login(self.admin)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def test_reports_page_renders(self):
        res = self.client.get(reverse("analytics:submission_reports"))
        self.assertEqual(res.status_code, 200)

    def test_activity_log_displays_login_network_origin(self):
        ActivityLog.objects.create(
            user=self.teacher,
            action="user_login",
            related_entity_type="LoginSession",
            related_entity_id=1,
            ip_address="198.51.100.27",
        )
        response = self.client.get(reverse("analytics:activity_logs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "198.51.100.27")
        self.assertContains(response, "Login IP / Network Origin")
        self.assertContains(response, "precise physical location")

    def test_generate_creates_persisted_downloadable_report(self):
        res = self.client.post(reverse("analytics:submission_report_generate"))
        self.assertEqual(res.status_code, 302)
        report = GeneratedReport.objects.get(
            report_type=GeneratedReport.TYPE_SUBMISSION_REPORT
        )
        self.assertEqual(report.generated_by, self.admin)
        self.assertEqual(report.academic_year, self.year)
        self.assertTrue(report.file.name)
        content = report.file.read().decode("utf-8")
        self.assertIn("Reading Test", content)
        self.assertIn("Mara", content)

    def test_report_download_serves_the_csv(self):
        self.client.post(reverse("analytics:submission_report_generate"))
        report = GeneratedReport.objects.get(
            report_type=GeneratedReport.TYPE_SUBMISSION_REPORT
        )
        res = self.client.get(
            reverse("analytics:report_download", args=[report.pk])
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(".csv", res["Content-Disposition"])
        body = b"".join(res.streaming_content).decode("utf-8")
        self.assertIn("Reading Test", body)

    def _upload_sf2_template(self, name="Official SF2"):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["LRN", "Learner's Name", "Sex", "1", "2", "3", "Present", "Absent"])
        sheet.append(["", "", "", "", "", "", "", ""])
        content = io.BytesIO()
        workbook.save(content)
        workbook.close()
        return SFReportTemplate.objects.create(
            name=name,
            form_type=SFReportTemplate.FORM_SF2,
            template_file=ContentFile(content.getvalue(), name="official_sf2.xlsx"),
            field_mappings={
                "header_row": 1,
                "first_data_row": 2,
                "row_limit": 2,
            },
            uploaded_by=self.admin,
        )

    def _upload_merged_official_sf2_template(self, learner_rows=3):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "School Form 2 (SF2)"
        sheet.merge_cells("A10:C13")
        sheet["A10"] = "LEARNER'S NAME (Last Name, First Name, Middle Name)"
        sheet.merge_cells("D10:AB10")
        sheet["D10"] = "1st row for date, 2nd row for Day: M,T,W,TH,F"
        sheet.merge_cells("AC10:AD11")
        sheet["AC10"] = "Total for the Month"
        sheet["AC12"] = "ABSENT"
        sheet["AD12"] = "TARDY"
        for row in range(14, 14 + learner_rows):
            sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        content = io.BytesIO()
        workbook.save(content)
        workbook.close()
        return SFReportTemplate.objects.create(
            name="Merged Official SF2",
            form_type=SFReportTemplate.FORM_SF2,
            template_file=ContentFile(content.getvalue(), name="merged_official_sf2.xlsx"),
            field_mappings={},
            uploaded_by=self.admin,
        )

    def test_sf2_uses_the_active_excel_template_and_monthly_attendance(self):
        template = self._upload_sf2_template()
        session = AttendanceSession.objects.create(
            virtual_classroom=self.vc,
            session_date=datetime.date(2026, 3, 3),
            created_by=self.teacher,
        )
        AttendanceRecord.objects.create(
            session=session,
            student=self.student,
            status=AttendanceRecord.PRESENT_LATE,
        )

        response = self.client.post(
            reverse("analytics:sf_report_home"),
            {
                "generate": "1",
                "report_type": GeneratedReport.TYPE_SF2,
                "academic_year": self.year.pk,
                "report_month": "2026-03",
            },
        )

        self.assertEqual(response.status_code, 302)
        report = GeneratedReport.objects.get(report_type=GeneratedReport.TYPE_SF2)
        self.assertEqual(report.parameters["month"], "2026-03")
        self.assertEqual(report.parameters["template_id"], template.pk)
        self.assertTrue(report.file.name.endswith(".xlsx"))
        with report.file.open("rb") as generated_file:
            output = load_workbook(io.BytesIO(generated_file.read()), data_only=True)
        sheet = output.active
        self.assertEqual(sheet["A2"].value, self.student.lrn)
        self.assertEqual(sheet["B2"].value, "Reyes, Mara")
        self.assertEqual(sheet["F2"].value, "L")
        self.assertEqual(sheet["G2"].value, 1)
        self.assertEqual(sheet["H2"].value, 0)
        output.close()

    def test_sf2_automatically_fills_official_merged_template(self):
        self._upload_merged_official_sf2_template()
        session = AttendanceSession.objects.create(
            virtual_classroom=self.vc,
            session_date=datetime.date(2026, 3, 3),
            created_by=self.teacher,
        )
        AttendanceRecord.objects.create(
            session=session,
            student=self.student,
            status=AttendanceRecord.PRESENT_LATE,
        )

        response = self.client.post(
            reverse("analytics:sf_report_home"),
            {
                "generate": "1",
                "report_type": GeneratedReport.TYPE_SF2,
                "academic_year": self.year.pk,
                "report_month": "2026-03",
            },
        )

        self.assertEqual(response.status_code, 302)
        report = GeneratedReport.objects.get(report_type=GeneratedReport.TYPE_SF2)
        with report.file.open("rb") as generated_file:
            output = load_workbook(io.BytesIO(generated_file.read()), data_only=True)
        sheet = output.active
        self.assertEqual(sheet["A14"].value, 1)
        self.assertEqual(sheet["B14"].value, "Reyes, Mara")
        self.assertEqual(sheet["D11"].value, 2)
        self.assertEqual(sheet["E11"].value, 3)
        self.assertEqual(sheet["D12"].value, "M")
        self.assertEqual(sheet["E12"].value, "T")
        self.assertEqual(sheet["E14"].value, "L")
        self.assertEqual(sheet["AD14"].value, 1)
        output.close()

    def test_sf2_splits_large_sections_across_copied_template_sheets(self):
        self._upload_merged_official_sf2_template(learner_rows=21)
        for index in range(44):
            student_user = User.objects.create_user(
                email=f"sf2_student_{index}@tass.local",
                password="pw",
            )
            student = StudentProfile.objects.create(
                user=student_user,
                lrn=f"LRN-2026-{8000 + index}",
                first_name=f"Student{index}",
                last_name="Added",
                birthdate=datetime.date(2009, 1, 1),
                sex="F",
            )
            VirtualClassroomEnrollment.objects.create(
                virtual_classroom=self.vc,
                student=student,
            )

        response = self.client.post(
            reverse("analytics:sf_report_home"),
            {
                "generate": "1",
                "report_type": GeneratedReport.TYPE_SF2,
                "academic_year": self.year.pk,
                "report_month": "2026-03",
            },
        )

        self.assertEqual(response.status_code, 302)
        report = GeneratedReport.objects.get(report_type=GeneratedReport.TYPE_SF2)
        with report.file.open("rb") as generated_file:
            output = load_workbook(io.BytesIO(generated_file.read()), data_only=True)
        self.assertEqual(len(output.worksheets), 3)
        self.assertTrue(output.worksheets[0].title.endswith("(1)"))
        self.assertTrue(output.worksheets[1].title.endswith("(2)"))
        self.assertTrue(output.worksheets[2].title.endswith("(3)"))
        listed_students = [
            sheet.cell(row=row, column=1).value
            for sheet in output.worksheets
            for row in range(14, 35)
            if sheet.cell(row=row, column=1).value is not None
        ]
        self.assertEqual(len(listed_students), 45)
        self.assertTrue(all(
            "B14:C14" in {str(area) for area in sheet.merged_cells.ranges}
            for sheet in output.worksheets
        ))
        output.close()

    def test_sf2_generation_requires_an_uploaded_excel_template(self):
        response = self.client.post(
            reverse("analytics:sf_report_home"),
            {
                "generate": "1",
                "report_type": GeneratedReport.TYPE_SF2,
                "academic_year": self.year.pk,
                "report_month": "2026-03",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload an active Excel SF2 template")
        self.assertFalse(GeneratedReport.objects.filter(report_type=GeneratedReport.TYPE_SF2).exists())

    def test_new_sf2_template_replaces_previous_active_template(self):
        old_template = self._upload_sf2_template("Old SF2")
        workbook = Workbook()
        workbook.active.append(["Name", "1"])
        content = io.BytesIO()
        workbook.save(content)
        workbook.close()
        response = self.client.post(
            reverse("analytics:sf_template_upload"),
            {
                "name": "New SF2",
                "form_type": "sf2",
                "field_mappings": '{"header_row":1,"first_data_row":2,"row_limit":2}',
                "template_file": ContentFile(
                    content.getvalue(), name="new_sf2.xlsx"
                ),
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(SFReportTemplate.objects.get(pk=old_template.pk).is_active)
        self.assertTrue(
            SFReportTemplate.objects.get(pk=response.json()["id"]).is_active
        )

    def test_invalid_sf2_workbook_does_not_replace_active_template(self):
        old_template = self._upload_sf2_template("Existing SF2")
        response = self.client.post(
            reverse("analytics:sf_template_upload"),
            {
                "name": "Broken SF2",
                "form_type": "sf2",
                "field_mappings": "{}",
                "template_file": ContentFile(
                    b"not an Excel workbook", name="broken.xlsx"
                ),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(SFReportTemplate.objects.get(pk=old_template.pk).is_active)

    def test_teacher_download_generates_the_same_uploaded_template(self):
        self._upload_sf2_template()
        self.teacher.status = User.Status.ACTIVE
        self.teacher.save()
        self.teacher.groups.add(Group.objects.get_or_create(name="teacher")[0])
        self.client.force_login(self.teacher)

        response = self.client.get(
            reverse("analytics:sf2_generate"),
            {"classroom_id": self.vc.pk, "month": "2026-03"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(".xlsx", response["Content-Disposition"])
        self.assertTrue(
            GeneratedReport.objects.filter(
                report_type=GeneratedReport.TYPE_SF2,
                generated_by=self.teacher,
            ).exists()
        )

    def test_teacher_can_save_filled_sf2_to_records_without_downloading(self):
        template = self._upload_sf2_template()
        self.teacher.status = User.Status.ACTIVE
        self.teacher.save()
        self.teacher.groups.add(Group.objects.get_or_create(name="teacher")[0])
        self.client.force_login(self.teacher)

        response = self.client.get(
            reverse("analytics:sf2_generate"),
            {
                "classroom_id": self.vc.pk,
                "month": "2026-03",
                "save_only": "1",
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload["ok"])
        report = GeneratedReport.objects.get(pk=payload["report_id"])
        self.assertEqual(report.generated_by, self.teacher)
        self.assertEqual(report.parameters["template_id"], template.pk)
        self.assertEqual(report.parameters["month"], "2026-03")

    def test_registrar_can_manage_sf2_templates(self):
        call_command("assign_role_permissions", verbosity=0)
        registrar = User.objects.create_user(
            email="sf2-registrar@tass.local",
            password="FixturePass123!",
            status=User.Status.ACTIVE,
        )
        registrar.groups.add(Group.objects.get_or_create(name="registrar")[0])

        self.assertTrue(registrar.has_perm("analytics.manage_sf_report_templates"))

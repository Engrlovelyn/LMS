from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, Permission


# Built directly from the official Table 12.1 User Role Matrix.
# Principal is given the same view-rights as Assistant Principal wherever
# the table showed Principal=X but Assistant Principal=checked on a
# view/self-service function (confirmed data-entry gap vs. Business Rules).
# Rows where Principal AND Assistant Principal are both X are left as-is
# (that pattern indicates an intentional restriction, not a gap).
ROLE_PERMISSION_MATRIX = {
    # ACCOUNT AND USER MANAGEMENT
    "create_employee_account": ["system_admin"],
    "view_employee_account": ["system_admin"],
    "generate_student_account": ["registrar"],
    "verify_email_address": ["system_admin", "registrar", "assistant_principal", "teacher", "student", "principal"],
    "password_setup": ["system_admin", "registrar", "assistant_principal", "teacher", "student", "principal"],
    "create_student_profile_account": ["registrar"],
    "view_student_record": ["registrar", "assistant_principal", "teacher", "principal"],
    "update_student_record": ["registrar"],
    "archive_student_record": ["registrar"],
    "bulk_import_student_records": ["registrar"],
    "bulk_import_students_to_vc": ["registrar"],
    "update_employee_account": ["system_admin"],
    "archive_account": ["system_admin"],
    "reset_account_password": ["system_admin"],
    "reset_forgotten_password": ["system_admin", "registrar", "assistant_principal", "teacher", "student", "principal"],
    "log_in_user": ["system_admin", "registrar", "assistant_principal", "teacher", "student", "principal"],
    "manage_active_sessions": ["system_admin", "registrar", "assistant_principal", "teacher", "student", "principal"],
    "view_role_based_dashboard": ["system_admin", "registrar", "assistant_principal", "teacher", "student", "principal"],

    # SYSTEM SETTINGS
    "configure_system_settings": ["system_admin"],
    "management_password_policy": ["system_admin"],
    "backup_restore_database": ["system_admin"],

    # ACADEMIC STRUCTURE MANAGEMENT
    "add_academic_year_custom": ["registrar", "assistant_principal"],
    "update_academic_year": ["registrar"],
    "archive_academic_year": ["registrar"],
    "manage_academic_structure_template": ["system_admin", "registrar", "assistant_principal"],
    "rollover_academic_year": ["system_admin", "registrar"],
    "promote_student": ["system_admin", "registrar"],
    "validate_section_subject_coverage": ["registrar"],
    "add_section_custom": ["system_admin", "registrar", "assistant_principal"],
    "update_section_custom": ["system_admin", "registrar", "assistant_principal"],
    "archive_section_custom": ["system_admin", "registrar", "assistant_principal"],
    "add_subject_custom": ["system_admin", "registrar", "assistant_principal"],
    "update_subject_custom": ["system_admin", "registrar", "assistant_principal"],
    "archive_subject_custom": ["system_admin"],

    # VIRTUAL CLASSROOM MANAGEMENT
    "create_virtual_classroom": ["registrar", "assistant_principal"],
    "view_virtual_classroom": ["registrar", "assistant_principal", "teacher", "student", "principal"],
    "add_student_to_class": ["registrar", "assistant_principal"],
    "archive_student_from_class": ["registrar"],
    "enroll_students_to_vc": ["registrar"],
    "update_virtual_classroom": ["registrar", "assistant_principal"],
    "archive_virtual_classroom": ["registrar", "assistant_principal"],

    # TEACHING AND LEARNING MANAGEMENT
    "add_topic_custom": ["teacher"],
    "upload_learning_resource": ["teacher"],
    "access_subject_level_repository": ["teacher"],
    "manage_learning_resources": ["teacher"],
    "view_download_learning_resource": ["assistant_principal", "student", "principal"],

    # ASSESSMENT MANAGEMENT
    "create_assessment_custom": ["teacher"],
    "access_assessment_repository": ["teacher"],
    "view_assessment_custom": ["assistant_principal", "teacher", "student", "principal"],
    "update_assessment_custom": ["teacher"],
    "complete_assessment_custom": ["student"],
    "monitor_student_submissions": ["teacher"],
    "manually_grade_subjective_assessment": ["teacher"],
    "archive_assessment_custom": ["teacher"],
    # Trigger Missed Assessment Notification, Auto-Grade Objective Assessment,
    # Compute Student Grade are all-X (system-only) in the source table -
    # intentionally not assigned to any group.

    # ATTENDANCE MONITORING
    "record_class_attendance": ["teacher"],
    "view_attendance_records": ["assistant_principal", "teacher", "student", "principal"],
    "update_attendance_record_custom": ["teacher"],
    "generate_attendance_report": ["system_admin", "registrar", "teacher"],  # asst_principal is also X here - intentional, not a gap

    # COMMUNICATION
    "post_class_announcement": ["teacher"],
    "post_school_wide_announcement": ["principal"],
    "view_announcements_custom": ["assistant_principal", "teacher", "student", "principal"],
    "update_class_announcement": ["teacher"],
    "archive_class_announcement": ["teacher"],
    "update_school_wide_announcement": ["principal"],
    "archive_school_wide_announcement": ["principal"],
    "send_group_messaging": ["teacher", "student"],
    "send_private_message": ["teacher", "student"],

    # REPORTS & ANALYTICS
    "view_school_wide_analytics": ["system_admin", "registrar", "assistant_principal", "principal"],
    "view_class_level_analytics": ["assistant_principal", "teacher", "principal"],
    "generate_attendance_trend_analytics": ["system_admin", "registrar"],  # asst_principal also X - intentional
    "generate_submission_assessment_reports": ["system_admin", "teacher"],  # asst_principal also X - intentional
    "manage_sf_report_templates": ["system_admin"],
    "generate_sf_reports": ["system_admin", "registrar"],  # asst_principal also X - intentional
    "monitor_student_activity_logs": ["system_admin"],

    # SYSTEM GOVERNANCE
    "conduct_annual_role_access_review": ["system_admin"],
}


class Command(BaseCommand):
    help = "Assign Table 12.1 permissions to their matching Groups (corrected, exact source table)"

    def handle(self, *args, **kwargs):
        missing = []
        assigned_count = 0

        # Clear existing group permissions first, so removed roles
        # (e.g. a role that used to have a permission by mistake) are
        # actually cleaned up, not just added-to.
        for group in Group.objects.all():
            group.permissions.clear()

        for codename, role_codes in ROLE_PERMISSION_MATRIX.items():
            try:
                permission = Permission.objects.get(codename=codename)
            except Permission.DoesNotExist:
                missing.append(codename)
                continue
            except Permission.MultipleObjectsReturned:
                permission = Permission.objects.filter(codename=codename).first()

            for role_code in role_codes:
                group, _ = Group.objects.get_or_create(name=role_code)
                group.permissions.add(permission)
                assigned_count += 1

        self.stdout.write(self.style.SUCCESS(f"Assigned {assigned_count} group-permission links."))
        if missing:
            self.stdout.write(self.style.WARNING(
                f"{len(missing)} permission codename(s) not found: {missing}"
            ))

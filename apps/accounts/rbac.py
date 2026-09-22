"""User Role Matrix (FINAL_SVI_MANUSCRIPT-2_0_with_TASS_Administrator.docx, Table 84).

Single source of truth for the role -> Django-permission grants. Each entry is an
"app_label.codename" string matching a Meta.permissions codename in the codebase.

Notes:
- `principal` is prose/frontend-aligned (the matrix marks Principal as X for
  log-in / dashboard / analytics / assessments / attendance, which contradicts
  the manuscript narrative and the Principal frontend). The Principal is granted
  monitoring/view permissions instead.
- `tass_administrator` = TASS Administrator; view-only monitoring per the matrix.
- `log_in_user` and `view_role_based_dashboard` are granted to every role so all
  authenticated users can reach their role dashboard.
"""

from django.contrib.auth.models import Group, Permission

ROLE_PERMISSIONS = {
    "system_admin": {
        "accounts.create_employee_account",
        "accounts.view_employee_account",
        "accounts.verify_email_address",
        "accounts.password_setup",
        "accounts.update_employee_account",
        "accounts.archive_account",
        "accounts.reset_account_password",
        "accounts.reset_forgotten_password",
        "accounts.log_in_user",
        "accounts.manage_active_sessions",
        "accounts.view_role_based_dashboard",
        "accounts.role_based_access_control",
        "academic_structure.view_academic_year",
        "academic_structure.manage_academic_structure_template",
        "academic_structure.rollover_academic_year",
        "academic_structure.promote_student",
        "academic_structure.view_section_custom",
        "academic_structure.add_section_custom",
        "academic_structure.update_section_custom",
        "academic_structure.archive_section_custom",
        "academic_structure.view_subject_custom",
        "academic_structure.add_subject_custom",
        "academic_structure.update_subject_custom",
        "academic_structure.archive_subject_custom",
        "assessments.view_grade_book",
        "assessments.view_student_performance_tracking",
        "assessments.auto_grade_objective_assessment",
        "assessments.compute_student_grade",
        "governance.retrieve_role_access_review_records",
        "attendance.generate_attendance_report",
        "analytics.view_school_wide_analytics",
        "analytics.generate_attendance_trend_analytics",
        "analytics.generate_submission_assessment_reports",
        "analytics.manage_sf_report_templates",
        "analytics.generate_sf_reports",
        "analytics.monitor_student_activity_logs",
        "governance.conduct_annual_role_access_review",
        "system_settings.configure_system_settings",
        "system_settings.management_password_policy",
        "system_settings.backup_restore_database",
    },
    "registrar": {
        "accounts.generate_student_account",
        "accounts.verify_email_address",
        "accounts.password_setup",
        "accounts.create_student_profile_account",
        "accounts.view_student_record",
        "accounts.update_student_record",
        "accounts.archive_student_record",
        "accounts.bulk_import_student_records",
        "accounts.bulk_import_students_to_vc",
        "accounts.reset_forgotten_password",
        "accounts.log_in_user",
        "accounts.manage_active_sessions",
        "accounts.view_role_based_dashboard",
        "academic_structure.view_academic_year",
        "academic_structure.add_academic_year_custom",
        "academic_structure.update_academic_year",
        "academic_structure.archive_academic_year",
        "academic_structure.manage_academic_structure_template",
        "academic_structure.rollover_academic_year",
        "academic_structure.promote_student",
        "academic_structure.validate_section_subject_coverage",
        "academic_structure.view_section_custom",
        "academic_structure.add_section_custom",
        "academic_structure.update_section_custom",
        "academic_structure.archive_section_custom",
        "academic_structure.view_subject_custom",
        "academic_structure.add_subject_custom",
        "academic_structure.update_subject_custom",
        "virtual_classrooms.create_virtual_classroom",
        "virtual_classrooms.view_virtual_classroom",
        "virtual_classrooms.add_student_to_class",
        "virtual_classrooms.archive_student_from_class",
        "virtual_classrooms.enroll_students_to_vc",
        "virtual_classrooms.update_virtual_classroom",
        "virtual_classrooms.archive_virtual_classroom",
        "assessments.view_grade_book",
        "assessments.view_student_performance_tracking",
        "attendance.generate_attendance_report",
        "attendance.view_attendance_records",
        "analytics.view_school_wide_analytics",
        "analytics.generate_attendance_trend_analytics",
        "analytics.generate_sf_reports",
    },
    "principal": {
        "accounts.verify_email_address",
        "accounts.password_setup",
        "accounts.reset_forgotten_password",
        "accounts.log_in_user",
        "accounts.manage_active_sessions",
        "accounts.view_role_based_dashboard",
        "accounts.view_student_record",
        "accounts.view_employee_account",
        "academic_structure.view_academic_year",
        "academic_structure.view_section_custom",
        "academic_structure.view_subject_custom",
        "analytics.view_school_wide_analytics",
        "analytics.view_class_level_analytics",
        "analytics.monitor_student_activity_logs",
        "assessments.view_assessment_custom",
        "assessments.view_student_performance_tracking",
        "attendance.view_attendance_records",
        "teaching_learning.view_download_learning_resource",
        "teaching_learning.access_subject_level_repository",
        "virtual_classrooms.view_virtual_classroom",
        "assessments.view_grade_book",
        "communication.view_announcements_custom",
        "communication.post_school_wide_announcement",
        "communication.update_school_wide_announcement",
        "communication.archive_school_wide_announcement",
    },
    "assistant_principal": {
        "accounts.verify_email_address",
        "accounts.password_setup",
        "accounts.view_student_record",
        "accounts.reset_forgotten_password",
        "accounts.log_in_user",
        "accounts.manage_active_sessions",
        "accounts.view_role_based_dashboard",
        "academic_structure.view_academic_year",
        "academic_structure.add_academic_year_custom",
        "academic_structure.manage_academic_structure_template",
        "academic_structure.view_section_custom",
        "academic_structure.add_section_custom",
        "academic_structure.update_section_custom",
        "academic_structure.archive_section_custom",
        "academic_structure.view_subject_custom",
        "academic_structure.add_subject_custom",
        "academic_structure.update_subject_custom",
        "virtual_classrooms.create_virtual_classroom",
        "virtual_classrooms.view_virtual_classroom",
        "virtual_classrooms.add_student_to_class",
        "virtual_classrooms.update_virtual_classroom",
        "virtual_classrooms.archive_virtual_classroom",
        "teaching_learning.view_download_learning_resource",
        "teaching_learning.access_subject_level_repository",
        "assessments.view_assessment_custom",
        "assessments.view_grade_book",
        "assessments.view_student_performance_tracking",
        "attendance.view_attendance_records",
        "communication.view_announcements_custom",
        "analytics.view_school_wide_analytics",
        "analytics.view_class_level_analytics",
    },
    "teacher": {
        "accounts.verify_email_address",
        "accounts.password_setup",
        "accounts.view_student_record",
        "accounts.reset_forgotten_password",
        "accounts.log_in_user",
        "accounts.manage_active_sessions",
        "accounts.view_role_based_dashboard",
        "teaching_learning.add_topic_custom",
        "teaching_learning.upload_learning_resource",
        "teaching_learning.view_download_learning_resource",
        "teaching_learning.access_subject_level_repository",
        "teaching_learning.manage_learning_resources",
        "assessments.create_assessment_custom",
        "assessments.access_assessment_repository",
        "assessments.view_assessment_custom",
        "assessments.update_assessment_custom",
        "assessments.monitor_student_submissions",
        "assessments.manually_grade_subjective_assessment",
        "assessments.view_grade_book",
        "assessments.view_student_performance_tracking",
        "assessments.auto_grade_objective_assessment",
        "assessments.compute_student_grade",
        "assessments.archive_assessment_custom",
        "attendance.record_class_attendance",
        "attendance.view_attendance_records",
        "attendance.update_attendance_record_custom",
        "attendance.generate_attendance_report",
        "communication.post_class_announcement",
        "communication.view_announcements_custom",
        "communication.update_class_announcement",
        "communication.archive_class_announcement",
        "communication.send_group_messaging",
        "communication.send_private_message",
        "analytics.view_class_level_analytics",
        "analytics.generate_submission_assessment_reports",
        "virtual_classrooms.view_virtual_classroom",
    },
    "student": {
        "accounts.verify_email_address",
        "accounts.password_setup",
        "accounts.reset_forgotten_password",
        "accounts.log_in_user",
        "accounts.manage_active_sessions",
        "accounts.view_role_based_dashboard",
        "virtual_classrooms.view_virtual_classroom",
        "teaching_learning.view_download_learning_resource",
        "assessments.view_assessment_custom",
        "assessments.complete_assessment_custom",
        "assessments.view_student_performance_tracking",
        "attendance.view_attendance_records",
        "communication.send_group_messaging",
        "communication.send_private_message",
        "communication.view_announcements_custom",
    },
    "tass_administrator": {
        "accounts.view_employee_account",
        "accounts.view_student_record",
        "accounts.log_in_user",
        "accounts.manage_active_sessions",
        "accounts.view_role_based_dashboard",
        "teaching_learning.access_subject_level_repository",
        "teaching_learning.view_download_learning_resource",
        "assessments.view_assessment_custom",
        "assessments.monitor_student_submissions",
        "assessments.trigger_missed_assessment_notification",
        "assessments.view_grade_book",
        "assessments.view_student_performance_tracking",
        "attendance.view_attendance_records",
        "attendance.generate_attendance_report",
        "academic_structure.view_academic_year",
        "academic_structure.view_section_custom",
        "academic_structure.view_subject_custom",
        "governance.retrieve_role_access_review_records",
        "analytics.view_school_wide_analytics",
        "analytics.view_class_level_analytics",
        "analytics.generate_attendance_trend_analytics",
        "analytics.monitor_student_activity_logs",
    },
    "quality_audit": {
        "accounts.view_student_record",
        "accounts.log_in_user",
        "accounts.manage_active_sessions",
        "accounts.view_role_based_dashboard",
        "academic_structure.view_academic_year",
        "academic_structure.view_section_custom",
        "academic_structure.view_subject_custom",
        "teaching_learning.access_subject_level_repository",
        "teaching_learning.view_download_learning_resource",
        "assessments.view_assessment_custom",
        "assessments.monitor_student_submissions",
        "assessments.view_grade_book",
        "assessments.view_student_performance_tracking",
        "attendance.view_attendance_records",
        "attendance.generate_attendance_report",
        "communication.view_announcements_custom",
        "governance.retrieve_role_access_review_records",
        "analytics.view_school_wide_analytics",
        "analytics.view_class_level_analytics",
        "analytics.generate_attendance_trend_analytics",
        "analytics.monitor_student_activity_logs",
        "virtual_classrooms.view_virtual_classroom",
    },
}

# Every custom permission is now mapped to at least one role. Auto-grading and
# grade computation are triggered by the system during submit/grade actions, but
# are additionally granted to the system administrator and teacher so the
# capabilities are represented in the role matrix.
UNGRANTED_CODENAMES = set()


def permission_for(qualified):
    """Return the Permission for an 'app_label.codename' string or None."""
    if "." not in qualified:
        return None
    app_label, codename = qualified.rsplit(".", 1)
    return Permission.objects.filter(
        content_type__app_label=app_label,
        codename=codename,
    ).first()


def sync_role_permissions(apply=True):
    """Reset every role group's permissions to exactly the matrix grants.

    Returns a list of (role_code, granted_count, missing) tuples.
    """
    from apps.accounts.models import Role

    results = []
    for code, _label in Role.Code.choices:
        wanted = ROLE_PERMISSIONS.get(code, set())
        perms = [p for p in (permission_for(q) for q in wanted) if p]
        missing = [q for q in wanted if not permission_for(q)]
        if apply:
            group, _ = Group.objects.get_or_create(name=code)
            group.permissions.set(perms)
        results.append((code, len(perms), missing))
    return results
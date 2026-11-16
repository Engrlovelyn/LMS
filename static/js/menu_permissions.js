(function () {
  'use strict';

  var source = document.getElementById('menu-permission-codenames');
  if (!source) return;

  var granted;
  try {
    granted = new Set(JSON.parse(source.textContent || '[]'));
  } catch (error) {
    console.error('Could not read the current menu permissions.', error);
    return;
  }

  var required = {
    schoolyear: ['academic_structure.view_academic_year'],
    acadyears: ['academic_structure.view_academic_year'],
    sections: ['academic_structure.view_section_custom'],
    sectioning: ['academic_structure.view_section_custom'],
    subjects: [
      'academic_structure.view_subject_custom',
      'virtual_classrooms.view_virtual_classroom',
    ],
    coverage: ['academic_structure.validate_section_subject_coverage'],
    promotion: ['academic_structure.promote_student'],
    classrooms: ['virtual_classrooms.view_virtual_classroom'],
    classes: ['virtual_classrooms.view_virtual_classroom'],
    students: ['accounts.view_student_record'],
    enrollment: [
      'accounts.view_student_record',
      'academic_structure.create_student_enrollment_inline',
      'virtual_classrooms.enroll_students_to_vc',
    ],
    materials: [
      'teaching_learning.access_subject_level_repository',
      'teaching_learning.view_download_learning_resource',
    ],
    resourcelibrary: [
      'teaching_learning.access_subject_level_repository',
      'teaching_learning.view_download_learning_resource',
    ],
    assessments: [
      'assessments.view_assessment_custom',
      'assessments.access_assessment_repository',
    ],
    assessmentrepo: ['assessments.access_assessment_repository'],
    performance: [
      'assessments.view_student_performance_tracking',
      'analytics.view_class_level_analytics',
    ],
    attendance: [
      'attendance.view_attendance_records',
      'attendance.generate_attendance_report',
    ],
    reports: [
      'analytics.view_school_wide_analytics',
      'analytics.view_class_level_analytics',
      'analytics.generate_sf_reports',
      'analytics.generate_submission_assessment_reports',
    ],
    analytics: [
      'analytics.view_school_wide_analytics',
      'analytics.view_class_level_analytics',
    ],
    demographics: [
      'analytics.view_school_wide_analytics',
      'accounts.view_student_record',
      'accounts.view_employee_account',
    ],
    studentdemographics: ['accounts.view_student_record'],
    staffdemographics: ['accounts.view_employee_account'],
    teachers: [
      'analytics.monitor_student_activity_logs',
      'accounts.view_employee_account',
    ],
    announcements: [
      'communication.view_announcements_custom',
      'communication.view_school_announcement',
    ],
    accounts: [
      'accounts.view_user_account',
      'accounts.view_employee_account',
    ],
    roles: ['accounts.role_based_access_control'],
    sftemplates: [
      'analytics.manage_sf_report_templates',
      'analytics.generate_sf_reports',
    ],
    security: [
      'accounts.unlock_account',
      'accounts.reset_account_password',
      'system_settings.configure_system_settings',
    ],
    sessions: ['accounts.manage_active_sessions'],
    backup: ['system_settings.backup_restore_database'],
    systemhealth: ['system_settings.configure_system_settings'],
  };

  document.querySelectorAll('.nav-item[onclick]').forEach(function (item) {
    var match = (item.getAttribute('onclick') || '').match(/showView\(['"]([^'"]+)['"]\)/);
    var permissions = match && required[match[1]];
    if (!permissions) return;
    if (!permissions.some(function (permission) { return granted.has(permission); })) {
      item.hidden = true;
      item.style.display = 'none';
      item.setAttribute('aria-hidden', 'true');
    }
  });
})();

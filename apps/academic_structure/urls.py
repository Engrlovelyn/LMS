from django.urls import path

from . import views

app_name = "academic"

urlpatterns = [
    path("years/", views.academic_year_list, name="academic_year_list"),
    path("years/add/", views.academic_year_add, name="academic_year_add"),
    path("years/<int:pk>/update/", views.academic_year_update, name="academic_year_update"),
    path("years/<int:pk>/archive/", views.academic_year_archive, name="academic_year_archive"),
    path("years/<int:pk>/finalize/", views.structure_finalize, name="structure_finalize"),
    path("structure/", views.structure_template_list, name="structure_template_list"),
    path("years/<int:pk>/rollover/", views.year_rollover, name="year_rollover"),
    path("sections/", views.section_list, name="section_list"),
    path("sections/add/", views.section_add, name="section_add"),
    path("sections/<int:pk>/update/", views.section_update, name="section_update"),
    path("sections/<int:pk>/archive/", views.section_archive, name="section_archive"),
    path("sections/<int:pk>/students/", views.section_students, name="section_students"),
    path(
        "sections/<int:pk>/students/<int:enrollment_pk>/remove/",
        views.section_student_remove,
        name="section_student_remove",
    ),
    path(
        "sections/<int:pk>/students/<int:enrollment_pk>/update/",
        views.student_enrollment_update,
        name="student_enrollment_update",
    ),
    path("subjects/", views.subject_list, name="subject_list"),
    path("subjects/add/", views.subject_add, name="subject_add"),
    path("subjects/<int:pk>/update/", views.subject_update, name="subject_update"),
    path("subjects/<int:pk>/archive/", views.subject_archive, name="subject_archive"),
    path("enrollments/", views.enrollment_list, name="enrollment_list"),
    path("enrollments/add/", views.enrollment_add, name="enrollment_add"),
    path("enrollments/<int:pk>/archive/", views.enrollment_archive, name="enrollment_archive"),
    path("promote/", views.promote_students, name="promote_students"),
    path("validate/", views.validate_coverage, name="validate_coverage"),
    path("years", views.academic_year_list, name="years"),
    path("sections", views.section_list, name="sections"),
    path("subjects", views.subject_list, name="subjects"),
    path("enrollments", views.enrollment_list, name="enrollments"),
]

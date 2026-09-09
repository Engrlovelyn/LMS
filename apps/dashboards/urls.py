from django.urls import path
from . import views

app_name = "dashboards"

urlpatterns = [
    path("ict/", views.system_admin, name="ict"),
    path("registrar/", views.registrar, name="registrar"),
    path("teacher/", views.teacher, name="teacher"),
    path("principal/", views.principal, name="principal"),
    path("academic-coordinator/", views.academic_coordinator, name="academic_coordinator"),
    path("student/", views.student, name="student"),
]

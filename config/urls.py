from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

from apps.dashboards import views as dashboards_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("dashboards/", include("apps.dashboards.urls")),
    path("", dashboards_views.hub, name="hub"),
    path("academic/", include("apps.academic_structure.urls")),
    path("classes/", include("apps.virtual_classrooms.urls")),
    path("teaching/", include("apps.teaching_learning.urls")),
    path("assessments/", include("apps.assessments.urls")),
    path("attendance/", include("apps.attendance.urls")),
    path("communication/", include("apps.communication.urls")),
    path("analytics/", include("apps.analytics.urls")),
    path("governance/", include("apps.governance.urls")),
    path("enrollment/", include("apps.enrollment.urls")),
    path("settings/", include("apps.system_settings.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
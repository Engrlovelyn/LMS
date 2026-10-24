from django.urls import path
from . import views

app_name = "dashboards"

# Each dashboard also accepts a trailing route (e.g. /teacher/classroom/assessment/)
# so deep links and refreshes re-load the same SPA; the client router restores the
# exact view/tab from the pathname.
_DASHBOARDS = [
    ("ict/", views.system_admin, "ict"),
    ("registrar/", views.registrar, "registrar"),
    ("teacher/", views.teacher, "teacher"),
    ("principal/", views.principal, "principal"),
    ("academic-coordinator/", views.academic_coordinator, "academic_coordinator"),
    ("student/", views.student, "student"),
    ("tass-administrator/", views.tass_administrator, "tass_administrator"),
    ("quality-audit/", views.quality_audit, "quality_audit"),
]

urlpatterns = [
    path("live-data/", views.live_dashboard_data, name="live_dashboard_data"),
    path("", views.hub, name="hub"),
    # Registered before the <path:route> catch-alls below so it resolves to the
    # JSON endpoint instead of the SPA.
    path(
        "academic-coordinator/data/",
        views.academic_coordinator_data,
        name="academic_coordinator_data",
    ),
]
for prefix, view, name in _DASHBOARDS:
    urlpatterns.append(path(prefix, view, name=name))
    urlpatterns.append(path(prefix + "<path:route>/", view, name=name + "_route"))
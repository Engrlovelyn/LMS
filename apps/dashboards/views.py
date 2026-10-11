from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.http import HttpResponseForbidden
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from . import context as dashboards_context


@ensure_csrf_cookie
@never_cache
@login_required
def hub(request):
    return render(request, "frontend/index.html")


def _require_role(user, role_code):
    if user.is_authenticated and user.is_superuser:
        return True
    return user.groups.filter(name=role_code).exists()


@ensure_csrf_cookie
@never_cache
@login_required
def system_admin(request, route=None):
    if not _require_role(request.user, "system_admin"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.system_admin_context(request.user)
    return render(request, "frontend/system_admin.html", ctx)


@ensure_csrf_cookie
@never_cache
@login_required
def registrar(request, route=None):
    if not _require_role(request.user, "registrar"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.registrar_context(request.user)
    return render(request, "frontend/registrar.html", ctx)


@ensure_csrf_cookie
@never_cache
@login_required
def teacher(request, route=None):
    if not _require_role(request.user, "teacher"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.teacher_context(request.user)
    return render(request, "frontend/teacher.html", ctx)


@ensure_csrf_cookie
@never_cache
@login_required
def principal(request, route=None):
    """The Principal dashboard, shared by both campuses.

    The High School Principal (``principal``) and the Elementary Principal
    (``elementary_principal``) reach this same view and the same template. The
    campus is not a different screen, it is a different set of rows: which
    grade bands, sections, classrooms and announcements get loaded is decided
    per-request in ``principal_context`` from the caller's campus scope.
    """
    if not (
        _require_role(request.user, "principal")
        or _require_role(request.user, "elementary_principal")
    ):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.principal_context(request.user)
    return render(request, "frontend/principal.html", ctx)


@ensure_csrf_cookie
@never_cache
@login_required
def academic_coordinator(request, route=None):
    if not _require_role(request.user, "assistant_principal"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.assistant_principal_context(request.user)
    return render(request, "frontend/academic_coordinator.html", ctx)


@ensure_csrf_cookie
@never_cache
@login_required
def student(request, route=None):
    if not _require_role(request.user, "student"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.student_context(request.user)
    return render(request, "frontend/student.html", ctx)


@ensure_csrf_cookie
@never_cache
@login_required
def tass_administrator(request, route=None):
    """Permanent redirect to the Quality Assurance dashboard.

    The TASS Administrator (SVI Administrator) role was a duplicate of Quality
    Assurance, so the route is kept only so existing bookmarks and deep links
    keep working instead of 404ing.
    """
    target = reverse("dashboards:quality_audit")
    if route:
        target = reverse("dashboards:quality_audit_route", kwargs={"route": route})
    return redirect(target)


@ensure_csrf_cookie
@never_cache
@login_required
def quality_audit(request, route=None):
    if not _require_role(request.user, "quality_audit"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.principal_context(request.user)
    return render(request, "frontend/principal.html", ctx)
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import HttpResponseForbidden
from django.views.decorators.cache import never_cache

from . import context as dashboards_context


@never_cache
@login_required
def hub(request):
    return render(request, "frontend/index.html")


def _require_role(user, role_code):
    return user.groups.filter(name=role_code).exists()


@never_cache
@login_required
def system_admin(request, route=None):
    if not _require_role(request.user, "system_admin"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.system_admin_context(request.user)
    return render(request, "frontend/system_admin.html", ctx)


@never_cache
@login_required
def registrar(request, route=None):
    if not _require_role(request.user, "registrar"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.registrar_context(request.user)
    return render(request, "frontend/registrar.html", ctx)


@never_cache
@login_required
def teacher(request, route=None):
    if not _require_role(request.user, "teacher"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.teacher_context(request.user)
    return render(request, "frontend/teacher.html", ctx)


@never_cache
@login_required
def principal(request, route=None):
    if not _require_role(request.user, "principal"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.principal_context(request.user)
    return render(request, "frontend/principal.html", ctx)


@never_cache
@login_required
def academic_coordinator(request, route=None):
    if not _require_role(request.user, "assistant_principal"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.assistant_principal_context(request.user)
    return render(request, "frontend/academic_coordinator.html", ctx)


@never_cache
@login_required
def student(request, route=None):
    if not _require_role(request.user, "student"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.student_context(request.user)
    return render(request, "frontend/student.html", ctx)


@never_cache
@login_required
def tass_administrator(request, route=None):
    if not _require_role(request.user, "tass_administrator"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.tass_administrator_context(request.user)
    return render(request, "frontend/system_admin.html", ctx)


@never_cache
@login_required
def quality_audit(request, route=None):
    if not _require_role(request.user, "quality_audit"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.quality_audit_context(request.user)
    return render(request, "frontend/quality_audit.html", ctx)
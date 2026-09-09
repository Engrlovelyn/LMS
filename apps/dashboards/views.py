from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import HttpResponseForbidden

from . import context as dashboards_context


def _require_role(user, role_code):
    return user.groups.filter(name=role_code).exists()


@login_required
def system_admin(request):
    if not _require_role(request.user, "system_admin"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.system_admin_context(request.user)
    return render(request, "dashboards/system_admin.html", ctx)


@login_required
def registrar(request):
    if not _require_role(request.user, "registrar"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.registrar_context(request.user)
    return render(request, "dashboards/registrar.html", ctx)


@login_required
def teacher(request):
    if not _require_role(request.user, "teacher"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.teacher_context(request.user)
    return render(request, "dashboards/teacher.html", ctx)


@login_required
def principal(request):
    if not _require_role(request.user, "principal"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.principal_context(request.user)
    return render(request, "dashboards/principal.html", ctx)


@login_required
def academic_coordinator(request):
    if not _require_role(request.user, "assistant_principal"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.assistant_principal_context(request.user)
    return render(request, "dashboards/assistant_principal.html", ctx)


@login_required
def student(request):
    if not _require_role(request.user, "student"):
        return HttpResponseForbidden("You do not have access to this dashboard.")
    ctx = dashboards_context.student_context(request.user)
    return render(request, "dashboards/student.html", ctx)
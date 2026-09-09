from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden


def _user_has_role(user, roles):
    return user.is_authenticated and user.groups.filter(name__in=roles).exists()


def role_required(*roles):
    """Restrict a view to users in one of the given role groups.

    Roles are matched against Django Groups which mirror Role.code values
    (system_admin, registrar, teacher, principal, assistant_principal, student).
    """
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not _user_has_role(request.user, roles):
                return HttpResponseForbidden(
                    "You do not have permission to access this page."
                )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def has_permission(user, codename):
    """True if user has a Django permission codename (direct or via group).

    Acccepts both fully-qualified codenames (``app_label.codename``) and
    bare codenames (``codename``). A bare codename is resolved by matching
    it against the user's effective permissions so that view-level guards
    work regardless of how each view spells the permission.
    """
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True

    if "." in codename:
        return user.has_perm(codename)

    if user.has_perm(codename):
        return True

    effective = user.get_all_permissions()  # e.g. {"assessments.view_assessment_custom", ...}
    for qualified in effective:
        if "." in qualified and qualified.rsplit(".", 1)[1] == codename:
            return True
    return False


def perm_required(codename):
    """Restrict a view to users holding a specific permission codename.

    Allows superusers, then falls back to Django's normal permission checks
    (including group permissions assigned by the RBAC matrix).
    """
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not has_permission(request.user, codename):
                return HttpResponseForbidden(
                    "You do not have permission to access this page."
                )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
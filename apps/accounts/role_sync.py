from django.contrib.auth.models import Group

from .models import Role, RoleAssignment


def sync_user_role_groups(user):
    """Repair stale Django group membership from the persistent role records."""
    if not getattr(user, "is_authenticated", False) or user.is_superuser:
        return
    role_codes = set(
        RoleAssignment.objects.filter(user=user).values_list("role__code", flat=True)
    )
    existing_role_groups = set(
        Group.objects.filter(name__in=role_codes).values_list("name", flat=True)
    )
    missing_groups = role_codes - existing_role_groups
    if missing_groups:
        from .rbac import permission_for, permissions_for_role

        for code in missing_groups:
            group, created = Group.objects.get_or_create(name=code)
            if created:
                permissions = [
                    permission
                    for permission in (
                        permission_for(codename)
                        for codename in permissions_for_role(code)
                    )
                    if permission is not None
                ]
                group.permissions.add(*permissions)
    group_codes = set(
        user.groups.filter(name__in=Role.Code.values).values_list("name", flat=True)
    )
    missing_codes = role_codes - group_codes
    if missing_codes:
        groups = list(Group.objects.filter(name__in=missing_codes))
        user.groups.add(*groups)
        for cache_name in (
            "_perm_cache",
            "_user_perm_cache",
            "_group_perm_cache",
        ):
            if hasattr(user, cache_name):
                delattr(user, cache_name)


class RoleGroupSyncMiddleware:
    """Keep legacy Django permission checks aligned with persistent roles."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        sync_user_role_groups(getattr(request, "user", None))
        return self.get_response(request)

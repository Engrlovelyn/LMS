from django.contrib.auth.models import Group
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import RoleAssignment


@receiver(post_save, sender=RoleAssignment)
def add_user_to_role_group(sender, instance, **kwargs):
    group, _ = Group.objects.get_or_create(name=instance.role.code)
    instance.user.groups.add(group)


@receiver(post_delete, sender=RoleAssignment)
def remove_user_from_role_group(sender, instance, **kwargs):
    group = Group.objects.filter(name=instance.role.code).first()
    if group:
        instance.user.groups.remove(group)
from django.urls import path

from . import views

app_name = "communication"

urlpatterns = [
    path("announcements/", views.announcement_list, name="announcements"),
    path("announcements/school/add/", views.school_announcement_add, name="school_announcement_add"),
    path(
        "announcements/school/<int:pk>/update/",
        views.school_announcement_update,
        name="school_announcement_update",
    ),
    path(
        "announcements/school/<int:pk>/archive/",
        views.school_announcement_archive,
        name="school_announcement_archive",
    ),
    path("announcements/class/add/", views.class_announcement_add, name="class_announcement_add"),
    path(
        "announcements/class/<int:pk>/update/",
        views.class_announcement_update,
        name="class_announcement_update",
    ),
    path(
        "announcements/class/<int:pk>/archive/",
        views.class_announcement_archive,
        name="class_announcement_archive",
    ),
    path("messages/", views.message_threads, name="messages"),
    path("messages/class/<int:vc_pk>/", views.group_message_thread, name="group_message_thread"),
    path("messages/private/<int:user_pk>/", views.private_message_thread, name="private_message_thread"),
]

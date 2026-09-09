from django.urls import path

from . import views

app_name = "teaching"

urlpatterns = [
    path("repository/", views.repository_list, name="repository_list"),
    path("resources/add/", views.resource_add, name="resource_add"),
    path("resources/<int:pk>/update/", views.resource_update, name="resource_update"),
    path("resources/<int:pk>/archive/", views.resource_archive, name="resource_archive"),
    path("classes/<int:vc_pk>/", views.class_materials, name="class_materials"),
    path("classes/<int:vc_pk>/topics/", views.topic_list, name="topic_list"),
    path("classes/<int:vc_pk>/topics/add/", views.topic_add, name="topic_add"),
    path("topics/<int:pk>/update/", views.topic_update, name="topic_update"),
    path("topics/<int:pk>/archive/", views.topic_archive, name="topic_archive"),
    path(
        "classes/<int:vc_pk>/resources/attach/",
        views.resource_attach,
        name="resource_attach",
    ),
]

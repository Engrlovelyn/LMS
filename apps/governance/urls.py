from django.urls import path

from . import views

app_name = "governance"

urlpatterns = [
    path("reviews/", views.review_list, name="review_list"),
    path("reviews/add/", views.review_add, name="review_add"),
    path("reviews/<int:pk>/update/", views.review_update, name="review_update"),
    path("reviews/<int:pk>/complete/", views.review_complete, name="review_complete"),
    path("reviews/<int:pk>/delete/", views.review_delete, name="review_delete"),
    path("reviews/<int:pk>/", views.review_detail, name="review_detail"),
    path("reviews", views.review_list, name="reviews"),
]

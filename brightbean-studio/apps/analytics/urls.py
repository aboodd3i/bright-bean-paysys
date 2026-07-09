"""URLs for the analytics page."""

from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("", views.analytics_index, name="index"),
    path("sync-posts/", views.sync_posts, name="sync_posts"),
    path("post/<uuid:post_id>/", views.post_detail, name="post_detail"),
    path("<uuid:account_id>/", views.analytics_account, name="account"),
]

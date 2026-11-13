from django.urls import path

from . import views

app_name = "ncr"

urlpatterns = [
    path("", views.ncr_list, name="list"),
    path("new/", views.create_ncr, name="create"),
    path("<uuid:public_id>/", views.ncr_detail, name="detail"),
    path("<uuid:public_id>/created/", views.creation_confirmation, name="created"),
]

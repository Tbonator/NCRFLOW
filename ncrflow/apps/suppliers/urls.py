from django.urls import path

from . import views

app_name = "suppliers"

urlpatterns = [
    path("", views.supplier_list, name="list"),
    path("new/", views.supplier_edit, name="create"),
    path("<int:supplier_id>/", views.supplier_detail, name="detail"),
    path("<int:supplier_id>/edit/", views.supplier_edit, name="edit"),
    path("<int:supplier_id>/status/", views.supplier_status, name="status"),
    path("<int:supplier_id>/contacts/new/", views.contact_edit, name="contact-create"),
    path(
        "<int:supplier_id>/contacts/<int:contact_id>/edit/",
        views.contact_edit,
        name="contact-edit",
    ),
    path(
        "<int:supplier_id>/contacts/<int:contact_id>/status/",
        views.contact_status,
        name="contact-status",
    ),
]

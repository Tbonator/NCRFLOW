from django.urls import path

from . import reference_views, views

app_name = "tenants"

urlpatterns = [
    path("workspace/", views.workspace, name="workspace"),
    path("companies/select/", views.company_selection, name="select"),
    path("companies/no-access/", views.no_company_access, name="no-access"),
    path("reference/", reference_views.reference_overview, name="reference-overview"),
    path("reference/<str:kind>/", reference_views.reference_list, name="reference-list"),
    path("reference/<str:kind>/new/", reference_views.reference_edit, name="reference-create"),
    path(
        "reference/<str:kind>/<int:reference_id>/edit/",
        reference_views.reference_edit,
        name="reference-edit",
    ),
    path(
        "reference/<str:kind>/<int:reference_id>/status/",
        reference_views.reference_status,
        name="reference-status",
    ),
]

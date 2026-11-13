from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from apps.tenants.permissions import require_company_role

from .forms import NCRCreateForm, NCRStatusFilterForm
from .models import NCR
from .services import (
    audit_history_for,
    company_today,
    create_internal_ncr,
    ncr_details_for,
    ncr_list_for,
)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def create_ncr(request):
    company, _ = require_company_role(
        by=request.user, actor_membership=request.membership, role=None
    )
    form = NCRCreateForm(request.POST if request.method == "POST" else None, company=company)
    if request.method == "POST" and form.is_valid():
        try:
            report = create_internal_ncr(
                by=request.user, actor_membership=request.membership, data=request.POST
            )
        except ValidationError as error:
            if hasattr(error, "error_dict"):
                for field, errors in error.error_dict.items():
                    form.add_error(field if field in form.fields else None, errors)
            else:
                form.add_error(None, error)
        else:
            return redirect("ncr:created", public_id=report.public_id)
    return render(request, "ncr/create.html", {"form": form})


@login_required
@never_cache
@require_GET
def creation_confirmation(request, public_id):
    company, actor = require_company_role(
        by=request.user, actor_membership=request.membership, role=None
    )
    # The receipt is deliberately originator-only; the general detail view is ncr_detail.
    report = get_object_or_404(
        NCR.objects.for_company(company)
        .filter(originator_id=actor.pk)
        .select_related("classification", "region", "discipline", "responsible_manager"),
        public_id=public_id,
    )
    return render(request, "ncr/created.html", {"report": report})


@login_required
@never_cache
@require_GET
def ncr_list(request):
    company, _ = require_company_role(
        by=request.user, actor_membership=request.membership, role=None
    )
    # An unknown status is shown as a form error and no filter is applied.
    filter_form = NCRStatusFilterForm(request.GET)
    selected_status = filter_form.cleaned_data["status"] if filter_form.is_valid() else ""
    reports = ncr_list_for(
        by=request.user,
        actor_membership=request.membership,
        today=company_today(company),
        status=selected_status,
    )
    page = Paginator(reports, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "ncr/list.html",
        {"page": page, "filter_form": filter_form, "selected_status": selected_status},
    )


@login_required
@never_cache
@require_GET
def ncr_detail(request, public_id):
    company, _ = require_company_role(
        by=request.user, actor_membership=request.membership, role=None
    )
    # Not visible, another company's and nonexistent NCRs all give the same 404.
    report = get_object_or_404(
        ncr_details_for(
            by=request.user, actor_membership=request.membership, today=company_today(company)
        ),
        public_id=public_id,
    )
    audit_entries = audit_history_for(
        by=request.user, actor_membership=request.membership, report=report
    )
    return render(
        request,
        "ncr/detail.html",
        {"report": report, "audit_entries": audit_entries, "company": company},
    )

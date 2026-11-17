from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.tenants.permissions import require_company_role

from .forms import (
    ExternalNCRCreateForm,
    NCRCreateForm,
    NCREditForm,
    NCRStatusFilterForm,
    NCRTransitionForm,
)
from .models import NCR
from .services import (
    audit_history_for,
    company_today,
    create_external_ncr,
    create_internal_ncr,
    editable_ncr_fields_for,
    ncr_actions_for,
    ncr_details_for,
    ncr_list_for,
    update_ncr,
)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def create_ncr(request):
    return _creation_page(request, external=False)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def create_external(request):
    return _creation_page(request, external=True)


def _creation_page(request, *, external):
    company, _ = require_company_role(
        by=request.user, actor_membership=request.membership, role=None
    )
    form_class = ExternalNCRCreateForm if external else NCRCreateForm
    create = create_external_ncr if external else create_internal_ncr
    form = form_class(request.POST if request.method == "POST" else None, company=company)
    if request.method == "POST" and form.is_valid():
        try:
            report = create(by=request.user, actor_membership=request.membership, data=request.POST)
        except ValidationError as error:
            if hasattr(error, "error_dict"):
                for field, errors in error.error_dict.items():
                    form.add_error(field if field in form.fields else None, errors)
            else:
                form.add_error(None, error)
        else:
            return redirect("ncr:created", public_id=report.public_id)
    return render(request, "ncr/create.html", {"form": form, "external": external})


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
        .select_related(
            "classification", "region", "discipline", "responsible_manager", "supplier"
        ),
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
    return _render_detail(request, public_id)


def _render_detail(request, public_id, *, form=None, status=200):
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
        {
            "report": report,
            "audit_entries": audit_entries,
            "company": company,
            "can_edit": bool(
                editable_ncr_fields_for(
                    by=request.user, actor_membership=request.membership, report=report
                )
            ),
            "transition_form": form if form is not None else NCRTransitionForm(),
            "ncr_actions": ncr_actions_for(
                by=request.user, actor_membership=request.membership, report=report
            ),
        },
        status=status,
    )


@login_required
@never_cache
@require_POST
def ncr_transition(request, public_id, action):
    methods = {"start-review": "start_review", "close": "close", "cancel": "cancel"}
    if action not in methods:
        raise Http404
    report = get_object_or_404(
        ncr_details_for(
            by=request.user,
            actor_membership=request.membership,
            today=company_today(request.company),
        ),
        public_id=public_id,
    )
    # The model method rechecks current state and authority inside the write transaction.
    kwargs = {"by": request.user, "actor_membership": request.membership}
    if action == "cancel":
        kwargs["reason"] = request.POST.get("reason", "")
    try:
        getattr(report, methods[action])(**kwargs)
    except ValidationError as error:
        form = NCRTransitionForm(request.POST)
        form.is_valid()
        form.add_error(None, error.messages)
        return _render_detail(request, public_id, form=form, status=409)
    messages.success(request, f"{report.number}: {report.get_status_display()}.")
    return redirect("ncr:detail", public_id=public_id)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def edit_ncr(request, public_id):
    company, _ = require_company_role(
        by=request.user, actor_membership=request.membership, role=None
    )
    report = get_object_or_404(
        ncr_details_for(
            by=request.user, actor_membership=request.membership, today=company_today(company)
        ),
        public_id=public_id,
    )
    allowed = editable_ncr_fields_for(
        by=request.user, actor_membership=request.membership, report=report
    )
    if not allowed:
        raise PermissionDenied("You cannot edit this NCR in its current status.")
    form = NCREditForm(
        request.POST if request.method == "POST" else None,
        company=company,
        report=report,
        editable_fields=allowed,
    )
    if request.method == "POST" and form.is_valid():
        try:
            report = update_ncr(
                by=request.user,
                actor_membership=request.membership,
                report_id=report.pk,
                data=request.POST,
            )
        except ValidationError as error:
            if hasattr(error, "error_dict"):
                for field, errors in error.error_dict.items():
                    form.add_error(field if field in form.fields else None, errors)
            else:
                conflict = NCRTransitionForm({})
                conflict.add_error(None, error.messages)
                return _render_detail(request, public_id, form=conflict, status=409)
        else:
            messages.success(request, f"{report.number} saved.")
            return redirect("ncr:detail", public_id=public_id)
    return render(request, "ncr/edit.html", {"form": form, "report": report})

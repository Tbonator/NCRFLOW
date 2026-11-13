from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import Membership
from .permissions import require_company_role
from .reference_data import REFERENCE_TYPES, save_reference, set_reference_active


def _config(kind):
    if kind not in REFERENCE_TYPES:
        raise Http404("Reference-data type not found.")
    return REFERENCE_TYPES[kind]


def _quality_company(request):
    company, _ = require_company_role(
        by=request.user, actor_membership=request.membership, role=Membership.Role.QUALITY
    )
    return company


@login_required
@never_cache
@require_GET
def reference_overview(request):
    _quality_company(request)
    return render(request, "reference/overview.html", {"sections": REFERENCE_TYPES})


@login_required
@never_cache
@require_GET
def reference_list(request, kind):
    config = _config(kind)
    company = _quality_company(request)
    records = config["model"].objects.for_company(company).order_by("name", "pk")
    page = Paginator(records, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "reference/list.html",
        {
            "kind": kind,
            "title": config["title"],
            "page": page,
            "has_sla": kind == "classifications",
        },
    )


def _form_errors(form, error):
    if hasattr(error, "error_dict"):
        for field, errors in error.error_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def reference_edit(request, kind, reference_id=None):
    config = _config(kind)
    company = _quality_company(request)
    record = (
        config["model"](company=company)
        if reference_id is None
        else get_object_or_404(config["model"].objects.for_company(company), pk=reference_id)
    )
    form = config["form"](request.POST if request.method == "POST" else None, instance=record)
    if request.method == "POST" and form.is_valid():
        try:
            save_reference(
                by=request.user,
                actor_membership=request.membership,
                kind=kind,
                data=request.POST,
                reference_id=reference_id,
            )
        except config["model"].DoesNotExist as error:
            raise Http404("Reference not found.") from error
        except ValidationError as error:
            _form_errors(form, error)
        else:
            messages.success(request, "Reference saved.")
            return redirect("tenants:reference-list", kind=kind)
    return render(
        request,
        "reference/edit.html",
        {
            "form": form,
            "kind": kind,
            "title": config["title"],
            "editing": reference_id is not None,
        },
    )


@login_required
@never_cache
@require_POST
def reference_status(request, kind, reference_id):
    config = _config(kind)
    _quality_company(request)
    try:
        reference = set_reference_active(
            by=request.user,
            actor_membership=request.membership,
            kind=kind,
            reference_id=reference_id,
            data=request.POST,
        )
    except config["model"].DoesNotExist as error:
        raise Http404("Reference not found.") from error
    except ValidationError:
        return HttpResponseBadRequest("Choose an active or inactive status.")
    messages.success(
        request, "Reference activated." if reference.active else "Reference deactivated."
    )
    return redirect("tenants:reference-list", kind=kind)

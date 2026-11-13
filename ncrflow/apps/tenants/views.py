from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .context import eligible_memberships, select_membership
from .forms import CompanySelectionForm


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def company_selection(request):
    if not eligible_memberships(request.user).exists():
        return redirect("tenants:no-access")
    form = CompanySelectionForm(
        request.POST if request.method == "POST" else None,
        user=request.user,
        initial={"membership": getattr(request.membership, "pk", None)},
    )
    if request.method == "POST" and form.is_valid():
        select_membership(request, form.cleaned_data["membership"].pk)
        messages.success(request, "Company selected.")
        return redirect("tenants:workspace")
    return render(request, "tenants/select_company.html", {"form": form})


@login_required
@never_cache
@require_GET
def no_company_access(request):
    if request.membership is not None:
        return redirect("tenants:workspace")
    if eligible_memberships(request.user).exists():
        return redirect("tenants:select")
    return render(request, "tenants/no_access.html", status=403)


@login_required
@never_cache
@require_GET
def workspace(request):
    return render(request, "tenants/workspace.html")

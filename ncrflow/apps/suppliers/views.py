from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .forms import SupplierContactForm, SupplierForm
from .models import Supplier, SupplierContact


def _suppliers(request):
    """Company-admin check first (403), then only the current company's suppliers."""
    return services.suppliers_for(by=request.user, actor_membership=request.membership)


def _form_errors(form, error):
    if hasattr(error, "error_dict"):
        for field, errors in error.error_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error)


@login_required
@never_cache
@require_GET
def supplier_list(request):
    suppliers = _suppliers(request).select_related("region").order_by("name", "pk")
    page = Paginator(suppliers, 25).get_page(request.GET.get("page"))
    return render(request, "suppliers/list.html", {"page": page})


@login_required
@never_cache
@require_GET
def supplier_detail(request, supplier_id):
    supplier = get_object_or_404(_suppliers(request).select_related("region"), pk=supplier_id)
    contacts = services.supplier_contacts_for(
        by=request.user, actor_membership=request.membership, supplier=supplier
    ).order_by("email", "pk")
    return render(request, "suppliers/detail.html", {"supplier": supplier, "contacts": contacts})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def supplier_edit(request, supplier_id=None):
    suppliers = _suppliers(request)
    supplier = (
        Supplier(company=request.company)
        if supplier_id is None
        else get_object_or_404(suppliers, pk=supplier_id)
    )
    form = SupplierForm(
        request.POST if request.method == "POST" else None,
        instance=supplier,
        company=request.company,
    )
    if request.method == "POST" and form.is_valid():
        try:
            if supplier_id is None:
                saved = services.create_supplier(
                    by=request.user, actor_membership=request.membership, data=request.POST
                )
            else:
                saved = services.update_supplier(
                    by=request.user,
                    actor_membership=request.membership,
                    supplier_id=supplier_id,
                    data=request.POST,
                )
        except Supplier.DoesNotExist as error:
            raise Http404("Supplier not found.") from error
        except ValidationError as error:
            _form_errors(form, error)
        else:
            messages.success(request, "Supplier saved.")
            return redirect("suppliers:detail", supplier_id=saved.pk)
    return render(
        request,
        "suppliers/edit.html",
        {"form": form, "supplier": supplier, "editing": supplier_id is not None},
    )


@login_required
@never_cache
@require_POST
def supplier_status(request, supplier_id):
    _suppliers(request)
    try:
        supplier = services.set_supplier_active(
            by=request.user,
            actor_membership=request.membership,
            supplier_id=supplier_id,
            data=request.POST,
        )
    except Supplier.DoesNotExist as error:
        raise Http404("Supplier not found.") from error
    except ValidationError:
        return HttpResponseBadRequest("Choose an active or inactive status.")
    messages.success(request, "Supplier activated." if supplier.active else "Supplier deactivated.")
    return redirect("suppliers:list")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def contact_edit(request, supplier_id, contact_id=None):
    supplier = get_object_or_404(_suppliers(request), pk=supplier_id)
    if contact_id is None:
        contact = SupplierContact(company=request.company, supplier=supplier)
    else:
        contact = get_object_or_404(
            services.supplier_contacts_for(
                by=request.user, actor_membership=request.membership, supplier=supplier
            ),
            pk=contact_id,
        )
    form = SupplierContactForm(request.POST if request.method == "POST" else None, instance=contact)
    if request.method == "POST" and form.is_valid():
        try:
            if contact_id is None:
                services.create_supplier_contact(
                    by=request.user,
                    actor_membership=request.membership,
                    supplier_id=supplier_id,
                    data=request.POST,
                )
            else:
                services.update_supplier_contact(
                    by=request.user,
                    actor_membership=request.membership,
                    supplier_id=supplier_id,
                    contact_id=contact_id,
                    data=request.POST,
                )
        except (Supplier.DoesNotExist, SupplierContact.DoesNotExist) as error:
            raise Http404("Supplier contact not found.") from error
        except ValidationError as error:
            _form_errors(form, error)
        else:
            messages.success(request, "Contact saved.")
            return redirect("suppliers:detail", supplier_id=supplier.pk)
    return render(
        request,
        "suppliers/contact_edit.html",
        {"form": form, "supplier": supplier, "editing": contact_id is not None},
    )


@login_required
@never_cache
@require_POST
def contact_status(request, supplier_id, contact_id):
    _suppliers(request)
    try:
        contact = services.set_supplier_contact_active(
            by=request.user,
            actor_membership=request.membership,
            supplier_id=supplier_id,
            contact_id=contact_id,
            data=request.POST,
        )
    except (Supplier.DoesNotExist, SupplierContact.DoesNotExist) as error:
        raise Http404("Supplier contact not found.") from error
    except ValidationError:
        return HttpResponseBadRequest("Choose an active or inactive status.")
    messages.success(request, "Contact activated." if contact.active else "Contact deactivated.")
    return redirect("suppliers:detail", supplier_id=supplier_id)

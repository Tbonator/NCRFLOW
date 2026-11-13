"""Audited write boundary for suppliers and supplier contacts (Company admin only)."""

import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Value
from django.db.models.functions import Lower

from apps.tenants.models import AuditEntry, Membership, Region
from apps.tenants.permissions import require_company_role

from .forms import ActiveStatusForm, SupplierContactForm, SupplierForm
from .models import Supplier, SupplierContact

SUPPLIER_FIELDS = ("name", "region_id", "active")
CONTACT_FIELDS = ("name", "email", "active")


def require_supplier_admin(*, by, actor_membership, lock=False):
    """The one place that decides who may see and manage suppliers and their contacts."""
    return require_company_role(
        by=by, actor_membership=actor_membership, role=Membership.Role.COMPANY_ADMIN, lock=lock
    )


def suppliers_for(*, by, actor_membership):
    company, _ = require_supplier_admin(by=by, actor_membership=actor_membership)
    return Supplier.objects.for_company(company)


def supplier_contacts_for(*, by, actor_membership, supplier):
    company, _ = require_supplier_admin(by=by, actor_membership=actor_membership)
    return SupplierContact.objects.for_company(company).filter(supplier_id=supplier.pk)


def _label(record):
    # audit_entry.object_label holds at most 100 characters.
    return (record.name if isinstance(record, Supplier) else record.email)[:100]


def _audit_created(company, actor, record, object_type, values):
    AuditEntry.objects.for_company(company).create(
        company=company,
        actor_user=actor,
        object_type=object_type,
        object_id=record.pk,
        object_label=_label(record),
        action=AuditEntry.Action.CREATED,
        new_value=json.dumps(values, sort_keys=True),
    )


def _audit_changes(company, actor, record, object_type, changes):
    for field, old_value, new_value in changes:
        AuditEntry.objects.for_company(company).create(
            company=company,
            actor_user=actor,
            object_type=object_type,
            object_id=record.pk,
            object_label=_label(record),
            action=AuditEntry.Action.FIELD_CHANGED,
            field_name=field,
            old_value=json.dumps(old_value),
            new_value=json.dumps(new_value),
        )


def _save(record, message, update_fields=None):
    try:
        # Keep an IntegrityError inside a savepoint so it does not poison the transaction.
        with transaction.atomic():
            record.save(update_fields=update_fields)
    except IntegrityError as error:
        raise ValidationError(message) from error


def _check_supplier_name(company, supplier, name):
    duplicates = (
        Supplier.objects.for_company(company)
        .annotate(normalised_name=Lower("name"))
        .filter(normalised_name=Lower(Value(name)))
    )
    if supplier.pk is not None:
        duplicates = duplicates.exclude(pk=supplier.pk)
    if duplicates.exists():
        raise ValidationError({"name": "A supplier with this name already exists in your company."})


def _check_region(company, region):
    if region is None:
        return
    available = (
        Region.objects.for_company(company).select_for_update().filter(pk=region.pk, active=True)
    )
    if not available.exists():
        raise ValidationError({"region": "Choose an active region in this company."})


def _check_contact_email(company, contact, email):
    duplicates = SupplierContact.objects.for_company(company).filter(email=email)
    if contact.pk is not None:
        duplicates = duplicates.exclude(pk=contact.pk)
    if duplicates.exists():
        raise ValidationError(
            {"email": "A supplier contact with this email already exists in your company."}
        )


def _changes(old_values, record, fields):
    return [
        (field, old_values[field], getattr(record, field))
        for field in fields
        if old_values[field] != getattr(record, field)
    ]


@transaction.atomic
def create_supplier(*, by, actor_membership, data):
    company, actor = require_supplier_admin(by=by, actor_membership=actor_membership, lock=True)
    supplier = Supplier(company=company)
    form = SupplierForm(data, instance=supplier, company=company)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    _check_supplier_name(company, supplier, form.cleaned_data["name"])
    _check_region(company, form.cleaned_data["region"])
    _save(supplier, "This supplier could not be saved. Check for a duplicate name.")
    _audit_created(
        company,
        actor,
        supplier,
        "supplier",
        {field: getattr(supplier, field) for field in SUPPLIER_FIELDS},
    )
    return supplier


@transaction.atomic
def update_supplier(*, by, actor_membership, supplier_id, data):
    company, actor = require_supplier_admin(by=by, actor_membership=actor_membership, lock=True)
    supplier = Supplier.objects.for_company(company).select_for_update().get(pk=supplier_id)
    old_values = {field: getattr(supplier, field) for field in SUPPLIER_FIELDS}
    form = SupplierForm(data, instance=supplier, company=company)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    changes = _changes(old_values, supplier, SUPPLIER_FIELDS)
    if not changes:
        return supplier
    changed = {field for field, _, _ in changes}
    if "name" in changed:
        _check_supplier_name(company, supplier, supplier.name)
    if "region_id" in changed:
        # An unchanged inactive region may be kept; a newly chosen one must be active.
        _check_region(company, form.cleaned_data["region"])
    _save(
        supplier,
        "This supplier could not be saved. Check for a duplicate name.",
        update_fields=[*changed, "updated_at"],
    )
    _audit_changes(company, actor, supplier, "supplier", changes)
    return supplier


@transaction.atomic
def set_supplier_active(*, by, actor_membership, supplier_id, data):
    company, actor = require_supplier_admin(by=by, actor_membership=actor_membership, lock=True)
    supplier = Supplier.objects.for_company(company).select_for_update().get(pk=supplier_id)
    form = ActiveStatusForm(data)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    active = form.cleaned_data["active"]
    if supplier.active != active:
        old_value = supplier.active
        supplier.active = active
        supplier.save(update_fields=["active", "updated_at"])
        _audit_changes(company, actor, supplier, "supplier", [("active", old_value, active)])
    return supplier


@transaction.atomic
def create_supplier_contact(*, by, actor_membership, supplier_id, data):
    company, actor = require_supplier_admin(by=by, actor_membership=actor_membership, lock=True)
    # The supplier comes from the company-scoped lookup, never from the submitted form.
    supplier = Supplier.objects.for_company(company).select_for_update().get(pk=supplier_id)
    contact = SupplierContact(company=company, supplier=supplier)
    form = SupplierContactForm(data, instance=contact)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    _check_contact_email(company, contact, form.cleaned_data["email"])
    _save(contact, "This contact could not be saved. Check for a duplicate email.")
    _audit_created(
        company,
        actor,
        contact,
        "supplier_contact",
        {
            "supplier_id": contact.supplier_id,
            "public_id": str(contact.public_id),
            **{field: getattr(contact, field) for field in CONTACT_FIELDS},
        },
    )
    return contact


def _locked_contact(company, supplier_id, contact_id):
    # Lock order: supplier, then contact. The contact must belong to the supplier in the URL.
    supplier = Supplier.objects.for_company(company).select_for_update().get(pk=supplier_id)
    return (
        SupplierContact.objects.for_company(company)
        .select_for_update()
        .get(pk=contact_id, supplier_id=supplier.pk)
    )


@transaction.atomic
def update_supplier_contact(*, by, actor_membership, supplier_id, contact_id, data):
    company, actor = require_supplier_admin(by=by, actor_membership=actor_membership, lock=True)
    contact = _locked_contact(company, supplier_id, contact_id)
    old_values = {field: getattr(contact, field) for field in CONTACT_FIELDS}
    form = SupplierContactForm(data, instance=contact)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    changes = _changes(old_values, contact, CONTACT_FIELDS)
    if not changes:
        return contact
    changed = {field for field, _, _ in changes}
    if "email" in changed:
        _check_contact_email(company, contact, contact.email)
    _save(
        contact,
        "This contact could not be saved. Check for a duplicate email.",
        update_fields=[*changed, "updated_at"],
    )
    _audit_changes(company, actor, contact, "supplier_contact", changes)
    return contact


@transaction.atomic
def set_supplier_contact_active(*, by, actor_membership, supplier_id, contact_id, data):
    company, actor = require_supplier_admin(by=by, actor_membership=actor_membership, lock=True)
    contact = _locked_contact(company, supplier_id, contact_id)
    form = ActiveStatusForm(data)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    active = form.cleaned_data["active"]
    if contact.active != active:
        old_value = contact.active
        contact.active = active
        contact.save(update_fields=["active", "updated_at"])
        _audit_changes(company, actor, contact, "supplier_contact", [("active", old_value, active)])
    return contact

"""The three explicit reference types and their audited write boundary."""

import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Value
from django.db.models.functions import Lower

from apps.ncr.models import Classification

from .models import AuditEntry, Discipline, Membership, Region
from .permissions import require_company_role
from .reference_forms import ClassificationForm, DisciplineForm, ReferenceStatusForm, RegionForm

REFERENCE_TYPES = {
    "regions": {"model": Region, "form": RegionForm, "title": "Regions", "type": "region"},
    "disciplines": {
        "model": Discipline,
        "form": DisciplineForm,
        "title": "Disciplines",
        "type": "discipline",
    },
    "classifications": {
        "model": Classification,
        "form": ClassificationForm,
        "title": "Classifications",
        "type": "classification",
    },
}


def reference_type(kind):
    try:
        return REFERENCE_TYPES[kind]
    except KeyError as error:
        raise ValidationError("Unknown reference-data type.") from error


def _record_changes(company, actor, reference, object_type, old_values, new_values):
    if old_values is None:
        AuditEntry.objects.for_company(company).create(
            company=company,
            actor_user=actor,
            object_type=object_type,
            object_id=reference.pk,
            object_label=reference.name,
            action=AuditEntry.Action.CREATED,
            new_value=json.dumps(new_values, sort_keys=True),
        )
        return
    for field, value in new_values.items():
        if old_values[field] != value:
            AuditEntry.objects.for_company(company).create(
                company=company,
                actor_user=actor,
                object_type=object_type,
                object_id=reference.pk,
                object_label=reference.name,
                action=AuditEntry.Action.FIELD_CHANGED,
                field_name=field,
                old_value=json.dumps(old_values[field]),
                new_value=json.dumps(value),
            )


@transaction.atomic
def save_reference(*, by, actor_membership, kind, data, reference_id=None):
    company, actor = require_company_role(
        by=by, actor_membership=actor_membership, role=Membership.Role.QUALITY, lock=True
    )
    config = reference_type(kind)
    model = config["model"]
    if reference_id is None:
        reference = model(company=company)
        old_values = None
    else:
        reference = model.objects.for_company(company).select_for_update().get(pk=reference_id)
        old_values = {field: getattr(reference, field) for field in config["form"].Meta.fields}
    form = config["form"](data, instance=reference)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    new_values = form.cleaned_data
    duplicates = (
        model.objects.for_company(company)
        .annotate(normalised_name=Lower("name"))
        .filter(normalised_name=Lower(Value(new_values["name"])))
    )
    if reference.pk is not None:
        duplicates = duplicates.exclude(pk=reference.pk)
    if duplicates.exists():
        raise ValidationError({"name": "This name is already used in your company."})
    if old_values is not None and old_values == new_values:
        return reference
    try:
        # Keep an IntegrityError inside a savepoint so it does not poison the transaction.
        with transaction.atomic():
            reference.save()
    except IntegrityError as error:
        raise ValidationError(
            "This reference could not be saved. Check for a duplicate name."
        ) from error
    _record_changes(company, actor, reference, config["type"], old_values, new_values)
    return reference


@transaction.atomic
def set_reference_active(*, by, actor_membership, kind, reference_id, data):
    company, actor = require_company_role(
        by=by, actor_membership=actor_membership, role=Membership.Role.QUALITY, lock=True
    )
    config = reference_type(kind)
    reference = (
        config["model"].objects.for_company(company).select_for_update().get(pk=reference_id)
    )
    form = ReferenceStatusForm(data)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    active = form.cleaned_data["active"]
    if reference.active != active:
        old_values = {"active": reference.active}
        reference.active = active
        reference.save(update_fields=["active", "updated_at"])
        _record_changes(company, actor, reference, config["type"], old_values, {"active": active})
    return reference

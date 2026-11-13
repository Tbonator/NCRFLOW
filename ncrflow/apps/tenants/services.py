"""Transactional provisioning; callers supply a verified actor membership."""

import json

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .forms import CompanyForm, MemberProvisionForm, MembershipChangeForm
from .models import AuditEntry, Company, Membership


def _validated(form):
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    return form.cleaned_data


def _platform_actor(by):
    actor = (
        get_user_model()
        .objects.filter(
            pk=getattr(by, "pk", None), is_active=True, is_staff=True, is_superuser=True
        )
        .first()
    )
    if actor is None:
        raise PermissionDenied("Only the platform developer may provision a company.")
    return actor


def _company_admin(by, actor_membership):
    # Company first, then memberships: all services use the same lock order.
    company = (
        Company.objects.select_for_update()
        .filter(pk=getattr(actor_membership, "company_id", None))
        .first()
    )
    actor = get_user_model().objects.filter(pk=getattr(by, "pk", None)).first()
    if company is None or actor is None:
        raise PermissionDenied("An active Company admin membership is required.")
    membership = (
        Membership.objects.for_company(company)
        .select_for_update()
        .filter(pk=actor_membership.pk, user_id=actor.pk, active=True)
        .first()
    )
    if (
        not actor.is_active
        or not company.active
        or membership is None
        or membership.role != Membership.Role.COMPANY_ADMIN
    ):
        raise PermissionDenied("An active Company admin membership is required.")
    return company, actor


def _created(company, by, obj, object_type, values):
    return AuditEntry.objects.for_company(company).create(
        company=company,
        actor_user=by,
        object_type=object_type,
        object_id=obj.pk,
        object_label=str(values.get("name", values.get("email", obj.pk)))[:100],
        action=AuditEntry.Action.CREATED,
        new_value=json.dumps(values, sort_keys=True),
    )


def _provision_member(company, by, values):
    # Narrow global-identity bootstrap, authorised before this function is called.
    # Existing global identity fields are never updated by a company administrator.
    user, user_created = get_user_model().objects.get_or_create(
        email=values["email"],
        defaults={
            "full_name": values["full_name"],
            "password": make_password(values.get("password") or None),
        },
    )
    if not user.is_active:
        raise ValidationError("This account cannot be provisioned.")
    membership, member_created = Membership.objects.for_company(company).get_or_create(
        company=company, user=user, defaults={"role": values["role"]}
    )
    if not member_created:
        if not membership.active or membership.role != values["role"]:
            raise ValidationError("Use the membership-change service for an existing membership.")
        return membership
    if user_created:
        _created(
            company,
            by,
            user,
            "user",
            {"email": user.email, "full_name": user.full_name, "is_active": user.is_active},
        )
    _created(
        company,
        by,
        membership,
        "membership",
        {"user_id": user.pk, "role": membership.role, "active": membership.active},
    )
    return membership


@transaction.atomic
def provision_company(*, by, company_data, admin_data):
    actor = _platform_actor(by)
    form = CompanyForm(company_data)
    _validated(form)
    values = _validated(MemberProvisionForm({**admin_data, "role": Membership.Role.COMPANY_ADMIN}))
    company = form.save()
    _created(
        company,
        actor,
        company,
        "company",
        {
            "name": company.name,
            "timezone": company.timezone,
            "escalation_tier_one_days": company.escalation_tier_one_days,
            "escalation_tier_two_days": company.escalation_tier_two_days,
            "active": company.active,
        },
    )
    membership = _provision_member(company, actor, values)
    return company, membership


@transaction.atomic
def provision_member(*, by, actor_membership, data):
    company, actor = _company_admin(by, actor_membership)
    values = _validated(MemberProvisionForm(data))
    return _provision_member(company, actor, values)


@transaction.atomic
def update_membership(*, by, actor_membership, membership_id, data):
    company, actor = _company_admin(by, actor_membership)
    values = _validated(MembershipChangeForm(data))
    membership = Membership.objects.for_company(company).select_for_update().get(pk=membership_id)
    changes = []
    for field in ("role", "active"):
        old_value = getattr(membership, field)
        new_value = values[field]
        if old_value != new_value:
            setattr(membership, field, new_value)
            changes.append((field, old_value, new_value))
    if changes:
        membership.save(update_fields=[*(field for field, _, _ in changes), "updated_at"])
        for field, old_value, new_value in changes:
            AuditEntry.objects.for_company(company).create(
                company=company,
                actor_user=actor,
                object_type="membership",
                object_id=membership.pk,
                action=AuditEntry.Action.FIELD_CHANGED,
                field_name=field,
                old_value=json.dumps(old_value),
                new_value=json.dumps(new_value),
            )
    return membership

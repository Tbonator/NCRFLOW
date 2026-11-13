import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction

from apps.tenants.models import AuditEntry, Company, Membership
from apps.tenants.services import provision_company, provision_member, update_membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def developer():
    return get_user_model().objects.create_superuser("developer@example.com", "test-password")


@pytest.fixture
def tenant(developer):
    return provision_company(
        by=developer,
        company_data={
            "name": "Factory A",
            "timezone": "Africa/Johannesburg",
            "escalation_tier_one_days": 3,
            "escalation_tier_two_days": 7,
        },
        admin_data={"email": "admin-a@example.com", "full_name": "Admin A"},
    )


@pytest.fixture
def other_tenant(developer):
    return provision_company(
        by=developer,
        company_data={
            "name": "Factory B",
            "timezone": "Africa/Johannesburg",
            "escalation_tier_one_days": 3,
            "escalation_tier_two_days": 7,
        },
        admin_data={"email": "admin-b@example.com"},
    )


def member_data(email="member@example.com", role=Membership.Role.ORIGINATOR):
    return {"email": email, "role": role, "full_name": "New Person"}


def test_company_provisioning_audits_company_user_and_membership(tenant, developer):
    company, admin = tenant
    assert admin.role == Membership.Role.COMPANY_ADMIN
    assert admin.company_id == company.pk
    assert not admin.user.is_staff and not admin.user.is_superuser
    entries = AuditEntry.objects.for_company(company)
    assert set(entries.values_list("object_type", flat=True)) == {"company", "user", "membership"}
    assert set(entries.values_list("actor_user_id", flat=True)) == {developer.pk}
    assert entries.count() == 3
    assert not admin.user.has_usable_password()


def test_customer_cannot_provision_a_company(tenant):
    _, admin = tenant
    with pytest.raises(PermissionDenied):
        provision_company(by=admin.user, company_data={}, admin_data={})
    assert Company.objects.count() == 1  # platform-level test assertion only


def test_company_provisioning_rechecks_platform_flags(developer):
    get_user_model().objects.filter(pk=developer.pk).update(is_active=False)
    with pytest.raises(PermissionDenied):
        provision_company(by=developer, company_data={}, admin_data={})


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "Not/AZone"},
        {"escalation_tier_one_days": 0},
        {"escalation_tier_two_days": 3},
        {"name": ""},
    ],
)
def test_invalid_company_settings_are_rejected(developer, changes):
    values = {
        "name": "Factory",
        "timezone": "Africa/Johannesburg",
        "escalation_tier_one_days": 3,
        "escalation_tier_two_days": 7,
        **changes,
    }
    with pytest.raises(ValidationError):
        provision_company(by=developer, company_data=values, admin_data={"email": "a@example.com"})
    assert not Company.objects.exists()


def test_membership_creation_is_scoped_and_audited(tenant, other_tenant):
    company, admin = tenant
    other_company, _ = other_tenant
    member = provision_member(by=admin.user, actor_membership=admin, data=member_data())
    assert member.company_id == company.pk
    assert Membership.objects.for_company(company).filter(pk=member.pk).exists()
    assert not Membership.objects.for_company(other_company).filter(pk=member.pk).exists()
    entries = AuditEntry.objects.for_company(company)
    assert entries.filter(object_type="membership", object_id=member.pk).count() == 1
    assert entries.filter(object_type="user", object_id=member.user_id).count() == 1
    assert (
        not AuditEntry.objects.for_company(other_company)
        .filter(object_type="user", object_id=member.user_id)
        .exists()
    )


def test_extra_company_and_platform_fields_cannot_override_provisioning(tenant, other_tenant):
    company, admin = tenant
    other_company, _ = other_tenant
    member = provision_member(
        by=admin.user,
        actor_membership=admin,
        data={
            **member_data(),
            "company_id": other_company.pk,
            "company": other_company.pk,
            "is_superuser": True,
            "is_staff": True,
        },
    )
    assert member.company_id == company.pk
    assert not member.user.is_superuser and not member.user.is_staff


def test_one_global_login_may_have_different_company_roles(tenant, other_tenant):
    company, admin = tenant
    other_company, other_admin = other_tenant
    first = provision_member(by=admin.user, actor_membership=admin, data=member_data())
    second = provision_member(
        by=other_admin.user,
        actor_membership=other_admin,
        data={
            **member_data(email="MEMBER@EXAMPLE.COM", role=Membership.Role.QUALITY),
            "full_name": "Do not overwrite the global profile",
            "password": "A-valid-password-284!",
        },
    )
    first.user.refresh_from_db()
    assert first.user_id == second.user_id
    assert first.role == Membership.Role.ORIGINATOR
    assert second.role == Membership.Role.QUALITY
    assert first.user.full_name == "New Person"
    assert not first.user.has_usable_password()
    assert (
        not AuditEntry.objects.for_company(other_company)
        .filter(object_type="user", object_id=first.user_id)
        .exists()
    )
    assert Membership.objects.for_company(company).filter(user=first.user).count() == 1


def test_repeat_provisioning_does_not_duplicate_or_change_role(tenant):
    company, admin = tenant
    first = provision_member(by=admin.user, actor_membership=admin, data=member_data())
    count = AuditEntry.objects.for_company(company).count()
    second = provision_member(by=admin.user, actor_membership=admin, data=member_data())
    assert first.pk == second.pk
    assert AuditEntry.objects.for_company(company).count() == count
    with pytest.raises(ValidationError, match="membership-change"):
        provision_member(
            by=admin.user, actor_membership=admin, data=member_data(role=Membership.Role.QUALITY)
        )


def test_cannot_use_another_users_admin_membership(tenant, other_tenant):
    _, admin = tenant
    _, other_admin = other_tenant
    with pytest.raises(PermissionDenied):
        provision_member(by=admin.user, actor_membership=other_admin, data=member_data())


@pytest.mark.parametrize(
    "role", [Membership.Role.ORIGINATOR, Membership.Role.MANAGER, Membership.Role.QUALITY]
)
def test_non_admin_roles_cannot_provision(tenant, role):
    _, admin = tenant
    actor = provision_member(
        by=admin.user,
        actor_membership=admin,
        data=member_data(email="actor@example.com", role=role),
    )
    with pytest.raises(PermissionDenied):
        provision_member(by=actor.user, actor_membership=actor, data=member_data())


@pytest.mark.parametrize("revoked", ["role", "membership", "user", "company"])
def test_stale_actor_context_does_not_bypass_revocation(tenant, revoked):
    company, admin = tenant
    if revoked == "role":
        Membership.objects.for_company(company).filter(pk=admin.pk).update(role="originator")
    elif revoked == "membership":
        Membership.objects.for_company(company).filter(pk=admin.pk).update(active=False)
    elif revoked == "user":
        get_user_model().objects.filter(pk=admin.user_id).update(is_active=False)
    else:
        Company.objects.filter(pk=company.pk).update(active=False)
    with pytest.raises(PermissionDenied):
        provision_member(by=admin.user, actor_membership=admin, data=member_data())


def test_audit_failure_rolls_back_new_user_and_membership(tenant):
    company, admin = tenant
    with patch("apps.tenants.services._created", side_effect=RuntimeError("audit failed")):
        with pytest.raises(RuntimeError):
            provision_member(by=admin.user, actor_membership=admin, data=member_data())
    assert not get_user_model().objects.filter(email="member@example.com").exists()
    assert Membership.objects.for_company(company).count() == 1


def test_company_provisioning_rolls_back_if_audit_fails(developer):
    with patch("apps.tenants.services._created", side_effect=RuntimeError("audit failed")):
        with pytest.raises(RuntimeError):
            provision_company(
                by=developer,
                company_data={
                    "name": "Factory",
                    "timezone": "Africa/Johannesburg",
                    "escalation_tier_one_days": 3,
                    "escalation_tier_two_days": 7,
                },
                admin_data={"email": "admin@example.com"},
            )
    assert not Company.objects.exists()
    assert not get_user_model().objects.filter(email="admin@example.com").exists()


def test_role_and_deactivation_changes_are_audited(tenant):
    company, admin = tenant
    member = provision_member(by=admin.user, actor_membership=admin, data=member_data())
    update_membership(
        by=admin.user,
        actor_membership=admin,
        membership_id=member.pk,
        data={"role": "manager", "active": False},
    )
    member.refresh_from_db()
    assert member.role == Membership.Role.MANAGER
    assert not member.active
    entries = AuditEntry.objects.for_company(company).filter(
        object_type="membership", object_id=member.pk, action="field_changed"
    )
    assert {entry.field_name: json.loads(entry.new_value) for entry in entries} == {
        "role": "manager",
        "active": False,
    }


def test_cannot_change_another_company_membership(tenant, other_tenant):
    _, admin = tenant
    _, other_admin = other_tenant
    with pytest.raises(Membership.DoesNotExist):
        update_membership(
            by=admin.user,
            actor_membership=admin,
            membership_id=other_admin.pk,
            data={"role": "originator", "active": False},
        )
    other_admin.refresh_from_db()
    assert other_admin.active and other_admin.role == Membership.Role.COMPANY_ADMIN


def test_weak_password_is_refused_without_creating_an_account(tenant):
    company, admin = tenant
    with pytest.raises(ValidationError):
        provision_member(
            by=admin.user, actor_membership=admin, data={**member_data(), "password": "123"}
        )
    assert Membership.objects.for_company(company).count() == 1
    assert not get_user_model().objects.filter(email="member@example.com").exists()


def test_created_audit_snapshots_do_not_contain_passwords(tenant):
    company, admin = tenant
    member = provision_member(
        by=admin.user,
        actor_membership=admin,
        data={**member_data(), "password": "An-example-password-382!"},
    )
    text = " ".join(AuditEntry.objects.for_company(company).values_list("new_value", flat=True))
    assert "An-example-password-382!" not in text
    assert member.user.password not in text


def test_database_enforces_one_membership_per_company_and_valid_role(tenant):
    company, admin = tenant
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.for_company(company).create(
            company=company, user=admin.user, role="quality"
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.for_company(company).filter(pk=admin.pk).update(role="unknown")


def test_database_prevents_membership_reassignment(tenant, other_tenant):
    company, admin = tenant
    other_company, other_admin = other_tenant
    for changes in ({"company_id": other_company.pk}, {"user_id": other_admin.user_id}):
        with pytest.raises(IntegrityError), transaction.atomic():
            Membership.objects.for_company(company).filter(pk=admin.pk).update(**changes)


def test_audit_cannot_be_changed_through_model_or_queryset(tenant):
    company, _ = tenant
    entry = AuditEntry.objects.for_company(company).first()
    entry.comment = "rewrite"
    with pytest.raises(ValidationError):
        entry.save()
    with pytest.raises(ValidationError):
        AuditEntry.objects.for_company(company).filter(pk=entry.pk).update(comment="rewrite")
    with pytest.raises(ValidationError):
        entry.delete()
    with pytest.raises(ValidationError):
        AuditEntry.objects.for_company(company).delete()


@pytest.mark.parametrize(
    "operation",
    ["UPDATE tenants_auditentry SET comment = 'rewrite'", "DELETE FROM tenants_auditentry"],
)
def test_database_prevents_raw_audit_modification(tenant, operation):
    company, _ = tenant
    entry = AuditEntry.objects.for_company(company).first()
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(operation + " WHERE id = %s", [entry.pk])
    assert AuditEntry.objects.for_company(company).filter(pk=entry.pk).exists()


def test_audit_actor_cannot_come_from_another_company(tenant, other_tenant):
    company, _ = tenant
    _, other_admin = other_tenant
    with pytest.raises(IntegrityError), transaction.atomic():
        AuditEntry.objects.for_company(company).create(
            company=company,
            actor_user=other_admin.user,
            object_type="company",
            object_id=company.pk,
            action="created",
        )


@pytest.mark.parametrize("table", ["tenants_company", "tenants_membership"])
def test_database_prevents_business_row_deletion(tenant, table):
    company, admin = tenant
    pk = company.pk if table == "tenants_company" else admin.pk
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f'DELETE FROM "{table}" WHERE id = %s', [pk])


def test_membership_change_rolls_back_when_audit_fails(tenant):
    _, admin = tenant
    member = provision_member(by=admin.user, actor_membership=admin, data=member_data())
    with patch.object(AuditEntry.objects, "for_company") as audit_scope:
        audit_scope.return_value.create.side_effect = RuntimeError("audit failed")
        with pytest.raises(RuntimeError):
            update_membership(
                by=admin.user,
                actor_membership=admin,
                membership_id=member.pk,
                data={"role": "manager", "active": False},
            )
    member.refresh_from_db()
    assert member.role == Membership.Role.ORIGINATOR
    assert member.active


@pytest.mark.parametrize(
    "field,value",
    [
        ("escalation_tier_one_days", 0),
        ("escalation_tier_two_days", 3),
    ],
)
def test_database_enforces_escalation_thresholds(tenant, field, value):
    company, _ = tenant
    with pytest.raises(IntegrityError), transaction.atomic():
        Company.objects.filter(pk=company.pk).update(**{field: value})


def test_inactive_global_account_cannot_be_added(tenant):
    company, admin = tenant
    user = get_user_model().objects.create_user("inactive@example.com", is_active=False)
    with pytest.raises(ValidationError, match="cannot be provisioned"):
        provision_member(by=admin.user, actor_membership=admin, data=member_data(email=user.email))
    assert not Membership.objects.for_company(company).filter(user=user).exists()


def test_scope_requires_a_saved_company():
    with pytest.raises(ValueError, match="saved company"):
        Membership.objects.for_company(Company(name="Unsaved"))


@pytest.mark.parametrize("field,value", [("object_type", "unknown"), ("action", "unknown")])
def test_database_rejects_invalid_audit_choices(tenant, field, value):
    company, admin = tenant
    values = {"object_type": "company", "action": "created", field: value}
    with pytest.raises(IntegrityError), transaction.atomic():
        AuditEntry.objects.for_company(company).create(
            company=company, actor_user=admin.user, object_id=company.pk, **values
        )


def test_missing_actor_context_is_denied(tenant):
    _, admin = tenant
    with pytest.raises(PermissionDenied):
        provision_member(by=admin.user, actor_membership=None, data=member_data())

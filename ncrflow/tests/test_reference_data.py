import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.urls import reverse

from apps.ncr.models import Classification
from apps.tenants.models import AuditEntry, Company, Discipline, Membership, Region
from apps.tenants.reference_data import save_reference, set_reference_active

pytestmark = pytest.mark.django_db


@pytest.fixture(
    params=[
        ("regions", Region, "region"),
        ("disciplines", Discipline, "discipline"),
        ("classifications", Classification, "classification"),
    ]
)
def reference_case(request):
    return request.param


@pytest.fixture
def quality():
    user = get_user_model().objects.create_user("quality@example.com", "test-password")
    company = Company.objects.create(name="Factory A")
    membership = Membership.objects.for_company(company).create(
        company=company, user=user, role=Membership.Role.QUALITY
    )
    return user, company, membership


@pytest.fixture
def other_quality():
    user = get_user_model().objects.create_user("other@example.com", "test-password")
    company = Company.objects.create(name="Private Factory B")
    membership = Membership.objects.for_company(company).create(
        company=company, user=user, role=Membership.Role.QUALITY
    )
    return user, company, membership


def values(kind, name="Example"):
    data = {"name": name, "active": True}
    if kind == "classifications":
        data["sla_days"] = 3
    return data


def create(quality, kind, data=None):
    user, _, member = quality
    return save_reference(
        by=user, actor_membership=member, kind=kind, data=data if data is not None else values(kind)
    )


def test_create_records_actor_and_safe_snapshot(quality, reference_case):
    kind, model, object_type = reference_case
    user, company, _ = quality
    record = create(quality, kind)
    assert record.company_id == company.pk
    assert record.active
    assert record.created_at is not None and record.updated_at is not None
    assert model.objects.for_company(company).get(pk=record.pk) == record
    entry = AuditEntry.objects.for_company(company).get(
        object_type=object_type, object_id=record.pk
    )
    assert entry.actor_user_id == user.pk
    assert entry.action == "created"
    assert json.loads(entry.new_value) == values(kind)


def test_names_are_case_insensitively_unique_per_company(quality, other_quality, reference_case):
    kind, model, _ = reference_case
    create(quality, kind)
    with pytest.raises(ValidationError):
        create(quality, kind, values(kind, "  EXAMPLE  "))
    other = create(other_quality, kind, values(kind, "EXAMPLE"))
    assert model.objects.for_company(other_quality[1]).get(pk=other.pk).name == "EXAMPLE"


def test_database_rejects_case_duplicate_even_without_the_service(quality, reference_case):
    kind, model, _ = reference_case
    create(quality, kind)
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.for_company(quality[1]).create(company=quality[1], **values(kind, "EXAMPLE"))


def test_rename_and_status_edits_have_field_audits(quality, reference_case):
    kind, _, object_type = reference_case
    record = create(quality, kind)
    user, company, member = quality
    changed = {**values(kind, "Renamed"), "active": False}
    save_reference(
        by=user, actor_membership=member, kind=kind, reference_id=record.pk, data=changed
    )
    record.refresh_from_db()
    assert record.name == "Renamed" and not record.active
    changes = AuditEntry.objects.for_company(company).filter(
        object_type=object_type, object_id=record.pk, action="field_changed"
    )
    assert {entry.field_name: json.loads(entry.new_value) for entry in changes} == {
        "name": "Renamed",
        "active": False,
    }


def test_deactivate_reactivate_retains_record_and_history(quality, reference_case):
    kind, model, object_type = reference_case
    record = create(quality, kind)
    for active in (False, True):
        set_reference_active(
            by=quality[0],
            actor_membership=quality[2],
            kind=kind,
            reference_id=record.pk,
            data={"active": active},
        )
        record.refresh_from_db()
        assert record.active is active
    assert model.objects.for_company(quality[1]).count() == 1
    assert (
        AuditEntry.objects.for_company(quality[1])
        .filter(object_type=object_type, object_id=record.pk, field_name="active")
        .count()
        == 2
    )


def test_identical_save_or_status_does_not_create_false_audit(quality, reference_case):
    kind, _, _ = reference_case
    record = create(quality, kind)
    count = AuditEntry.objects.for_company(quality[1]).count()
    save_reference(
        by=quality[0],
        actor_membership=quality[2],
        kind=kind,
        reference_id=record.pk,
        data=values(kind),
    )
    set_reference_active(
        by=quality[0],
        actor_membership=quality[2],
        kind=kind,
        reference_id=record.pk,
        data={"active": True},
    )
    assert AuditEntry.objects.for_company(quality[1]).count() == count


def test_audit_failure_rolls_back_creation(quality, reference_case):
    kind, model, _ = reference_case
    with patch(
        "apps.tenants.reference_data._record_changes", side_effect=RuntimeError("audit failed")
    ):
        with pytest.raises(RuntimeError):
            create(quality, kind)
    assert not model.objects.for_company(quality[1]).exists()


def test_audit_failure_rolls_back_edit(quality, reference_case):
    kind, _, _ = reference_case
    record = create(quality, kind)
    with patch(
        "apps.tenants.reference_data._record_changes", side_effect=RuntimeError("audit failed")
    ):
        with pytest.raises(RuntimeError):
            save_reference(
                by=quality[0],
                actor_membership=quality[2],
                kind=kind,
                reference_id=record.pk,
                data=values(kind, "Must roll back"),
            )
    record.refresh_from_db()
    assert record.name == "Example"


def test_audit_failure_rolls_back_deactivation(quality, reference_case):
    kind, _, _ = reference_case
    record = create(quality, kind)
    with patch(
        "apps.tenants.reference_data._record_changes", side_effect=RuntimeError("audit failed")
    ):
        with pytest.raises(RuntimeError):
            set_reference_active(
                by=quality[0],
                actor_membership=quality[2],
                kind=kind,
                reference_id=record.pk,
                data={"active": False},
            )
    record.refresh_from_db()
    assert record.active


def test_cannot_update_another_company_reference(quality, other_quality, reference_case):
    kind, model, _ = reference_case
    foreign = create(other_quality, kind)
    with pytest.raises(model.DoesNotExist):
        save_reference(
            by=quality[0],
            actor_membership=quality[2],
            kind=kind,
            reference_id=foreign.pk,
            data=values(kind, "Forged edit"),
        )
    with pytest.raises(model.DoesNotExist):
        set_reference_active(
            by=quality[0],
            actor_membership=quality[2],
            kind=kind,
            reference_id=foreign.pk,
            data={"active": False},
        )
    foreign.refresh_from_db()
    assert foreign.name == "Example" and foreign.active


def test_submitted_company_and_ids_are_not_editable(quality, other_quality, reference_case):
    kind, _, _ = reference_case
    record = create(
        quality,
        kind,
        {
            **values(kind),
            "company": other_quality[1].pk,
            "company_id": other_quality[1].pk,
            "id": 999999,
            "created_at": "2000-01-01",
            "is_superuser": True,
        },
    )
    assert record.company_id == quality[1].pk
    assert record.pk != 999999


@pytest.mark.parametrize("role", ["originator", "manager", "company_admin"])
def test_non_quality_role_cannot_write(quality, reference_case, role):
    kind, model, _ = reference_case
    Membership.objects.for_company(quality[1]).filter(pk=quality[2].pk).update(role=role)
    with pytest.raises(PermissionDenied):
        create(quality, kind)
    assert not model.objects.for_company(quality[1]).exists()


@pytest.mark.parametrize("revoked", ["user", "membership", "company"])
def test_stale_actor_cannot_write_after_revocation(quality, reference_case, revoked):
    kind, _, _ = reference_case
    if revoked == "user":
        get_user_model().objects.filter(pk=quality[0].pk).update(is_active=False)
    elif revoked == "company":
        Company.objects.filter(pk=quality[1].pk).update(active=False)
    else:
        Membership.objects.for_company(quality[1]).filter(pk=quality[2].pk).update(active=False)
    with pytest.raises(PermissionDenied):
        create(quality, kind)


def test_database_prevents_company_reassignment_and_deletion(
    quality, other_quality, reference_case
):
    kind, model, _ = reference_case
    record = create(quality, kind)
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.for_company(quality[1]).filter(pk=record.pk).update(company=other_quality[1])
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f'DELETE FROM "{model._meta.db_table}" WHERE id = %s', [record.pk])
    with pytest.raises(ValidationError):
        record.delete()
    assert model.objects.for_company(quality[1]).filter(pk=record.pk).exists()


def test_quality_can_use_create_list_and_edit_screens(client, quality, reference_case):
    kind, model, _ = reference_case
    client.force_login(quality[0])
    create_url = reverse("tenants:reference-create", kwargs={"kind": kind})
    assert client.get(create_url).status_code == 200
    response = client.post(create_url, values(kind))
    assert response.url == reverse("tenants:reference-list", kwargs={"kind": kind})
    record = model.objects.for_company(quality[1]).get()
    listing = client.get(response.url)
    assert b"Example" in listing.content
    assert "no-store" in listing["Cache-Control"]
    edit_url = reverse("tenants:reference-edit", kwargs={"kind": kind, "reference_id": record.pk})
    assert client.get(edit_url).status_code == 200
    assert client.post(edit_url, values(kind, "Edited through form")).status_code == 302
    record.refresh_from_db()
    assert record.name == "Edited through form"


def test_list_never_shows_other_company_records(client, quality, other_quality, reference_case):
    kind, _, _ = reference_case
    create(quality, kind, values(kind, "Visible entry"))
    create(other_quality, kind, values(kind, "Secret entry from another company"))
    client.force_login(quality[0])
    response = client.get(
        reverse("tenants:reference-list", kwargs={"kind": kind}),
        {
            "company_id": other_quality[1].pk,
        },
    )
    assert b"Visible entry" in response.content
    assert b"Secret entry" not in response.content


def test_guessed_edit_and_status_urls_are_not_found(client, quality, other_quality, reference_case):
    kind, _, _ = reference_case
    record = create(other_quality, kind)
    client.force_login(quality[0])
    kwargs = {"kind": kind, "reference_id": record.pk}
    url = reverse("tenants:reference-edit", kwargs=kwargs)
    assert client.get(url).status_code == 404
    assert client.post(url, values(kind, "Forged edit")).status_code == 404
    status_url = reverse("tenants:reference-status", kwargs=kwargs)
    assert client.post(status_url, {"active": False}).status_code == 404


def test_duplicate_shows_form_error_instead_of_crashing(client, quality, reference_case):
    kind, model, _ = reference_case
    create(quality, kind)
    client.force_login(quality[0])
    response = client.post(
        reverse("tenants:reference-create", kwargs={"kind": kind}), values(kind, "EXAMPLE")
    )
    assert response.status_code == 200
    assert response.context["form"].errors
    assert model.objects.for_company(quality[1]).count() == 1


def test_status_is_post_only_and_audited(client, quality, reference_case):
    kind, _, object_type = reference_case
    record = create(quality, kind)
    client.force_login(quality[0])
    url = reverse("tenants:reference-status", kwargs={"kind": kind, "reference_id": record.pk})
    assert client.get(url).status_code == 405
    assert client.post(url, {"active": False}).status_code == 302
    record.refresh_from_db()
    assert not record.active
    listing = client.get(reverse("tenants:reference-list", kwargs={"kind": kind}))
    assert b"Example" in listing.content and b"Inactive" in listing.content
    assert client.post(url, {"active": True}).status_code == 302
    record.refresh_from_db()
    assert record.active
    assert (
        AuditEntry.objects.for_company(quality[1])
        .filter(object_type=object_type, object_id=record.pk, field_name="active")
        .exists()
    )


@pytest.mark.parametrize("role", ["originator", "manager", "company_admin"])
def test_non_quality_cannot_use_management_urls(client, quality, reference_case, role):
    kind, _, _ = reference_case
    record = create(quality, kind)
    Membership.objects.for_company(quality[1]).filter(pk=quality[2].pk).update(role=role)
    client.force_login(quality[0])
    urls = [
        reverse("tenants:reference-overview"),
        reverse("tenants:reference-list", kwargs={"kind": kind}),
        reverse("tenants:reference-create", kwargs={"kind": kind}),
        reverse("tenants:reference-edit", kwargs={"kind": kind, "reference_id": record.pk}),
    ]
    for url in urls:
        assert client.get(url).status_code == 403
    assert client.post(urls[2], values(kind, "Forbidden")).status_code == 403
    assert (
        client.post(
            reverse(
                "tenants:reference-status",
                kwargs={
                    "kind": kind,
                    "reference_id": record.pk,
                },
            ),
            {"active": False},
        ).status_code
        == 403
    )


def test_management_posts_require_csrf(quality):
    client = Client(enforce_csrf_checks=True)
    client.force_login(quality[0])
    url = reverse("tenants:reference-create", kwargs={"kind": "regions"})
    assert client.post(url, values("regions")).status_code == 403
    client.get(url)
    token = client.cookies["csrftoken"].value
    assert client.post(url, {**values("regions"), "csrfmiddlewaretoken": token}).status_code == 302
    record = Region.objects.for_company(quality[1]).get()
    status_url = reverse(
        "tenants:reference-status",
        kwargs={
            "kind": "regions",
            "reference_id": record.pk,
        },
    )
    assert client.post(status_url, {"active": False}).status_code == 403
    assert (
        client.post(status_url, {"active": False, "csrfmiddlewaretoken": token}).status_code == 302
    )


def test_reference_name_is_escaped(client, quality):
    create(quality, "regions", values("regions", "<script>alert(1)</script>"))
    client.force_login(quality[0])
    response = client.get(reverse("tenants:reference-list", kwargs={"kind": "regions"}))
    assert b"<script>alert(1)</script>" not in response.content
    assert b"&lt;script&gt;" in response.content


def test_anonymous_and_platform_only_accounts_cannot_manage_references(client):
    url = reverse("tenants:reference-overview")
    assert client.get(url).url.startswith(reverse("accounts:login"))
    developer = get_user_model().objects.create_superuser("developer@example.com", "test-password")
    client.force_login(developer)
    assert client.get(url).url == reverse("tenants:no-access")


def test_classification_sla_can_be_zero_but_never_negative(quality):
    zero = create(quality, "classifications", {**values("classifications"), "sla_days": 0})
    assert zero.sla_days == 0
    with pytest.raises(ValidationError):
        create(quality, "classifications", {**values("classifications", "Bad"), "sla_days": -1})
    with pytest.raises(IntegrityError), transaction.atomic():
        Classification.objects.for_company(quality[1]).filter(pk=zero.pk).update(sla_days=-1)


def test_classification_sla_changes_are_audited(quality):
    reference = create(quality, "classifications")
    save_reference(
        by=quality[0],
        actor_membership=quality[2],
        kind="classifications",
        reference_id=reference.pk,
        data={**values("classifications"), "sla_days": 8},
    )
    entry = AuditEntry.objects.for_company(quality[1]).get(
        object_type="classification", object_id=reference.pk, field_name="sla_days"
    )
    assert json.loads(entry.old_value) == 3 and json.loads(entry.new_value) == 8


def test_unknown_reference_type_is_not_a_dynamic_model_lookup(client, quality):
    client.force_login(quality[0])
    assert client.get("/reference/users/").status_code == 404

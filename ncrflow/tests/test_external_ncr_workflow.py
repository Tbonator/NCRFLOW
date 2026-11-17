"""Approved N2/N8 and Q38 behaviour for an external NCR before the response loop."""

import json
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from apps.ncr.forms import ExternalNCRCreateForm
from apps.ncr.models import NCR
from apps.ncr.services import (
    create_external_ncr,
    create_internal_ncr,
    editable_ncr_fields_for,
    ncr_actions_for,
    update_ncr,
)
from apps.suppliers.models import Supplier, SupplierContact
from apps.tenants.models import AuditEntry, NumberSequence
from tests.test_ncr_creation import data, setup_company
from tests.test_ncr_editing import payload
from tests.test_ncr_transitions import set_state
from tests.test_ncr_transitions import world as world

pytestmark = pytest.mark.django_db


@pytest.fixture
def supplier(world):
    company = world[0]["company"]
    return Supplier.objects.for_company(company).create(company=company, name="Chosen Supplier")


def create_external(world, supplier, key="originator", **extra):
    setup, people, _ = world
    user, member = people[key]
    return create_external_ncr(
        by=user, actor_membership=member, data=data(setup, supplier=supplier.pk, **extra)
    )


@pytest.mark.parametrize("key", ["originator", "manager", "quality", "admin", "other"])
@freeze_time("2026-11-14 22:30:00")
def test_every_active_internal_role_can_raise_external_with_audited_sla_and_number(
    world, supplier, key, mailoutbox
):
    setup, people, _ = world
    report = create_external(world, supplier, key)
    assert report.direction == "external" and report.supplier_id == supplier.pk
    assert report.status == "open" and report.number == "NCR-00002"
    assert report.originator_id == people[key][0].pk
    assert str(report.due_date) == "2026-11-18"  # Local Nov15 +3 calendar days.
    assert report.sent_to_supplier_at is None
    entry = AuditEntry.objects.for_company(setup["company"]).get(ncr=report, action="created")
    snapshot = json.loads(entry.new_value)
    assert snapshot["supplier_id"] == supplier.pk and snapshot["direction"] == "external"
    assert len(mailoutbox) == 0


@pytest.mark.parametrize("case", ["missing", "inactive", "foreign"])
def test_external_supplier_must_be_present_active_and_same_company_without_consuming_number(
    world, supplier, case
):
    setup, people, _ = world
    chosen = supplier.pk
    if case == "missing":
        chosen = ""
    elif case == "inactive":
        Supplier.objects.for_company(setup["company"]).filter(pk=supplier.pk).update(active=False)
    else:
        other = setup_company("foreign-creation")
        chosen = (
            Supplier.objects.for_company(other["company"])
            .create(company=other["company"], name="Foreign supplier")
            .pk
        )
    with pytest.raises(ValidationError):
        create_external_ncr(
            by=people["originator"][0],
            actor_membership=people["originator"][1],
            data=data(setup, supplier=chosen),
        )
    assert (
        NumberSequence.objects.for_company(setup["company"]).get(sequence_name="ncr").last_number
        == 1
    )
    assert NCR.objects.for_company(setup["company"]).count() == 1


def test_direction_is_fixed_by_service_and_internal_endpoint_ignores_supplier(world, supplier):
    setup, people, _ = world
    report = create_external(
        world,
        supplier,
        direction="internal",
        status="closed",
        company=99999,
        originator=99999,
        number="FORGED",
        supplier_id=99999,
        due_date="2099-01-01",
    )
    assert (
        report.direction == "external" and report.status == "open" and report.number == "NCR-00002"
    )
    assert (
        report.company_id == setup["company"].pk
        and report.originator_id == people["originator"][0].pk
    )
    internal = create_internal_ncr(
        by=people["originator"][0],
        actor_membership=people["originator"][1],
        data=data(setup, supplier=supplier.pk, direction="external"),
    )
    assert internal.direction == "internal" and internal.supplier_id is None
    assert internal.number == "NCR-00003"


def test_external_creation_rollback_keeps_number_and_report_atomic(world, supplier):
    setup, _, _ = world
    with patch.object(AuditEntry, "save", side_effect=RuntimeError("Audit unavailable")):
        with pytest.raises(RuntimeError, match="Audit unavailable"):
            create_external(world, supplier)
    assert (
        NumberSequence.objects.for_company(setup["company"]).get(sequence_name="ncr").last_number
        == 1
    )
    assert NCR.objects.for_company(setup["company"]).count() == 1


def test_supplier_is_rechecked_under_lock_after_form_validation(world, supplier):
    original = ExternalNCRCreateForm.is_valid

    def deactivate(form):
        valid = original(form)
        Supplier.objects.for_company(world[0]["company"]).filter(pk=supplier.pk).update(
            active=False
        )
        return valid

    with patch.object(ExternalNCRCreateForm, "is_valid", deactivate):
        with pytest.raises(ValidationError):
            create_external(world, supplier)
    assert (
        NumberSequence.objects.for_company(world[0]["company"]).get(sequence_name="ncr").last_number
        == 1
    )


def test_external_creation_refuses_forged_membership(world, supplier):
    setup, people, _ = world
    other = setup_company("forged-external")
    with pytest.raises(PermissionDenied):
        create_external_ncr(
            by=people["originator"][0],
            actor_membership=other["member"],
            data=data(setup, supplier=supplier.pk),
        )


@pytest.mark.parametrize("key", ["originator", "manager", "quality", "admin"])
def test_supplier_picker_is_narrow_scoped_and_does_not_grant_directory_access(world, supplier, key):
    setup, people, _ = world
    SupplierContact.objects.for_company(setup["company"]).create(
        company=setup["company"],
        supplier=supplier,
        name="Private contact",
        email="private-contact@example.com",
    )
    inactive = Supplier.objects.for_company(setup["company"]).create(
        company=setup["company"], name="Inactive hidden supplier", active=False
    )
    other = setup_company("picker-other")
    foreign = Supplier.objects.for_company(other["company"]).create(
        company=other["company"], name="Foreign hidden supplier"
    )
    client = Client()
    client.force_login(people[key][0])
    response = client.get(reverse("ncr:create-external"))
    assert response.status_code == 200
    assert list(response.context["form"].fields["supplier"].queryset) == [supplier]
    page = response.content.decode()
    for hidden in (inactive.name, foreign.name, "Private contact", "private-contact@example.com"):
        assert hidden not in page
    assert client.get(reverse("suppliers:list")).status_code == (200 if key == "admin" else 403)


def test_external_http_creation_csrf_receipt_and_company_isolation(world, supplier):
    setup, people, _ = world
    client = Client(enforce_csrf_checks=True)
    client.force_login(people["originator"][0])
    url = reverse("ncr:create-external")
    assert client.post(url, data(setup, supplier=supplier.pk)).status_code == 403
    response = client.get(url)
    assert response.status_code == 200
    posted = data(
        setup, supplier=supplier.pk, csrfmiddlewaretoken=client.cookies["csrftoken"].value
    )
    response = client.post(url, posted)
    report = NCR.objects.for_company(setup["company"]).get(direction="external")
    assert response.status_code == 302
    receipt = client.get(response.url)
    assert b"External NCR" in receipt.content and supplier.name.encode() in receipt.content
    detail_url = reverse("ncr:detail", kwargs={"public_id": report.public_id})
    assert b"Not dispatched" in client.get(detail_url).content
    assert supplier.name.encode() in client.get(reverse("ncr:list")).content
    other = setup_company("http-foreign")
    foreign = Client()
    foreign.force_login(other["user"])
    assert foreign.get(detail_url).status_code == 404
    assert foreign.get(response.url).status_code == 404


def test_external_supplier_name_is_escaped_in_receipt_and_detail(world, supplier):
    Supplier.objects.for_company(world[0]["company"]).filter(pk=supplier.pk).update(
        name="<script>example</script>"
    )
    supplier.refresh_from_db()
    report = create_external(world, supplier)
    client = Client()
    client.force_login(world[1]["originator"][0])
    for route in ("ncr:created", "ncr:detail"):
        page = client.get(reverse(route, kwargs={"public_id": report.public_id})).content
        assert b"&lt;script&gt;example&lt;/script&gt;" in page
        assert b"<script>example</script>" not in page


@pytest.mark.parametrize("key", ["originator", "manager", "quality", "admin", "other"])
def test_external_read_and_edit_http_scope_matches_existing_internal_user_roles(external, key):
    _, people, report = external
    client = Client()
    client.force_login(people[key][0])
    detail = client.get(reverse("ncr:detail", kwargs={"public_id": report.public_id}))
    assert detail.status_code == (404 if key == "other" else 200)
    receipt = client.get(reverse("ncr:created", kwargs={"public_id": report.public_id}))
    assert receipt.status_code == (200 if key == "originator" else 404)
    edit_page = client.get(reverse("ncr:edit", kwargs={"public_id": report.public_id}))
    expected = 404 if key == "other" else 403 if key == "admin" else 200
    assert edit_page.status_code == expected
    if expected == 200:
        assert "supplier" not in edit_page.context["form"].fields
    if detail.status_code == 200:
        assert "close" not in detail.context["ncr_actions"]


def test_external_waiting_http_edit_form_has_only_manager_and_revision(external):
    _, people, report = external
    state(report, "awaiting_supplier", people["quality"][0])
    client = Client()
    client.force_login(people["quality"][0])
    page = client.get(reverse("ncr:edit", kwargs={"public_id": report.public_id}))
    assert page.status_code == 200
    assert set(page.context["form"].fields) == {"responsible_manager", "revision"}


def test_external_creation_http_requires_account_company_and_supported_method(world):
    url = reverse("ncr:create-external")
    client = Client()
    assert client.get(url).status_code == 302
    client.force_login(world[1]["originator"][0])
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)(url).status_code == 405


@pytest.fixture
def external(world):
    setup, people, internal = world
    company = setup["company"]
    supplier = Supplier.objects.for_company(company).create(
        company=company, name="Example Supplier"
    )
    report = NCR.objects.for_company(company).create(
        company=company,
        number="NCR-00002",
        sequence_number=2,
        subject="External issue",
        description="External workflow fixture",
        direction="external",
        supplier=supplier,
        classification_id=internal.classification_id,
        region_id=internal.region_id,
        discipline_id=internal.discipline_id,
        originator_id=internal.originator_id,
        responsible_manager_id=internal.responsible_manager_id,
        due_date=internal.due_date,
    )
    return setup, people, report


def state(report, status, actor):
    if status in ("awaiting_supplier", "awaiting_internal_review"):
        NCR.objects.for_company(report.company_id).filter(pk=report.pk).update(
            sent_to_supplier_at=timezone.now()
        )
    set_state(report, status, actor)


@pytest.mark.parametrize("key", ["originator", "manager", "quality", "admin", "other"])
def test_external_review_requires_assigned_manager_or_quality(external, key):
    _, people, report = external
    user, membership = people[key]
    if key in ("manager", "quality"):
        report.start_review(by=user, actor_membership=membership)
        report.refresh_from_db()
        assert report.status == "in_review" and report.review_started_at is not None
        entry = AuditEntry.objects.for_company(report.company_id).get(
            ncr=report, action="status_changed"
        )
        assert entry.old_value == "open" and entry.new_value == "in_review"
    else:
        with pytest.raises(PermissionDenied):
            report.start_review(by=user, actor_membership=membership)
        report.refresh_from_db()
        assert report.status == "open"


def test_external_cannot_use_internal_close_before_dispatch_or_response_acceptance(external):
    _, people, report = external
    user, membership = people["quality"]
    for status in ("open", "in_review", "awaiting_supplier", "awaiting_internal_review"):
        state(report, status, user)
        assert "close" not in ncr_actions_for(by=user, actor_membership=membership, report=report)
        with pytest.raises(PermissionDenied):
            report.close(by=user, actor_membership=membership)
        report.refresh_from_db()
        assert report.status == status and report.closed_at is None


@pytest.mark.parametrize(
    "status", ["open", "in_review", "awaiting_supplier", "awaiting_internal_review"]
)
def test_quality_can_cancel_external_from_each_nonterminal_status(external, status):
    _, people, report = external
    user, membership = people["quality"]
    state(report, status, user)
    report.cancel(by=user, actor_membership=membership, reason="Duplicate external NCR")
    report.refresh_from_db()
    assert report.status == "cancelled" and report.cancelled_by_id == user.pk
    entry = AuditEntry.objects.for_company(report.company_id).get(
        ncr=report, action="status_changed"
    )
    assert entry.old_value == status and entry.comment == "Duplicate external NCR"
    with pytest.raises(ValidationError):
        report.cancel(by=user, actor_membership=membership)


@pytest.mark.parametrize(
    "status", ["open", "in_review", "awaiting_supplier", "awaiting_internal_review"]
)
@pytest.mark.parametrize("key", ["originator", "manager", "admin", "other"])
def test_other_roles_cannot_cancel_external(external, status, key):
    _, people, report = external
    state(report, status, people["quality"][0])
    user, membership = people[key]
    with pytest.raises(PermissionDenied):
        report.cancel(by=user, actor_membership=membership)


@pytest.mark.parametrize("status", ["open", "in_review"])
def test_external_quality_edit_preserves_supplier_and_identity(external, status):
    _, people, report = external
    user, membership = people["quality"]
    state(report, status, user)
    supplier_id, due = report.supplier_id, report.due_date
    result = update_ncr(
        by=user,
        actor_membership=membership,
        report_id=report.pk,
        data=payload(
            report,
            subject="Updated external",
            supplier=999999,
            direction="internal",
            due_date="2099-01-01",
        ),
    )
    assert result.subject == "Updated external"
    assert (result.supplier_id, result.due_date, result.direction) == (supplier_id, due, "external")
    assert list(
        AuditEntry.objects.for_company(report.company_id)
        .filter(ncr=report, action="field_changed")
        .values_list("field_name", flat=True)
    ) == ["subject"]


@pytest.mark.parametrize("status", ["awaiting_supplier", "awaiting_internal_review"])
def test_waiting_external_allows_only_quality_manager_reassignment(external, status):
    _, people, report = external
    user, membership = people["quality"]
    state(report, status, user)
    before = report.subject, report.description, report.region_id, report.discipline_id
    assert editable_ncr_fields_for(by=user, actor_membership=membership, report=report) == (
        "responsible_manager",
    )
    result = update_ncr(
        by=user,
        actor_membership=membership,
        report_id=report.pk,
        data=payload(
            report,
            subject="Forbidden",
            description="Forbidden",
            region=999999,
            discipline=999999,
            responsible_manager=people["other"][0].pk,
        ),
    )
    assert (result.subject, result.description, result.region_id, result.discipline_id) == before
    assert result.responsible_manager_id == people["other"][0].pk
    assert list(
        AuditEntry.objects.for_company(report.company_id)
        .filter(ncr=report, action="field_changed")
        .values_list("field_name", flat=True)
    ) == ["responsible_manager_id"]
    for key in ("originator", "manager", "admin", "other"):
        actor, member = people[key]
        assert editable_ncr_fields_for(by=actor, actor_membership=member, report=result) == ()
        with pytest.raises(PermissionDenied):
            update_ncr(by=actor, actor_membership=member, report_id=result.pk, data=payload(result))

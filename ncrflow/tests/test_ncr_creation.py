import json
import uuid
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from apps.ncr.forms import NCRCreateForm
from apps.ncr.models import NCR, Classification
from apps.ncr.services import create_internal_ncr
from apps.tenants.models import AuditEntry, Company, Discipline, Membership, NumberSequence, Region

pytestmark = pytest.mark.django_db


def setup_company(suffix):
    company = Company.objects.create(name=f"Factory {suffix}")
    user = get_user_model().objects.create_user(f"originator-{suffix}@example.com", "test-password")
    member = Membership.objects.for_company(company).create(
        company=company, user=user, role="originator"
    )
    manager = get_user_model().objects.create_user(f"manager-{suffix}@example.com", "test-password")
    manager_member = Membership.objects.for_company(company).create(
        company=company, user=manager, role="manager"
    )
    classification = Classification.objects.for_company(company).create(
        company=company, name=f"Class {suffix}", sla_days=3
    )
    region = Region.objects.for_company(company).create(company=company, name=f"Region {suffix}")
    discipline = Discipline.objects.for_company(company).create(
        company=company, name=f"Discipline {suffix}"
    )
    return {
        "company": company,
        "user": user,
        "member": member,
        "manager": manager,
        "manager_member": manager_member,
        "classification": classification,
        "region": region,
        "discipline": discipline,
    }


@pytest.fixture
def setup():
    return setup_company("A")


@pytest.fixture
def other():
    return setup_company("B")


def data(setup, **extra):
    return {
        "subject": "Part outside tolerance",
        "description": "Measurement exceeds drawing limits.",
        "classification": setup["classification"].pk,
        "region": setup["region"].pk,
        "discipline": setup["discipline"].pk,
        "responsible_manager": setup["manager"].pk,
        "part_number": "PART-1",
        "job_number": "JOB-1",
        **extra,
    }


def create(setup, **extra):
    return create_internal_ncr(
        by=setup["user"], actor_membership=setup["member"], data=data(setup, **extra)
    )


@freeze_time("2026-11-13 10:00:00")
def test_creates_open_internal_report_with_audited_number_and_due_date(setup):
    report = create(setup)
    report.refresh_from_db()
    assert report.number == "NCR-00001" and report.sequence_number == 1
    assert report.status == NCR.Status.OPEN and report.direction == "internal"
    assert report.originator_id == setup["user"].pk
    assert report.responsible_manager_id == setup["manager"].pk
    assert report.due_date == date(2026, 11, 16)
    assert isinstance(report.public_id, uuid.UUID)
    assert report.created_at == timezone.now()
    counter = NumberSequence.objects.for_company(setup["company"]).get(sequence_name="ncr")
    assert counter.last_number == 1
    entry = AuditEntry.objects.for_company(setup["company"]).get(
        object_type="ncr", object_id=report.pk
    )
    assert entry.ncr_id == report.pk and entry.actor_user_id == setup["user"].pk
    snapshot = json.loads(entry.new_value)
    assert snapshot["number"] == report.number and snapshot["due_date"] == "2026-11-16"
    assert (
        AuditEntry.objects.for_company(setup["company"])
        .filter(object_type="number_sequence")
        .count()
        == 1
    )


def test_number_series_is_separate_per_company(setup, other):
    first, foreign, second = create(setup), create(other), create(setup)
    assert [first.number, foreign.number, second.number] == ["NCR-00001", "NCR-00001", "NCR-00002"]
    assert first.public_id != second.public_id


def test_number_padding_does_not_truncate_large_numbers(setup):
    create(setup)
    NumberSequence.objects.for_company(setup["company"]).filter(sequence_name="ncr").update(
        last_number=99999
    )
    assert create(setup).number == "NCR-100000"


@pytest.mark.parametrize(
    "zone,instant,expected",
    [
        ("Africa/Johannesburg", "2026-11-13 22:30:00", date(2026, 11, 17)),
        ("America/Los_Angeles", "2026-11-14 01:00:00", date(2026, 11, 16)),
    ],
)
def test_sla_uses_company_local_creation_date_and_calendar_days(setup, zone, instant, expected):
    Company.objects.filter(pk=setup["company"].pk).update(timezone=zone)
    with freeze_time(instant):
        report = create(setup)
    assert report.due_date == expected
    assert report.created_at.astimezone(
        timezone.get_fixed_timezone(0)
    ).date() == date.fromisoformat(instant[:10])


@freeze_time("2026-11-13 22:30:00")
def test_zero_day_sla_and_later_sla_edits_do_not_rewrite_due_date(setup):
    Classification.objects.for_company(setup["company"]).filter(
        pk=setup["classification"].pk
    ).update(sla_days=0)
    report = create(setup)
    assert report.due_date == date(2026, 11, 14)
    Classification.objects.for_company(setup["company"]).filter(
        pk=setup["classification"].pk
    ).update(sla_days=15)
    report.refresh_from_db()
    assert report.due_date == date(2026, 11, 14)


@pytest.mark.parametrize(
    "field",
    ["subject", "description", "classification", "region", "discipline", "responsible_manager"],
)
def test_required_fields_fail_before_consuming_a_number(setup, field):
    with pytest.raises(ValidationError):
        create(setup, **{field: ""})
    assert not NCR.objects.for_company(setup["company"]).exists()
    assert not NumberSequence.objects.for_company(setup["company"]).exists()


@pytest.mark.parametrize("field", ["classification", "region", "discipline", "responsible_manager"])
def test_foreign_reference_or_manager_cannot_be_chosen(setup, other, field):
    value = other["manager"].pk if field == "responsible_manager" else other[field].pk
    with pytest.raises(ValidationError):
        create(setup, **{field: value})
    assert not NumberSequence.objects.for_company(setup["company"]).exists()


@pytest.mark.parametrize("field", ["classification", "region", "discipline"])
def test_inactive_reference_is_refused(setup, field):
    instance = setup[field]
    type(instance).objects.for_company(setup["company"]).filter(pk=instance.pk).update(active=False)
    with pytest.raises(ValidationError):
        create(setup)


@pytest.mark.parametrize("change", ["inactive_user", "inactive_membership", "wrong_role"])
def test_responsible_manager_must_have_active_manager_membership(setup, change):
    if change == "inactive_user":
        get_user_model().objects.filter(pk=setup["manager"].pk).update(is_active=False)
    else:
        updates = {"active": False} if change == "inactive_membership" else {"role": "quality"}
        Membership.objects.for_company(setup["company"]).filter(
            pk=setup["manager_member"].pk
        ).update(**updates)
    with pytest.raises(ValidationError):
        create(setup)


@pytest.mark.parametrize("role", Membership.Role.values)
def test_each_active_internal_role_may_raise_an_ncr(setup, role):
    Membership.objects.for_company(setup["company"]).filter(pk=setup["member"].pk).update(role=role)
    assert create(setup).originator_id == setup["user"].pk


@pytest.mark.parametrize("change", ["company", "user", "membership"])
def test_inactive_creator_context_is_refused(setup, change):
    if change == "company":
        Company.objects.filter(pk=setup["company"].pk).update(active=False)
    elif change == "user":
        get_user_model().objects.filter(pk=setup["user"].pk).update(is_active=False)
    else:
        Membership.objects.for_company(setup["company"]).filter(pk=setup["member"].pk).update(
            active=False
        )
    with pytest.raises(PermissionDenied):
        create(setup)


def test_forged_membership_context_cannot_create_in_another_company(setup, other):
    with pytest.raises(PermissionDenied):
        create_internal_ncr(by=setup["user"], actor_membership=other["member"], data=data(other))


def test_server_owned_fields_cannot_be_supplied(setup, other):
    report = create(
        setup,
        company_id=other["company"].pk,
        company=other["company"].pk,
        originator=other["user"].pk,
        status="closed",
        direction="external",
        sequence_number=900,
        number="FORGED",
        due_date="2000-01-01",
        supplier=123,
    )
    assert report.company_id == setup["company"].pk
    assert report.originator_id == setup["user"].pk
    assert (
        report.number == "NCR-00001" and report.status == "open" and report.direction == "internal"
    )
    assert report.due_date != date(2000, 1, 1)


@pytest.mark.parametrize("existing", [False, True])
def test_ncr_audit_failure_rolls_back_the_report_and_number(setup, existing):
    from apps.ncr.services import _created

    if existing:
        create(setup)

    def fail_report(company, actor, obj, object_type, values, **kwargs):
        if object_type == "ncr":
            raise RuntimeError("audit failed")
        return _created(company, actor, obj, object_type, values, **kwargs)

    with patch("apps.ncr.services._created", side_effect=fail_report):
        with pytest.raises(RuntimeError):
            create(setup)
    assert NCR.objects.for_company(setup["company"]).count() == int(existing)
    if existing:
        assert NumberSequence.objects.for_company(setup["company"]).get().last_number == 1
    else:
        assert not NumberSequence.objects.for_company(setup["company"]).exists()
    assert create(setup).number == ("NCR-00002" if existing else "NCR-00001")


def raw_values(setup, **changes):
    return {
        "company": setup["company"],
        "originator": setup["user"],
        "responsible_manager": setup["manager"],
        "classification": setup["classification"],
        "region": setup["region"],
        "discipline": setup["discipline"],
        "subject": "Direct integrity probe",
        "description": "Database check",
        "direction": "internal",
        "sequence_number": 1,
        "number": "NCR-00001",
        "due_date": date(2026, 11, 16),
        **changes,
    }


@pytest.mark.parametrize(
    "field", ["classification", "region", "discipline", "originator", "responsible_manager"]
)
def test_database_enforces_reference_and_user_company_matches(setup, other, field):
    foreign = (
        other["user"]
        if field == "originator"
        else other["manager"]
        if field == "responsible_manager"
        else other[field]
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        NCR.objects.for_company(setup["company"]).create(**raw_values(setup, **{field: foreign}))


@pytest.mark.parametrize("field,status", [("closed_by", "closed"), ("cancelled_by", "cancelled")])
def test_database_enforces_terminal_actor_company(setup, other, field, status):
    stamp = "closed_at" if status == "closed" else "cancelled_at"
    with pytest.raises(IntegrityError), transaction.atomic():
        NCR.objects.for_company(setup["company"]).create(
            **raw_values(setup, **{field: other["user"], stamp: timezone.now(), "status": status})
        )


def test_database_enforces_same_company_ncr_audit_link(setup, other):
    report = create(setup)
    with pytest.raises(IntegrityError), transaction.atomic():
        AuditEntry.objects.for_company(other["company"]).create(
            company=other["company"],
            actor_user=other["user"],
            ncr=report,
            object_type="ncr",
            object_id=report.pk,
            action="created",
        )


@pytest.mark.parametrize(
    "field",
    [
        "due_date",
        "number",
        "public_id",
        "company_id",
        "originator_id",
        "classification_id",
        "created_at",
    ],
)
def test_database_keeps_report_identity_and_sla_basis_immutable(setup, other, field):
    report = create(setup)
    changed = {
        "due_date": report.due_date + timedelta(days=1),
        "number": "NCR-99999",
        "public_id": uuid.uuid4(),
        "company_id": other["company"].pk,
        "originator_id": other["user"].pk,
        "classification_id": other["classification"].pk,
        "created_at": report.created_at + timedelta(days=1),
    }[field]
    with pytest.raises(IntegrityError), transaction.atomic():
        NCR.objects.for_company(setup["company"]).filter(pk=report.pk).update(**{field: changed})


@pytest.mark.parametrize("status", ["closed", "cancelled", "invalid", "awaiting_supplier"])
def test_database_rejects_inconsistent_status_and_stamp_states(setup, status):
    report = create(setup)
    with pytest.raises(IntegrityError), transaction.atomic():
        NCR.objects.for_company(setup["company"]).filter(pk=report.pk).update(status=status)


@pytest.mark.parametrize("table", ["ncr_ncr", "tenants_numbersequence"])
def test_database_retains_reports_and_number_counters(setup, table):
    report = create(setup)
    pk = (
        report.pk
        if table == "ncr_ncr"
        else NumberSequence.objects.for_company(setup["company"]).get().pk
    )
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f'DELETE FROM "{table}" WHERE id = %s', [pk])


def test_counter_cannot_decrease_or_move_company(setup, other):
    create(setup)
    counter = NumberSequence.objects.for_company(setup["company"]).get()
    for changes in (
        {"last_number": 0},
        {"company_id": other["company"].pk},
        {"sequence_name": "supplier_ncr"},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            NumberSequence.objects.for_company(setup["company"]).filter(pk=counter.pk).update(
                **changes
            )


def test_number_unique_constraints_are_database_backstops(setup):
    create(setup)
    with pytest.raises(IntegrityError), transaction.atomic():
        NCR.objects.for_company(setup["company"]).create(**raw_values(setup))


def test_creation_and_due_date_keep_one_instant_when_insert_crosses_local_midnight(setup):
    before = timezone.datetime(
        2026, 11, 13, 21, 59, 59, 999999, tzinfo=timezone.get_fixed_timezone(0)
    )
    after = before + timedelta(microseconds=2)
    instants = iter([before])

    def clock():
        return next(instants, after)

    with patch("apps.ncr.services.timezone.now", side_effect=clock):
        report = create(setup)
    report.refresh_from_db()
    assert report.created_at == before
    assert report.due_date == date(2026, 11, 16)


def test_sla_date_overflow_is_a_form_error_and_consumes_no_number(setup):
    Classification.objects.for_company(setup["company"]).filter(
        pk=setup["classification"].pk
    ).update(sla_days=2**31 - 1)
    with pytest.raises(ValidationError, match="date range"):
        create(setup)
    assert not NumberSequence.objects.for_company(setup["company"]).exists()


def test_form_offers_only_current_company_active_references_and_managers(setup, other):
    form = NCRCreateForm(company=setup["company"])
    assert set(form.fields) == {
        "subject",
        "description",
        "classification",
        "region",
        "discipline",
        "responsible_manager",
        "part_number",
        "job_number",
    }
    for field in ("classification", "region", "discipline"):
        assert list(form.fields[field].queryset) == [setup[field]]
    assert list(form.fields["responsible_manager"].queryset) == [setup["manager"]]


def test_create_screen_redirects_to_owned_confirmation(client, setup):
    client.force_login(setup["user"])
    url = reverse("ncr:create")
    assert client.get(url).status_code == 200
    response = client.post(url, data(setup))
    report = NCR.objects.for_company(setup["company"]).get()
    assert response.url == reverse("ncr:created", kwargs={"public_id": report.public_id})
    confirmation = client.get(response.url)
    assert confirmation.status_code == 200
    assert (
        b"NCR-00001" in confirmation.content and b"Part outside tolerance" in confirmation.content
    )
    assert "no-store" in confirmation["Cache-Control"]


def test_confirmation_does_not_leak_to_other_originators_or_companies(client, setup, other):
    report = create(setup)
    url = reverse("ncr:created", kwargs={"public_id": report.public_id})
    client.force_login(other["user"])
    assert client.get(url).status_code == 404
    client.force_login(setup["manager"])
    assert client.get(url).status_code == 404


def test_invalid_foreign_selection_returns_form_error_without_number(client, setup, other):
    client.force_login(setup["user"])
    response = client.post(
        reverse("ncr:create"), data(setup, classification=other["classification"].pk)
    )
    assert response.status_code == 200 and response.context["form"].errors
    assert not NumberSequence.objects.for_company(setup["company"]).exists()


def test_creation_requires_csrf(setup):
    client = Client(enforce_csrf_checks=True)
    client.force_login(setup["user"])
    url = reverse("ncr:create")
    assert client.post(url, data(setup)).status_code == 403
    client.get(url)
    token = client.cookies["csrftoken"].value
    assert client.post(url, {**data(setup), "csrfmiddlewaretoken": token}).status_code == 302


def test_anonymous_and_unassigned_platform_accounts_cannot_create(client):
    assert client.get(reverse("ncr:create")).url.startswith(reverse("accounts:login"))
    developer = get_user_model().objects.create_superuser("developer@example.com", "test-password")
    client.force_login(developer)
    assert client.get(reverse("ncr:create")).url == reverse("tenants:no-access")


def test_confirmation_escapes_report_content(client, setup):
    report = create(
        setup, subject="<script>alert(1)</script>", description="<script>alert(2)</script>"
    )
    client.force_login(setup["user"])
    response = client.get(reverse("ncr:created", kwargs={"public_id": report.public_id}))
    assert b"<script>" not in response.content and b"&lt;script&gt;" in response.content

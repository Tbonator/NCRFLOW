"""Q38: current actor/state/field permissions, retained history and atomic edits."""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from apps.ncr.forms import DESCRIPTIVE_FIELDS, QUALITY_EDIT_FIELDS, NCREditForm
from apps.ncr.models import NCR
from apps.ncr.services import editable_ncr_fields_for, update_internal_ncr
from apps.tenants.models import AuditEntry, Discipline, Membership, Region
from tests.test_ncr_creation import create, setup_company
from tests.test_ncr_transitions import set_state
from tests.test_ncr_transitions import world as world

pytestmark = pytest.mark.django_db


def payload(report, **changes):
    return {
        "subject": report.subject,
        "description": report.description,
        "part_number": report.part_number,
        "job_number": report.job_number,
        "region": report.region_id,
        "discipline": report.discipline_id,
        "responsible_manager": report.responsible_manager_id,
        "revision": report.updated_at.isoformat(),
        **changes,
    }


def edit(people, key, report, **changes):
    user, membership = people[key]
    return update_internal_ncr(
        by=user, actor_membership=membership, report_id=report.pk, data=payload(report, **changes)
    )


def events(report):
    return AuditEntry.objects.for_company(report.company_id).filter(
        ncr=report, action="field_changed"
    )


@pytest.mark.parametrize("key", ["originator", "manager", "quality", "admin", "other"])
@pytest.mark.parametrize("status", ["open", "in_review", "closed", "cancelled"])
def test_descriptive_edit_role_and_state_matrix(world, key, status):
    _, people, report = world
    set_state(report, status, people["quality"][0])
    old_subject, old_description = report.subject, report.description
    allowed = status in ("open", "in_review") and (
        key in ("manager", "quality") or (key == "originator" and status == "open")
    )
    user, membership = people[key]
    fields = editable_ncr_fields_for(by=user, actor_membership=membership, report=report)
    assert bool(fields) == allowed
    if not allowed:
        error = ValidationError if status in ("closed", "cancelled") else PermissionDenied
        with pytest.raises(error):
            edit(people, key, report, subject="Changed subject", description="Changed description")
        assert not events(report).exists()
        return
    saved = edit(people, key, report, subject="Changed subject", description="Changed description")
    report.refresh_from_db()
    assert report.subject == saved.subject == "Changed subject"
    assert report.description == "Changed description" and report.status == status
    found = {entry.field_name: entry for entry in events(report)}
    assert set(found) == {"subject", "description"}
    for field, old in (("subject", old_subject), ("description", old_description)):
        entry = found[field]
        assert json.loads(entry.old_value) == old
        assert json.loads(entry.new_value) == getattr(report, field)
        assert entry.actor_user_id == user.pk and entry.object_id == report.pk


@pytest.mark.parametrize("role", ["company_admin", "manager", "quality"])
def test_creator_identity_grants_open_descriptive_edit_regardless_of_role(role):
    setup = setup_company("creator-edit")
    Membership.objects.for_company(setup["company"]).filter(pk=setup["member"].pk).update(role=role)
    report = create(setup)
    user, member = setup["user"], setup["member"]
    result = update_internal_ncr(
        by=user,
        actor_membership=member,
        report_id=report.pk,
        data=payload(report, part_number="CREATOR-EDIT"),
    )
    assert result.part_number == "CREATOR-EDIT"


@pytest.mark.parametrize("key", ["originator", "manager", "quality"])
def test_forged_immutable_and_status_fields_are_never_applied(world, key):
    _, people, report = world
    immutable = {
        field: getattr(report, field)
        for field in (
            "classification_id",
            "originator_id",
            "company_id",
            "public_id",
            "number",
            "due_date",
            "created_at",
            "sequence_number",
            "status",
            "closed_at",
            "closed_by_id",
            "direction",
        )
    }
    edit(
        people,
        key,
        report,
        subject="Allowed edit",
        classification=99999,
        originator=99999,
        company=99999,
        public_id="invalid",
        number="NCR-99999",
        due_date="2099-01-01",
        created_at="2099-01-01",
        sequence_number=99999,
        status="closed",
        closed_at=timezone.now(),
        closed_by=people["quality"][0].pk,
        direction="external",
    )
    report.refresh_from_db()
    assert {field: getattr(report, field) for field in immutable} == immutable
    assert list(events(report).values_list("field_name", flat=True)) == ["subject"]


@pytest.mark.parametrize("key", ["originator", "manager"])
def test_non_quality_cannot_change_routing_or_assignment(world, key):
    setup, people, report = world
    region = Region.objects.for_company(setup["company"]).create(
        company=setup["company"], name="Other"
    )
    before = report.region_id, report.discipline_id, report.responsible_manager_id
    edit(
        people,
        key,
        report,
        region=region.pk,
        discipline=999999,
        responsible_manager=people["other"][0].pk,
        subject="Allowed descriptive change",
    )
    report.refresh_from_db()
    assert (report.region_id, report.discipline_id, report.responsible_manager_id) == before
    assert events(report).count() == 1


def test_quality_changes_routing_and_reassigns_manager_with_three_audits(world):
    setup, people, report = world
    company = setup["company"]
    region = Region.objects.for_company(company).create(company=company, name="Other region")
    discipline = Discipline.objects.for_company(company).create(
        company=company, name="Other discipline"
    )
    old_due = report.due_date
    saved = edit(
        people,
        "quality",
        report,
        region=region.pk,
        discipline=discipline.pk,
        responsible_manager=people["other"][0].pk,
    )
    assert (saved.region_id, saved.discipline_id, saved.responsible_manager_id) == (
        region.pk,
        discipline.pk,
        people["other"][0].pk,
    )
    assert saved.due_date == old_due
    assert set(events(report).values_list("field_name", flat=True)) == {
        "region_id",
        "discipline_id",
        "responsible_manager_id",
    }
    # The old manager loses authority immediately; the new manager gains it.
    with pytest.raises(PermissionDenied):
        edit(people, "manager", saved, description="Old manager write")
    assert (
        edit(people, "other", saved, description="New manager write").description
        == "New manager write"
    )


@pytest.mark.parametrize("field", ["region", "discipline", "responsible_manager"])
@pytest.mark.parametrize("case", ["foreign", "inactive"])
def test_quality_cannot_choose_foreign_or_inactive_replacements(world, field, case):
    setup, people, report = world
    other = setup_company("other-edit")
    if field == "responsible_manager":
        choice = other["manager"] if case == "foreign" else people["other"][0]
        if case == "inactive":
            get_user_model().objects.filter(pk=choice.pk).update(is_active=False)
    else:
        model = Region if field == "region" else Discipline
        target = other["company"] if case == "foreign" else setup["company"]
        choice = model.objects.for_company(target).create(
            company=target, name="Replacement", active=case != "inactive"
        )
    with pytest.raises(ValidationError):
        edit(people, "quality", report, **{field: choice.pk})
    assert not events(report).exists()


@pytest.mark.parametrize("change", ["role", "membership"])
def test_quality_reassignment_requires_current_active_manager_membership(world, change):
    setup, people, report = world
    member = people["other"][1]
    values = {"role": "originator"} if change == "role" else {"active": False}
    Membership.objects.for_company(setup["company"]).filter(pk=member.pk).update(**values)
    with pytest.raises(ValidationError):
        edit(people, "quality", report, responsible_manager=member.user_id)
    assert not events(report).exists()


def test_unchanged_inactive_historical_choices_can_be_retained(world):
    setup, people, report = world
    company = setup["company"]
    Region.objects.for_company(company).filter(pk=report.region_id).update(active=False)
    Discipline.objects.for_company(company).filter(pk=report.discipline_id).update(active=False)
    get_user_model().objects.filter(pk=report.responsible_manager_id).update(is_active=False)
    Membership.objects.for_company(company).filter(pk=people["manager"][1].pk).update(active=False)
    saved = edit(people, "quality", report, description="Edited with historical assignment")
    assert saved.region_id == report.region_id
    assert saved.responsible_manager_id == report.responsible_manager_id
    assert events(report).count() == 1


def test_noop_has_no_audit_or_timestamp_change(world):
    _, people, report = world
    before = report.updated_at
    with freeze_time(before + timedelta(days=1)):
        saved = edit(people, "quality", report)
    saved.refresh_from_db()
    assert saved.updated_at == before and not events(report).exists()


def test_stale_revision_cannot_overwrite_a_newer_disjoint_edit(world):
    setup, people, report = world
    stale_data = payload(report, description="Stale browser write")
    region = Region.objects.for_company(setup["company"]).create(
        company=setup["company"], name="Fresh"
    )
    with freeze_time(report.updated_at + timedelta(seconds=2)):
        edit(people, "quality", report, region=region.pk)
    with pytest.raises(ValidationError, match="changed while you were editing"):
        update_internal_ncr(
            by=people["originator"][0],
            actor_membership=people["originator"][1],
            report_id=report.pk,
            data=stale_data,
        )
    report.refresh_from_db()
    assert report.region_id == region.pk and report.description != "Stale browser write"
    assert events(report).count() == 1


@pytest.mark.parametrize(
    "change", ["role", "assignment", "user", "membership", "company", "closed"]
)
def test_edit_rechecks_stale_authority_and_terminal_state(world, change):
    setup, people, report = world
    company = setup["company"]
    user, membership = people["manager"]
    if change == "role":
        Membership.objects.for_company(company).filter(pk=membership.pk).update(role="originator")
    elif change == "assignment":
        NCR.objects.for_company(company).filter(pk=report.pk).update(
            responsible_manager=people["other"][0]
        )
    elif change == "user":
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    elif change == "membership":
        Membership.objects.for_company(company).filter(pk=membership.pk).update(active=False)
    elif change == "company":
        type(company).objects.filter(pk=company.pk).update(active=False)
    else:
        set_state(report, "closed", people["quality"][0])
    with pytest.raises(ValidationError if change == "closed" else PermissionDenied):
        edit(people, "manager", report, subject="Refused edit")
    assert not events(report).exists()


def test_foreign_report_forged_membership_and_platform_bypass_are_refused(world):
    _, people, report = world
    foreign = setup_company("foreign-edit")
    developer = get_user_model().objects.create_superuser("developer-edit@example.com", "password")
    for user, membership in (
        (foreign["user"], foreign["member"]),
        (people["quality"][0], foreign["member"]),
        (foreign["user"], people["quality"][1]),
        (developer, None),
        (developer, people["quality"][1]),
    ):
        with pytest.raises(PermissionDenied):
            update_internal_ncr(
                by=user,
                actor_membership=membership,
                report_id=report.pk,
                data=payload(report, subject="Forged edit"),
            )
    assert not events(report).exists()


def test_second_audit_failure_rolls_back_all_fields_and_first_audit(world):
    _, people, report = world
    before = report.subject, report.description, report.updated_at
    original_save, inserted = AuditEntry.save, []

    def fail_second(entry, *args, **kwargs):
        inserted.append(entry)
        if len(inserted) == 2:
            raise RuntimeError("Second audit failed")
        return original_save(entry, *args, **kwargs)

    with patch.object(AuditEntry, "save", autospec=True, side_effect=fail_second):
        with pytest.raises(RuntimeError, match="Second audit failed"):
            edit(people, "quality", report, subject="First change", description="Second change")
    report.refresh_from_db()
    assert (report.subject, report.description, report.updated_at) == before
    assert not events(report).exists()


@pytest.mark.parametrize(
    "changes", [{"subject": ""}, {"subject": "x" * 201}, {"description": ""}, {"revision": ""}]
)
def test_edit_form_validation_refuses_invalid_input(world, changes):
    _, people, report = world
    with pytest.raises(ValidationError):
        edit(people, "quality", report, **changes)
    assert not events(report).exists()


def edit_url(report):
    return reverse("ncr:edit", kwargs={"public_id": report.public_id})


def login(user, csrf=False):
    client = Client(enforce_csrf_checks=csrf)
    client.force_login(user)
    return client


@pytest.mark.parametrize("key", ["originator", "manager", "quality", "admin", "other"])
def test_edit_screen_fields_and_link_follow_role_permissions(world, key):
    _, people, report = world
    client = login(people[key][0])
    detail = client.get(reverse("ncr:detail", kwargs={"public_id": report.public_id}))
    permitted = key in ("originator", "manager", "quality")
    assert (edit_url(report).encode() in detail.content) == permitted
    response = client.get(edit_url(report))
    if not permitted:
        assert response.status_code in (403, 404)
        return
    assert response.status_code == 200
    expected = set(DESCRIPTIVE_FIELDS) | {"revision"}
    if key == "quality":
        expected |= set(QUALITY_EDIT_FIELDS)
    assert set(response.context["form"].fields) == expected
    assert "classification" not in response.context["form"].fields
    assert not events(report).exists()


def test_post_uses_csrf_and_redirects_after_valid_edit(world):
    _, people, report = world
    client = login(people["quality"][0], csrf=True)
    assert client.post(edit_url(report), payload(report, subject="No CSRF")).status_code == 403
    response = client.get(edit_url(report))
    data = payload(report, subject="<script>example</script>")
    data["revision"] = response.context["form"]["revision"].value()
    data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
    saved = client.post(edit_url(report), data, follow=True)
    assert saved.status_code == 200 and saved.redirect_chain
    assert b"&lt;script&gt;example&lt;/script&gt;" in saved.content
    assert b"<script>example</script>" not in saved.content
    assert events(report).count() == 1


def test_http_stale_form_returns_conflict_without_overwriting(world):
    _, people, report = world
    client = login(people["manager"][0])
    initial = client.get(edit_url(report))
    data = payload(report, description="Stale edit")
    data["revision"] = initial.context["form"]["revision"].value()
    with freeze_time(report.updated_at + timedelta(seconds=3)):
        edit(people, "quality", report, subject="Newer subject")
    response = client.post(edit_url(report), data)
    assert response.status_code == 409 and b"changed while you were editing" in response.content
    report.refresh_from_db()
    assert report.subject == "Newer subject" and report.description != "Stale edit"


def test_http_foreign_ncr_and_wrong_methods_are_refused(world):
    _, people, report = world
    foreign = setup_company("foreign-edit-http")
    foreign_client = login(foreign["user"])
    for method in ("get", "post"):
        assert getattr(foreign_client, method)(edit_url(report)).status_code == 404
    own = login(people["quality"][0])
    for method in ("put", "patch", "delete"):
        assert getattr(own, method)(edit_url(report)).status_code == 405
    assert not events(report).exists()


def test_form_shows_only_same_company_active_or_current_choices(world):
    setup, people, report = world
    foreign = setup_company("foreign-form-edit")
    form = NCREditForm(
        company=setup["company"],
        report=report,
        editable_fields=DESCRIPTIVE_FIELDS + QUALITY_EDIT_FIELDS,
    )
    assert foreign["region"] not in form.fields["region"].queryset
    assert foreign["discipline"] not in form.fields["discipline"].queryset
    assert foreign["manager"] not in form.fields["responsible_manager"].queryset
    assert people["originator"][0] not in form.fields["responsible_manager"].queryset

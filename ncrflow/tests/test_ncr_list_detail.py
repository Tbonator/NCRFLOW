"""Read-only NCR list and detail screens: visibility, isolation and display rules."""

import html
import re
import uuid
from datetime import date, timedelta
from urllib.parse import parse_qs

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone
from freezegun import freeze_time

from apps.ncr.models import NCR, Classification
from apps.ncr.services import (
    audit_history_for,
    company_today,
    create_internal_ncr,
    ncr_details_for,
    ncr_list_for,
    visible_ncrs_for,
)
from apps.tenants.models import AuditEntry, Company, Discipline, Membership, Region

pytestmark = pytest.mark.django_db

PEOPLE = [
    ("orig1", "originator"),
    ("orig2", "originator"),
    ("manager1", "manager"),
    ("manager2", "manager"),
    ("quality", "quality"),
    ("admin", "company_admin"),
]
ROLE_KEYS = [key for key, _ in PEOPLE]
# Owner-approved visibility of the four company A reports built by the `world` fixture.
VISIBLE = {
    "orig1": {"n1"},
    "orig2": {"n2"},
    "manager1": {"n1", "n3", "n4"},
    "manager2": {"n2", "n3"},
    "quality": {"n1", "n2", "n3", "n4"},
    "admin": {"n1", "n2", "n3", "n4"},
}
SUBJECTS = {
    "n1": "Alpha one widget",
    "n2": "Alpha two gasket",
    "n3": "Alpha three flange",
    "n4": "Alpha four bracket",
}
FOREIGN_SUBJECT = "Bravo secret casting"
UNSAFE_METHODS = ["post", "put", "patch", "delete"]


def member(company, email, role, full_name=""):
    user = get_user_model().objects.create_user(email, "test-password", full_name=full_name)
    membership = Membership.objects.for_company(company).create(
        company=company, user=user, role=role
    )
    return user, membership


def make_company(suffix, zone="Africa/Johannesburg"):
    company = Company.objects.create(name=f"Factory {suffix}", timezone=zone)
    people = {
        key: member(company, f"{key}-{suffix.lower()}@example.com", role, f"{key.title()} {suffix}")
        for key, role in PEOPLE
    }
    return {
        "company": company,
        "people": people,
        "classification": Classification.objects.for_company(company).create(
            company=company, name=f"Class {suffix}", sla_days=3
        ),
        "region": Region.objects.for_company(company).create(
            company=company, name=f"Region {suffix}"
        ),
        "discipline": Discipline.objects.for_company(company).create(
            company=company, name=f"Discipline {suffix}"
        ),
    }


def raise_ncr(setup, by, manager, subject, **extra):
    user, membership = setup["people"][by]
    return create_internal_ncr(
        by=user,
        actor_membership=membership,
        data={
            "subject": subject,
            "description": f"Description of {subject}",
            "classification": setup["classification"].pk,
            "region": setup["region"].pk,
            "discipline": setup["discipline"].pk,
            "responsible_manager": setup["people"][manager][0].pk,
            "part_number": "PART-1",
            "job_number": "JOB-1",
            **extra,
        },
    )


def raw_ncr(setup, sequence, due, *, status="open", by="orig1", manager="manager1", subject=None):
    """A consistent row written directly, for due dates and statuses services cannot yet make."""
    stamps = {}
    if status == "closed":
        stamps = {"closed_at": timezone.now(), "closed_by": setup["people"]["quality"][0]}
    elif status == "cancelled":
        stamps = {
            "cancelled_at": timezone.now(),
            "cancelled_by": setup["people"]["quality"][0],
            "cancellation_reason": "Raised in error",
        }
    return NCR.objects.for_company(setup["company"]).create(
        company=setup["company"],
        originator=setup["people"][by][0],
        responsible_manager=setup["people"][manager][0],
        classification=setup["classification"],
        region=setup["region"],
        discipline=setup["discipline"],
        subject=subject or f"Raw report {sequence}",
        description="Raw description",
        direction="internal",
        sequence_number=sequence,
        number=f"NCR-{sequence:05d}",
        due_date=due,
        status=status,
        **stamps,
    )


def add_audit(report, actor, comment, **extra):
    return AuditEntry.objects.for_company(report.company_id).create(
        company_id=report.company_id,
        actor_user=actor,
        ncr=report,
        object_type="ncr",
        object_id=report.pk,
        object_label=report.number,
        action="field_changed",
        field_name="part_number",
        comment=comment,
        **{"old_value": "OLD", "new_value": "NEW", **extra},
    )


@pytest.fixture
def company_a():
    return make_company("A")


@pytest.fixture
def world():
    a, b = make_company("A"), make_company("B")
    reports = {
        "n1": raise_ncr(a, "orig1", "manager1", SUBJECTS["n1"]),
        "n2": raise_ncr(a, "orig2", "manager2", SUBJECTS["n2"]),
        "n3": raise_ncr(a, "manager1", "manager2", SUBJECTS["n3"]),
        "n4": raise_ncr(a, "quality", "manager1", SUBJECTS["n4"]),
    }
    foreign = raise_ncr(b, "orig1", "manager1", FOREIGN_SUBJECT)
    return {"a": a, "b": b, "reports": reports, "foreign": foreign}


def login(user):
    client = Client()
    client.force_login(user)
    return client


def list_url():
    return reverse("ncr:list")


def detail_url(report):
    return reverse("ncr:detail", kwargs={"public_id": report.public_id})


def expected_ids(world, key):
    return {world["reports"][name].public_id for name in VISIBLE[key]}


def service_ids(setup, key):
    user, membership = setup["people"][key]
    return set(
        visible_ncrs_for(by=user, actor_membership=membership).values_list("public_id", flat=True)
    )


def listed_ids(response):
    return [report.public_id for report in response.context["page"].object_list]


def rows(response):
    return re.findall(r"<tr\b.*?</tr>", response.content.decode(), re.S)


def row_for(response, text):
    matches = [row for row in rows(response) if text in row]
    assert len(matches) == 1, f"expected exactly one table row containing {text!r}"
    return matches[0]


def snapshot(*setups):
    state = []
    for setup in setups:
        company = setup["company"]
        state.append(
            (
                list(
                    NCR.objects.for_company(company)
                    .order_by("pk")
                    .values_list("pk", "status", "subject", "due_date", "updated_at")
                ),
                AuditEntry.objects.for_company(company).count(),
            )
        )
    return state


# 1. Cross-company isolation ------------------------------------------------------------


@pytest.mark.parametrize("key", ROLE_KEYS)
def test_service_never_returns_another_companys_ncrs(world, key):
    user, membership = world["a"]["people"][key]
    for queryset in (
        visible_ncrs_for(by=user, actor_membership=membership),
        ncr_list_for(by=user, actor_membership=membership, today=date(2026, 11, 13)),
        ncr_details_for(by=user, actor_membership=membership, today=date(2026, 11, 13)),
    ):
        found = list(queryset)
        assert found, "every company A role in this fixture can see at least one NCR"
        assert {report.company_id for report in found} == {world["a"]["company"].pk}
        assert world["foreign"].public_id not in {report.public_id for report in found}


@pytest.mark.parametrize("key", ROLE_KEYS)
def test_list_never_shows_another_companys_ncrs(world, key):
    client = login(world["a"]["people"][key][0])
    for query in (
        {},
        {"company_id": world["b"]["company"].pk, "company": world["b"]["company"].pk},
    ):
        response = client.get(list_url(), query)
        assert response.status_code == 200
        assert world["foreign"].public_id not in listed_ids(response)
        assert FOREIGN_SUBJECT.encode() not in response.content
        assert str(world["foreign"].public_id).encode() not in response.content
        assert b"Factory B" not in response.content


@pytest.mark.parametrize("key", ROLE_KEYS)
def test_foreign_detail_is_the_same_404_as_a_nonexistent_ncr(world, key):
    client = login(world["a"]["people"][key][0])
    foreign_id, random_id = world["foreign"].public_id, uuid.uuid4()
    foreign = client.get(detail_url(world["foreign"]))
    missing = client.get(reverse("ncr:detail", kwargs={"public_id": random_id}))
    assert foreign.status_code == 404 and missing.status_code == 404
    assert FOREIGN_SUBJECT.encode() not in foreign.content
    assert foreign.content.replace(str(foreign_id).encode(), b"X") == missing.content.replace(
        str(random_id).encode(), b"X"
    )
    with_parameter = client.get(
        detail_url(world["foreign"]), {"company_id": world["b"]["company"].pk}
    )
    assert with_parameter.status_code == 404


def test_company_b_user_cannot_see_company_a(world):
    user, membership = world["b"]["people"]["quality"]
    assert service_ids(world["b"], "quality") == {world["foreign"].public_id}
    client = login(user)
    response = client.get(list_url())
    assert listed_ids(response) == [world["foreign"].public_id]
    for name, report in world["reports"].items():
        assert SUBJECTS[name].encode() not in response.content
        assert client.get(detail_url(report)).status_code == 404


def test_user_in_two_companies_sees_only_the_selected_company(world):
    user, membership_a = member(world["a"]["company"], "dual@example.com", "quality", "Dual User")
    membership_b = Membership.objects.for_company(world["b"]["company"]).create(
        company=world["b"]["company"], user=user, role="quality"
    )
    all_a = {report.public_id for report in world["reports"].values()}
    in_a = visible_ncrs_for(by=user, actor_membership=membership_a)
    in_b = visible_ncrs_for(by=user, actor_membership=membership_b)
    assert set(in_a.values_list("public_id", flat=True)) == all_a
    assert set(in_b.values_list("public_id", flat=True)) == {world["foreign"].public_id}

    client = login(user)
    # No company chosen yet: neither screen may guess one.
    assert client.get(list_url()).url == reverse("tenants:select")
    assert client.get(detail_url(world["reports"]["n1"])).url == reverse("tenants:select")

    client.post(reverse("tenants:select"), {"membership": membership_a.pk})
    response = client.get(list_url())
    assert set(listed_ids(response)) == all_a
    assert FOREIGN_SUBJECT.encode() not in response.content
    assert client.get(detail_url(world["reports"]["n1"])).status_code == 200
    assert client.get(detail_url(world["foreign"])).status_code == 404

    client.post(reverse("tenants:select"), {"membership": membership_b.pk})
    response = client.get(list_url())
    assert listed_ids(response) == [world["foreign"].public_id]
    for subject in SUBJECTS.values():
        assert subject.encode() not in response.content
    assert client.get(detail_url(world["foreign"])).status_code == 200
    assert client.get(detail_url(world["reports"]["n1"])).status_code == 404


def test_forged_membership_context_is_refused_by_the_services(world):
    user, _ = world["a"]["people"]["orig1"]
    foreign_quality = world["b"]["people"]["quality"][1]
    colleague_quality = world["a"]["people"]["quality"][1]
    for forged in (foreign_quality, colleague_quality, None):
        with pytest.raises(PermissionDenied):
            visible_ncrs_for(by=user, actor_membership=forged)
        with pytest.raises(PermissionDenied):
            ncr_list_for(by=user, actor_membership=forged, today=date(2026, 11, 13))
        with pytest.raises(PermissionDenied):
            ncr_details_for(by=user, actor_membership=forged, today=date(2026, 11, 13))


# 2. Role checks -------------------------------------------------------------------------


@pytest.mark.parametrize("key", ROLE_KEYS)
def test_service_visibility_per_role(world, key):
    user, membership = world["a"]["people"][key]
    expected = expected_ids(world, key)
    assert service_ids(world["a"], key) == expected
    today = date(2026, 11, 13)
    listed = ncr_list_for(by=user, actor_membership=membership, today=today)
    details = ncr_details_for(by=user, actor_membership=membership, today=today)
    assert {report.public_id for report in listed} == expected
    assert {report.public_id for report in details} == expected


@pytest.mark.parametrize("key", ROLE_KEYS)
def test_list_screen_shows_exactly_the_visible_ncrs_per_role(world, key):
    response = login(world["a"]["people"][key][0]).get(list_url())
    assert response.status_code == 200
    assert set(listed_ids(response)) == expected_ids(world, key)
    assert len(listed_ids(response)) == len(VISIBLE[key])
    for name, subject in SUBJECTS.items():
        shown = subject.encode() in response.content
        linked = detail_url(world["reports"][name]).encode() in response.content
        assert shown == linked == (name in VISIBLE[key])


@pytest.mark.parametrize("key", ROLE_KEYS)
def test_detail_screen_is_200_when_visible_and_404_otherwise(world, key):
    client = login(world["a"]["people"][key][0])
    for name, report in world["reports"].items():
        response = client.get(detail_url(report))
        if name in VISIBLE[key]:
            assert response.status_code == 200
            assert response.context["report"].public_id == report.public_id
            assert SUBJECTS[name].encode() in response.content
        else:
            assert response.status_code == 404
            assert SUBJECTS[name].encode() not in response.content


def test_hidden_detail_matches_nonexistent_404_inside_the_same_company(world):
    client = login(world["a"]["people"]["orig1"][0])
    hidden, random_id = world["reports"]["n2"], uuid.uuid4()
    refused = client.get(detail_url(hidden))
    missing = client.get(reverse("ncr:detail", kwargs={"public_id": random_id}))
    assert refused.status_code == missing.status_code == 404
    assert refused.content.replace(str(hidden.public_id).encode(), b"X") == missing.content.replace(
        str(random_id).encode(), b"X"
    )


def test_role_is_read_fresh_so_a_demoted_user_loses_wider_visibility(world):
    setup = world["a"]
    for key in ("quality", "admin", "manager1"):
        user, stale_membership = setup["people"][key]
        Membership.objects.for_company(setup["company"]).filter(pk=stale_membership.pk).update(
            role="originator"
        )
        raised = {
            report.public_id
            for report in world["reports"].values()
            if report.originator_id == user.pk
        }
        # The in-memory membership still claims the old role.
        assert stale_membership.role != "originator"
        found = visible_ncrs_for(by=user, actor_membership=stale_membership)
        assert set(found.values_list("public_id", flat=True)) == raised
        client = login(user)
        assert set(listed_ids(client.get(list_url()))) == raised
        for report in world["reports"].values():
            expected = 200 if report.public_id in raised else 404
            assert client.get(detail_url(report)).status_code == expected


def test_promoted_originator_gains_company_wide_visibility_only_in_own_company(world):
    user, membership = world["a"]["people"]["orig2"]
    Membership.objects.for_company(world["a"]["company"]).filter(pk=membership.pk).update(
        role="quality"
    )
    response = login(user).get(list_url())
    assert set(listed_ids(response)) == {report.public_id for report in world["reports"].values()}
    assert world["foreign"].public_id not in listed_ids(response)


# 3. Inactive and anonymous access -------------------------------------------------------


def deactivate(setup, key, change):
    user, membership = setup["people"][key]
    if change == "user":
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    elif change == "membership":
        Membership.objects.for_company(setup["company"]).filter(pk=membership.pk).update(
            active=False
        )
    else:
        Company.objects.filter(pk=setup["company"].pk).update(active=False)


@pytest.mark.parametrize("change", ["user", "membership", "company"])
@pytest.mark.parametrize("key", ["orig1", "manager1", "quality", "admin"])
def test_services_refuse_inactive_user_membership_or_company(world, key, change):
    user, membership = world["a"]["people"][key]
    deactivate(world["a"], key, change)
    with pytest.raises(PermissionDenied):
        visible_ncrs_for(by=user, actor_membership=membership)
    with pytest.raises(PermissionDenied):
        ncr_list_for(by=user, actor_membership=membership, today=date(2026, 11, 13))
    with pytest.raises(PermissionDenied):
        ncr_details_for(by=user, actor_membership=membership, today=date(2026, 11, 13))


@pytest.mark.parametrize("change", ["user", "membership", "company"])
@pytest.mark.parametrize("key", ["orig1", "manager1", "quality", "admin"])
def test_screens_refuse_inactive_user_membership_or_company(world, key, change):
    user, _ = world["a"]["people"][key]
    report = world["reports"]["n1" if key != "quality" else "n4"]
    client = login(user)
    urls = [list_url(), detail_url(report)]
    for url in urls:
        assert client.get(url).status_code == 200
    deactivate(world["a"], key, change)
    # Both an already signed-in session and a fresh one are refused.
    if change == "user":
        # A deactivated account cannot even open a new session.
        with pytest.raises(PermissionDenied):
            login(user)
        clients = [client]
    else:
        clients = [client, login(user)]
    for active_client in clients:
        for url in urls:
            response = active_client.get(url)
            assert response.status_code in (302, 403, 404)
            assert report.subject.encode() not in response.content
            assert report.number.encode() not in response.content
            if response.status_code == 302:
                assert response.url.split("?")[0] in (
                    reverse("accounts:login"),
                    reverse("tenants:select"),
                    reverse("tenants:no-access"),
                )
            if change == "user":
                assert response.url.startswith(reverse("accounts:login"))


def test_anonymous_users_are_redirected_to_login(client, world):
    for url in (list_url(), detail_url(world["reports"]["n1"]), detail_url(world["foreign"])):
        response = client.get(url)
        assert response.status_code == 302
        assert response.url.startswith(reverse("accounts:login"))
        for method in UNSAFE_METHODS:
            response = getattr(client, method)(url)
            assert response.status_code == 302
            assert response.url.startswith(reverse("accounts:login"))


def test_platform_account_without_membership_sees_nothing(client, world):
    developer = get_user_model().objects.create_superuser("developer@example.com", "test-password")
    client.force_login(developer)
    for url in (list_url(), detail_url(world["reports"]["n1"])):
        response = client.get(url)
        assert response.url == reverse("tenants:no-access")
        assert SUBJECTS["n1"].encode() not in response.content


# 4. Detail lookup cannot be bypassed ----------------------------------------------------


def test_integer_and_malformed_identifiers_are_not_routable(world):
    report = world["reports"]["n1"]
    client = login(world["a"]["people"]["quality"][0])
    for value in (
        str(report.pk),
        "1",
        str(report.sequence_number),
        report.number,
        "not-a-uuid",
        str(report.public_id)[:-1],
        f"{report.public_id}x",
    ):
        path = f"/ncrs/{value}/"
        with pytest.raises(Resolver404):
            resolve(path)
        with pytest.raises(Resolver404):
            resolve(f"{path}created/")
        response = client.get(path)
        assert response.status_code == 404
        assert report.subject.encode() not in response.content
    assert client.get(detail_url(report)).status_code == 200


@pytest.mark.parametrize("key", ROLE_KEYS)
def test_guessed_uuids_are_not_found(world, key):
    client = login(world["a"]["people"][key][0])
    for guess in (uuid.uuid4(), uuid.UUID(int=0), uuid.UUID(int=world["reports"]["n1"].pk)):
        assert client.get(reverse("ncr:detail", kwargs={"public_id": guess})).status_code == 404


def test_query_parameters_cannot_widen_the_detail_lookup(world):
    client = login(world["a"]["people"]["orig1"][0])
    hidden = world["reports"]["n2"]
    for query in (
        {"public_id": str(hidden.public_id)},
        {"id": hidden.pk, "pk": hidden.pk},
        {"role": "quality", "membership": world["a"]["people"]["quality"][1].pk},
    ):
        assert client.get(detail_url(hidden), query).status_code == 404
        own = client.get(detail_url(world["reports"]["n1"]), query)
        assert own.context["report"].public_id == world["reports"]["n1"].public_id
        assert set(listed_ids(client.get(list_url(), query))) == expected_ids(world, "orig1")


# 5. Read-only ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["orig1", "manager2", "quality", "admin"])
def test_unsafe_methods_are_rejected_and_change_nothing(world, key):
    client = login(world["a"]["people"][key][0])
    before = snapshot(world["a"], world["b"])
    urls = [
        list_url(),
        detail_url(world["reports"]["n1"]),
        detail_url(world["reports"]["n2"]),
        detail_url(world["foreign"]),
        reverse("ncr:detail", kwargs={"public_id": uuid.uuid4()}),
    ]
    payload = {"status": "closed", "subject": "Changed", "action": "close", "delete": "1"}
    for url in urls:
        for method in UNSAFE_METHODS:
            # The same answer for own, hidden, foreign and missing NCRs: no existence oracle.
            assert getattr(client, method)(url, payload).status_code == 405
    assert snapshot(world["a"], world["b"]) == before


def test_get_requests_never_change_status_or_audit_history(world):
    client = login(world["a"]["people"]["quality"][0])
    before = snapshot(world["a"], world["b"])
    for query in ({}, {"status": "closed"}, {"status": "bogus", "page": "x"}, {"action": "close"}):
        assert client.get(list_url(), query).status_code == 200
        for report in world["reports"].values():
            assert client.get(detail_url(report), query).status_code == 200
        assert client.get(detail_url(world["foreign"]), query).status_code == 404
    assert snapshot(world["a"], world["b"]) == before
    for report in world["reports"].values():
        report.refresh_from_db()
        assert report.status == NCR.Status.OPEN


@pytest.mark.parametrize("key", ROLE_KEYS)
def test_pages_contain_no_data_changing_form_except_sign_out(world, key):
    client = login(world["a"]["people"][key][0])
    visible = world["reports"][sorted(VISIBLE[key])[0]]
    for url in (list_url(), detail_url(visible)):
        page = client.get(url).content.decode()
        forms = re.findall(r"<form\b[^>]*>", page, re.I)
        # A form without an explicit method is a GET form; anything else changes data.
        changing = [
            form
            for form in forms
            if re.search(r"\bmethod\s*=", form, re.I)
            and not re.search(r"\bmethod\s*=\s*[\"']?get\b", form, re.I)
        ]
        assert len(changing) == 1
        assert f'action="{reverse("accounts:logout")}"' in changing[0]
        assert not re.search(r"\b(hx-(post|put|patch|delete)|formaction|formmethod)\b", page, re.I)
        assert not re.search(r"<(input|textarea)\b(?![^>]*type=\"hidden\")", page, re.I)


# 6. List behaviour ----------------------------------------------------------------------


def test_default_order_is_due_date_then_sequence_number(company_a):
    start = date(2026, 12, 1)
    for sequence, offset in ((5, 2), (3, 0), (1, 1), (2, 0), (100000, 1), (99999, 1)):
        raw_ncr(company_a, sequence, start + timedelta(days=offset))
    expected = [2, 3, 1, 99999, 100000, 5]
    user, membership = company_a["people"]["quality"]
    listed = ncr_list_for(by=user, actor_membership=membership, today=start)
    assert [report.sequence_number for report in listed] == expected
    response = login(user).get(list_url())
    shown = [report.sequence_number for report in response.context["page"].object_list]
    assert shown == expected
    page = response.content.decode()
    positions = [page.index(f">NCR-{sequence:05d}<") for sequence in expected]
    assert positions == sorted(positions)


@pytest.fixture
def mixed_statuses(company_a):
    due = date(2026, 12, 1)
    return {
        "open_own": raw_ncr(company_a, 1, due, subject="Open own"),
        "open_other": raw_ncr(company_a, 2, due, by="orig2", manager="manager2"),
        "in_review": raw_ncr(company_a, 3, due, status="in_review", subject="Reviewing own"),
        "closed": raw_ncr(company_a, 4, due, status="closed", subject="Closed own"),
        "cancelled": raw_ncr(company_a, 5, due, status="cancelled", by="orig2", manager="manager2"),
    }


@pytest.mark.parametrize(
    "status,expected",
    [
        ("", {"open_own", "open_other", "in_review", "closed", "cancelled"}),
        ("open", {"open_own", "open_other"}),
        ("in_review", {"in_review"}),
        ("closed", {"closed"}),
        ("cancelled", {"cancelled"}),
        ("awaiting_supplier", set()),
    ],
)
def test_status_filter_with_a_valid_value(company_a, mixed_statuses, status, expected):
    user, membership = company_a["people"]["quality"]
    wanted = {mixed_statuses[name].public_id for name in expected}
    listed = ncr_list_for(
        by=user, actor_membership=membership, today=date(2026, 11, 13), status=status
    )
    assert {report.public_id for report in listed} == wanted
    response = login(user).get(list_url(), {"status": status})
    assert response.status_code == 200
    assert set(listed_ids(response)) == wanted
    assert response.context["selected_status"] == status
    assert not response.context["filter_form"].errors


def test_status_filter_never_widens_visibility(company_a, mixed_statuses):
    user, membership = company_a["people"]["orig1"]
    response = login(user).get(list_url(), {"status": "open"})
    assert listed_ids(response) == [mixed_statuses["open_own"].public_id]
    assert b"Raw report 2" not in response.content
    cancelled = login(user).get(list_url(), {"status": "cancelled"})
    assert listed_ids(cancelled) == []
    assert b"NCR-00005" not in cancelled.content
    listed = ncr_list_for(
        by=user, actor_membership=membership, today=date(2026, 11, 13), status="open"
    )
    assert [report.public_id for report in listed] == [mixed_statuses["open_own"].public_id]


@pytest.mark.parametrize(
    "value",
    [
        "nonsense",
        "OPEN",
        "open' OR '1'='1",
        "<script>alert(1)</script>",
        "open,closed",
        "\x00",
        "9",
    ],
)
def test_invalid_status_is_handled_without_error_or_leak(company_a, mixed_statuses, value):
    own = {mixed_statuses[name].public_id for name in ("open_own", "in_review", "closed")}
    response = login(company_a["people"]["orig1"][0]).get(list_url(), {"status": value})
    assert response.status_code == 200
    assert set(listed_ids(response)) == own
    assert response.context["selected_status"] == ""
    assert b"Raw report 2" not in response.content and b"NCR-00005" not in response.content
    assert b"<script>alert(1)</script>" not in response.content
    everything = login(company_a["people"]["quality"][0]).get(list_url(), {"status": value})
    assert everything.status_code == 200 and len(listed_ids(everything)) == 5


@pytest.mark.parametrize("value", ["abc", "0", "-5", "99999", "1.5", "", "2; DROP TABLE", "١"])
def test_invalid_page_is_handled_without_error_or_leak(company_a, mixed_statuses, value):
    own = {mixed_statuses[name].public_id for name in ("open_own", "in_review", "closed")}
    client = login(company_a["people"]["orig1"][0])
    for query in ({"page": value}, {"page": value, "status": "bogus"}):
        response = client.get(list_url(), query)
        assert response.status_code == 200
        assert set(listed_ids(response)) == own
        assert b"Raw report 2" not in response.content


def page_links(response):
    hrefs = re.findall(r'href="\?([^"]*)"', response.content.decode())
    return [parse_qs(html.unescape(href)) for href in hrefs]


def test_pagination_is_25_per_page_and_keeps_the_status_filter(company_a):
    due = date(2026, 12, 1)
    for sequence in range(1, 31):
        raw_ncr(company_a, sequence, due)
    for sequence in range(31, 34):
        raw_ncr(company_a, sequence, due, status="in_review")
    client = login(company_a["people"]["quality"][0])

    first = client.get(list_url())
    assert len(listed_ids(first)) == 25
    assert first.context["page"].paginator.count == 33
    assert first.context["page"].paginator.num_pages == 2
    assert {"page": ["2"]} in page_links(first)
    second = client.get(list_url(), {"page": 2})
    assert len(listed_ids(second)) == 8
    assert not set(listed_ids(first)) & set(listed_ids(second))
    assert {"page": ["1"]} in page_links(second)

    filtered = client.get(list_url(), {"status": "open"})
    assert len(listed_ids(filtered)) == 25
    assert filtered.context["page"].paginator.count == 30
    links = [link for link in page_links(filtered) if "page" in link]
    assert links and all(link == {"status": ["open"], "page": ["2"]} for link in links)
    filtered_second = client.get(list_url(), {"status": "open", "page": 2})
    assert len(listed_ids(filtered_second)) == 5
    assert {report.status for report in filtered_second.context["page"].object_list} == {"open"}
    back = [link for link in page_links(filtered_second) if "page" in link]
    assert back and all(link == {"status": ["open"], "page": ["1"]} for link in back)
    # Out-of-range pages fall back to a real page of the same filtered set.
    beyond = client.get(list_url(), {"status": "open", "page": 50})
    assert beyond.status_code == 200
    assert {report.status for report in beyond.context["page"].object_list} == {"open"}

    exactly = client.get(list_url(), {"status": "in_review"})
    assert len(listed_ids(exactly)) == 3
    assert not [link for link in page_links(exactly) if "page" in link]


def test_empty_states(company_a):
    client = login(company_a["people"]["orig1"][0])
    nothing = client.get(list_url())
    assert nothing.status_code == 200
    assert listed_ids(nothing) == [] and rows(nothing) == []
    assert b"no ncrs" in nothing.content.lower()

    raw_ncr(company_a, 1, date(2026, 12, 1), by="orig2", manager="manager2")
    still_nothing = client.get(list_url())
    assert listed_ids(still_nothing) == [] and b"NCR-00001" not in still_nothing.content

    raw_ncr(company_a, 2, date(2026, 12, 1))
    no_match = client.get(list_url(), {"status": "closed"})
    assert no_match.status_code == 200
    assert listed_ids(no_match) == [] and rows(no_match) == []
    assert b"no ncrs" in no_match.content.lower()
    assert no_match.content != nothing.content
    assert len(listed_ids(client.get(list_url()))) == 1


def test_list_columns_show_status_classification_manager_and_due_date(world):
    response = login(world["a"]["people"]["orig1"][0]).get(list_url())
    row = row_for(response, SUBJECTS["n1"])
    report = world["reports"]["n1"]
    assert report.number in row and "Open" in row
    assert "Class A" in row and "Manager1 A" in row
    assert str(report.due_date.day) in row and str(report.due_date.year) in row
    assert "no-store" in response["Cache-Control"]


# 7. Overdue marker ----------------------------------------------------------------------

ZONE_BOUNDARIES = [
    # Created 13 November local in both zones with a 3 day SLA, so due 16 November.
    ("Africa/Johannesburg", "2026-11-16 21:59:59", "2026-11-16 22:00:01"),
    ("America/Los_Angeles", "2026-11-17 07:59:59", "2026-11-17 08:00:01"),
]


def get_at(instant, user, url):
    with freeze_time(instant):
        return login(user).get(url)


@pytest.mark.parametrize("zone,last_moment,first_overdue", ZONE_BOUNDARIES)
def test_overdue_boundary_follows_the_company_timezone(zone, last_moment, first_overdue):
    setup = make_company("Z", zone)
    with freeze_time("2026-11-13 10:00:00"):
        report = raise_ncr(setup, "orig1", "manager1", "Boundary probe")
    assert report.due_date == date(2026, 11, 16)
    user = setup["people"]["orig1"][0]
    for url in (list_url(), detail_url(report)):
        for instant in ("2026-11-13 10:00:00", "2026-11-16 10:00:00", last_moment):
            response = get_at(instant, user, url)
            assert response.status_code == 200
            assert b"Overdue" not in response.content, (url, instant)
        for instant in (first_overdue, "2026-11-30 10:00:00"):
            response = get_at(instant, user, url)
            assert response.status_code == 200
            assert b"Overdue" in response.content, (url, instant)


@pytest.mark.parametrize("zone,last_moment,first_overdue", ZONE_BOUNDARIES)
def test_company_today_is_the_local_calendar_date(zone, last_moment, first_overdue):
    company = Company.objects.create(name="Clock", timezone=zone)
    with freeze_time(last_moment):
        assert company_today(company) == date(2026, 11, 16)
    with freeze_time(first_overdue):
        assert company_today(company) == date(2026, 11, 17)


def test_two_companies_disagree_about_overdue_at_the_same_instant():
    south, west = make_company("S"), make_company("W", "America/Los_Angeles")
    with freeze_time("2026-11-13 10:00:00"):
        south_report = raise_ncr(south, "orig1", "manager1", "South probe")
        west_report = raise_ncr(west, "orig1", "manager1", "West probe")
    assert south_report.due_date == west_report.due_date == date(2026, 11, 16)
    # 05:00 on the 17th in Johannesburg, 19:00 on the 16th in Los Angeles.
    instant = "2026-11-17 03:00:00"
    for url in (list_url(), detail_url(south_report)):
        assert b"Overdue" in get_at(instant, south["people"]["quality"][0], url).content
    for url in (list_url(), detail_url(west_report)):
        assert b"Overdue" not in get_at(instant, west["people"]["quality"][0], url).content


def test_overdue_marker_is_per_row(company_a):
    past = raw_ncr(company_a, 1, date(2026, 11, 12), subject="Past due row")
    today = raw_ncr(company_a, 2, date(2026, 11, 13), subject="Due today row")
    future = raw_ncr(company_a, 3, date(2026, 11, 14), subject="Future row")
    user, membership = company_a["people"]["quality"]
    response = get_at("2026-11-13 10:00:00", user, list_url())
    assert "Overdue" in row_for(response, "Past due row")
    assert "Overdue" not in row_for(response, "Due today row")
    assert "Overdue" not in row_for(response, "Future row")
    listed = ncr_list_for(by=user, actor_membership=membership, today=date(2026, 11, 13))
    assert {report.pk: report.is_overdue for report in listed} == {
        past.pk: True,
        today.pk: False,
        future.pk: False,
    }


def test_closed_and_cancelled_ncrs_are_never_marked_overdue(company_a):
    long_ago = date(2026, 1, 5)
    reports = {
        status: raw_ncr(company_a, index, long_ago, status=status, subject=f"Probe {status} row")
        for index, status in enumerate(["open", "in_review", "closed", "cancelled"], start=1)
    }
    for status, report in reports.items():
        report.refresh_from_db()
        assert report.status == status
    expected = {"open": True, "in_review": True, "closed": False, "cancelled": False}
    user, membership = company_a["people"]["quality"]
    today = date(2026, 11, 13)
    listed = ncr_list_for(by=user, actor_membership=membership, today=today)
    assert {report.status: report.is_overdue for report in listed} == expected
    details = ncr_details_for(by=user, actor_membership=membership, today=today)
    assert {report.status: report.is_overdue for report in details} == expected
    response = get_at("2026-11-13 10:00:00", user, list_url())
    for status, overdue in expected.items():
        assert ("Overdue" in row_for(response, f"Probe {status} row")) is overdue
        detail = get_at("2026-11-13 10:00:00", user, detail_url(reports[status]))
        assert detail.status_code == 200
        assert (b"Overdue" in detail.content) is overdue


# 8. Detail content and audit history ----------------------------------------------------


def test_detail_shows_the_report_fields(world):
    report = world["reports"]["n1"]
    response = login(world["a"]["people"]["orig1"][0]).get(detail_url(report))
    assert response.context["company"].pk == world["a"]["company"].pk
    for text in (
        report.number,
        SUBJECTS["n1"],
        f"Description of {SUBJECTS['n1']}",
        "Open",
        "Internal",
        "Class A",
        "Region A",
        "Discipline A",
        "Orig1 A",
        "Manager1 A",
        "PART-1",
        "JOB-1",
    ):
        assert text.encode() in response.content, text
    assert "no-store" in response["Cache-Control"]


def test_detail_audit_history_is_only_for_that_ncr(world):
    a, b = world["a"], world["b"]
    first, second = world["reports"]["n1"], world["reports"]["n2"]
    with freeze_time("2026-11-20 09:00:00"):
        later = add_audit(first, a["people"]["quality"][0], "VISIBLE-N1-LATER")
    with freeze_time("2026-11-19 09:00:00"):
        earlier = add_audit(first, None, "VISIBLE-N1-SYSTEM")
    add_audit(second, a["people"]["quality"][0], "HIDDEN-N2-COMMENT")
    add_audit(world["foreign"], b["people"]["quality"][0], "HIDDEN-B-COMMENT")
    # A non-NCR entry in the same company must not appear either.
    AuditEntry.objects.for_company(a["company"]).create(
        company=a["company"],
        actor_user=a["people"]["quality"][0],
        object_type="region",
        object_id=a["region"].pk,
        action="field_changed",
        comment="HIDDEN-REGION-COMMENT",
    )
    response = login(a["people"]["quality"][0]).get(detail_url(first))
    entries = list(response.context["audit_entries"])
    assert {entry.ncr_id for entry in entries} == {first.pk}
    assert {entry.company_id for entry in entries} == {a["company"].pk}
    assert len(entries) == 3
    assert entries[0].action == "created"
    assert [entry.pk for entry in entries[1:]] == [earlier.pk, later.pk]
    assert [entry.pk for entry in history(a, "quality", first)] == [entry.pk for entry in entries]
    page = response.content.decode()
    assert "VISIBLE-N1-LATER" in page and "VISIBLE-N1-SYSTEM" in page
    assert page.index("VISIBLE-N1-SYSTEM") < page.index("VISIBLE-N1-LATER")
    for hidden in (
        "HIDDEN-N2-COMMENT",
        "HIDDEN-B-COMMENT",
        "HIDDEN-REGION-COMMENT",
        SUBJECTS["n2"],
        FOREIGN_SUBJECT,
        str(second.public_id),
        str(world["foreign"].public_id),
    ):
        assert hidden not in page, hidden
    assert "Quality A" in page and "System" in page
    assert "OLD" in page and "NEW" in page

    other = login(a["people"]["quality"][0]).get(detail_url(second))
    assert "HIDDEN-N2-COMMENT" in other.content.decode()
    assert "VISIBLE-N1-LATER" not in other.content.decode()
    foreign = login(b["people"]["quality"][0]).get(detail_url(world["foreign"]))
    assert {entry.ncr_id for entry in foreign.context["audit_entries"]} == {world["foreign"].pk}
    assert "HIDDEN-B-COMMENT" in foreign.content.decode()
    assert "VISIBLE-N1" not in foreign.content.decode()


def test_same_numbered_ncr_in_another_company_does_not_share_history(world):
    first, foreign = world["reports"]["n1"], world["foreign"]
    assert first.number == foreign.number == "NCR-00001"
    entries = history(world["a"], "quality", first)
    assert entries and all(entry.company_id == first.company_id for entry in entries)
    foreign_entries = history(world["b"], "quality", foreign)
    assert foreign_entries and all(entry.ncr_id == foreign.pk for entry in foreign_entries)
    assert not {entry.pk for entry in entries} & {entry.pk for entry in foreign_entries}


def history(setup, key, report):
    user, membership = setup["people"][key]
    return list(audit_history_for(by=user, actor_membership=membership, report=report))


@pytest.mark.parametrize("key", ROLE_KEYS)
def test_audit_history_service_is_empty_for_another_companys_report(world, key):
    add_audit(world["foreign"], world["b"]["people"]["quality"][0], "HIDDEN-B-COMMENT")
    assert history(world["a"], key, world["foreign"]) == []
    # And the other way round: company B never reads company A's history.
    for report in world["reports"].values():
        assert history(world["b"], key, report) == []


@pytest.mark.parametrize("key", ROLE_KEYS)
def test_audit_history_service_follows_visibility_per_role(world, key):
    quality = world["a"]["people"]["quality"][0]
    extra = {
        name: add_audit(report, quality, f"EXTRA-{name}")
        for name, report in world["reports"].items()
    }
    for name, report in world["reports"].items():
        entries = history(world["a"], key, report)
        if name in VISIBLE[key]:
            assert {entry.ncr_id for entry in entries} == {report.pk}
            assert {entry.company_id for entry in entries} == {world["a"]["company"].pk}
            assert len(entries) == 2
            assert entries[0].action == "created" and entries[1].pk == extra[name].pk
        else:
            assert entries == [], (key, name)


def test_audit_history_service_hides_same_company_reports_the_actor_may_not_see(world):
    reports = world["reports"]
    # Originator on someone else's NCR; manager on one they neither manage nor raised.
    assert history(world["a"], "orig1", reports["n2"]) == []
    assert history(world["a"], "orig2", reports["n1"]) == []
    assert history(world["a"], "manager1", reports["n2"]) == []
    assert history(world["a"], "manager2", reports["n1"]) == []
    assert history(world["a"], "manager2", reports["n4"]) == []
    assert history(world["a"], "orig1", reports["n1"])
    assert history(world["a"], "manager1", reports["n1"])
    assert history(world["a"], "manager1", reports["n3"])


def test_audit_history_service_is_oldest_first(world):
    report, quality = world["reports"]["n1"], world["a"]["people"]["quality"][0]
    with freeze_time("2030-01-03 09:00:00"):
        last = add_audit(report, quality, "third")
    with freeze_time("2030-01-01 09:00:00"):
        first = add_audit(report, quality, "first")
    with freeze_time("2030-01-02 09:00:00"):
        middle = add_audit(report, None, "second")
    entries = history(world["a"], "orig1", report)
    assert entries[0].action == "created"
    assert [entry.pk for entry in entries[1:]] == [first.pk, middle.pk, last.pk]


def test_audit_history_service_refuses_forged_membership(world):
    user, _ = world["a"]["people"]["orig1"]
    own, foreign = world["reports"]["n1"], world["foreign"]
    forgeries = (
        world["b"]["people"]["quality"][1],
        world["b"]["people"]["orig1"][1],
        world["a"]["people"]["quality"][1],
        None,
    )
    for forged in forgeries:
        for report in (own, foreign):
            with pytest.raises(PermissionDenied):
                audit_history_for(by=user, actor_membership=forged, report=report)
    with pytest.raises(PermissionDenied):
        audit_history_for(by=None, actor_membership=world["a"]["people"]["orig1"][1], report=own)


@pytest.mark.parametrize("change", ["user", "membership", "company"])
@pytest.mark.parametrize("key", ["orig1", "manager1", "quality", "admin"])
def test_audit_history_service_refuses_inactive_user_membership_or_company(world, key, change):
    user, membership = world["a"]["people"][key]
    report = world["reports"]["n1"]
    assert audit_history_for(by=user, actor_membership=membership, report=report).exists()
    deactivate(world["a"], key, change)
    with pytest.raises(PermissionDenied):
        audit_history_for(by=user, actor_membership=membership, report=report)


def test_audit_history_service_reads_the_role_fresh(world):
    setup, reports = world["a"], world["reports"]
    user, stale_membership = setup["people"]["quality"]
    assert history(setup, "quality", reports["n1"])
    Membership.objects.for_company(setup["company"]).filter(pk=stale_membership.pk).update(
        role="originator"
    )
    assert stale_membership.role == "quality"
    for name in ("n1", "n2", "n3"):
        found = audit_history_for(by=user, actor_membership=stale_membership, report=reports[name])
        assert list(found) == [], name
    # The NCR this user raised stays readable.
    own = audit_history_for(by=user, actor_membership=stale_membership, report=reports["n4"])
    assert {entry.ncr_id for entry in own} == {reports["n4"].pk}


def test_audit_history_service_takes_company_from_membership_not_report(world):
    """A member of both companies reads a report's history only through that company."""
    user, membership_a = member(world["a"]["company"], "dual-audit@example.com", "quality")
    membership_b = Membership.objects.for_company(world["b"]["company"]).create(
        company=world["b"]["company"], user=user, role="originator"
    )
    own, foreign = world["reports"]["n1"], world["foreign"]
    assert list(audit_history_for(by=user, actor_membership=membership_a, report=own))
    assert list(audit_history_for(by=user, actor_membership=membership_a, report=foreign)) == []
    # In company B this user is only an originator who raised nothing.
    assert list(audit_history_for(by=user, actor_membership=membership_b, report=foreign)) == []
    assert list(audit_history_for(by=user, actor_membership=membership_b, report=own)) == []


@pytest.mark.parametrize(
    "zone,local_day,local_time",
    [("Africa/Johannesburg", "14", "00:30"), ("America/Los_Angeles", "13", "14:30")],
)
def test_created_time_is_shown_in_the_company_timezone(zone, local_day, local_time):
    setup = make_company("T", zone)
    with freeze_time("2026-11-13 22:30:00"):
        report = raise_ncr(setup, "orig1", "manager1", "Timezone probe")
        response = login(setup["people"]["orig1"][0]).get(detail_url(report))
    page = response.content.decode()
    created = re.search(r"<dt>Created</dt>\s*<dd>(.*?)</dd>", page, re.S)
    assert created, "the detail page should label the created time"
    assert local_time in created.group(1)
    assert re.search(rf"\b{local_day}\b", created.group(1)) and "November" in created.group(1)
    if local_time != "22:30":
        assert "22:30" not in created.group(1)
    # The audit history row for the creation uses the same local clock.
    history = page[page.index("Audit history") :]
    assert local_time in history


# 9. Escaping ----------------------------------------------------------------------------


def payload(number):
    return f"<script>alert({number})</script>"


def escaped(number):
    return f"&lt;script&gt;alert({number})&lt;/script&gt;"


def test_user_supplied_text_is_escaped_on_both_screens(company_a):
    users = get_user_model().objects
    users.filter(pk=company_a["people"]["orig1"][0].pk).update(full_name=payload(3))
    users.filter(pk=company_a["people"]["manager1"][0].pk).update(full_name=payload(4))
    users.filter(pk=company_a["people"]["quality"][0].pk).update(full_name=payload(6))
    report = raise_ncr(
        company_a,
        "orig1",
        "manager1",
        payload(1),
        description=payload(2),
        part_number=payload(7),
        job_number=payload(8),
    )
    add_audit(
        report,
        company_a["people"]["quality"][0],
        payload(5),
        old_value=payload(9),
        new_value=payload(10),
    )
    client = login(company_a["people"]["quality"][0])
    listing = client.get(list_url()).content.decode()
    assert "<script" not in listing
    for number in (1, 4):
        assert escaped(number) in listing, number
    detail = client.get(detail_url(report)).content.decode()
    assert "<script" not in detail
    for number in range(1, 11):
        assert escaped(number) in detail, number


def test_user_without_a_name_falls_back_to_escaped_email(company_a):
    get_user_model().objects.filter(pk=company_a["people"]["manager1"][0].pk).update(full_name="")
    report = raise_ncr(company_a, "orig1", "manager1", "Nameless manager")
    client = login(company_a["people"]["orig1"][0])
    assert b"manager1-a@example.com" in client.get(list_url()).content
    assert b"manager1-a@example.com" in client.get(detail_url(report)).content


# 10. Existing receipt and routing -------------------------------------------------------


def test_receipt_is_still_originator_only(world):
    report = world["reports"]["n1"]
    url = reverse("ncr:created", kwargs={"public_id": report.public_id})
    owner = login(world["a"]["people"]["orig1"][0]).get(url)
    assert owner.status_code == 200
    assert report.number.encode() in owner.content and SUBJECTS["n1"].encode() in owner.content
    assert detail_url(report).encode() in owner.content
    for key in ("orig2", "manager1", "manager2", "quality", "admin"):
        refused = login(world["a"]["people"][key][0]).get(url)
        assert refused.status_code == 404, key
        assert SUBJECTS["n1"].encode() not in refused.content
    for key in ROLE_KEYS:
        assert login(world["b"]["people"][key][0]).get(url).status_code == 404
    assert login(world["a"]["people"]["orig1"][0]).post(url).status_code == 405


def test_create_route_is_not_shadowed_and_still_creates(company_a):
    assert resolve("/ncrs/").view_name == "ncr:list"
    assert resolve("/ncrs/new/").view_name == "ncr:create"
    identifier = uuid.uuid4()
    assert resolve(f"/ncrs/{identifier}/").view_name == "ncr:detail"
    assert resolve(f"/ncrs/{identifier}/created/").view_name == "ncr:created"
    assert reverse("ncr:list") == "/ncrs/"
    assert reverse("ncr:detail", kwargs={"public_id": identifier}) == f"/ncrs/{identifier}/"

    user = company_a["people"]["orig1"][0]
    client = login(user)
    page = client.get("/ncrs/new/")
    assert page.status_code == 200 and "form" in page.context
    response = client.post(
        "/ncrs/new/",
        {
            "subject": "Raised through the screen",
            "description": "Still works",
            "classification": company_a["classification"].pk,
            "region": company_a["region"].pk,
            "discipline": company_a["discipline"].pk,
            "responsible_manager": company_a["people"]["manager1"][0].pk,
        },
    )
    report = NCR.objects.for_company(company_a["company"]).get()
    assert response.url == reverse("ncr:created", kwargs={"public_id": report.public_id})
    assert client.get(response.url).status_code == 200
    assert listed_ids(client.get(list_url())) == [report.public_id]
    detail = client.get(detail_url(report))
    assert detail.status_code == 200 and b"Raised through the screen" in detail.content
    assert len(detail.context["audit_entries"]) == 1


# 11. Query counts -----------------------------------------------------------------------


def query_count(client, url):
    client.get(url)  # settle session and one-off lookups first
    with CaptureQueriesContext(connection) as queries:
        assert client.get(url).status_code == 200
    return len(queries)


def test_list_query_count_does_not_grow_with_rows(company_a):
    due = date(2026, 12, 1)
    client = login(company_a["people"]["quality"][0])
    for sequence in (1, 2):
        raw_ncr(company_a, sequence, due)
    few = query_count(client, list_url())
    extra_classification = Classification.objects.for_company(company_a["company"]).create(
        company=company_a["company"], name="Second class", sla_days=1
    )
    for sequence in range(3, 25):
        report = raw_ncr(
            company_a,
            sequence,
            due,
            by="orig2" if sequence % 2 else "orig1",
            manager="manager2" if sequence % 3 else "manager1",
        )
        assert report.pk
    NCR.objects.for_company(company_a["company"]).filter(sequence_number__gt=20).update(
        review_started_at=timezone.now(), status="in_review"
    )
    assert extra_classification.pk
    many_response = client.get(list_url())
    assert len(listed_ids(many_response)) == 24
    assert query_count(client, list_url()) == few


def test_detail_query_count_does_not_grow_with_audit_entries(company_a):
    quiet = raise_ncr(company_a, "orig1", "manager1", "Quiet report")
    busy = raise_ncr(company_a, "orig1", "manager1", "Busy report")
    for index, key in enumerate(ROLE_KEYS * 3):
        add_audit(busy, company_a["people"][key][0], f"Entry {index}")
    client = login(company_a["people"]["quality"][0])
    assert len(client.get(detail_url(busy)).context["audit_entries"]) == 19
    assert query_count(client, detail_url(busy)) == query_count(client, detail_url(quiet))

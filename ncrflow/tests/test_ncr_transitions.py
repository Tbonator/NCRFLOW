"""Approved internal N2/N7/N8 actors, states, atomic history and HTTP boundaries."""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from apps.ncr.models import NCR
from apps.ncr.services import ncr_actions_for
from apps.tenants.models import AuditEntry, Membership
from tests.test_ncr_creation import create, setup_company

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    setup = setup_company("lifecycle")
    people = {
        "originator": (setup["user"], setup["member"]),
        "manager": (setup["manager"], setup["manager_member"]),
    }
    for key, role in (("quality", "quality"), ("admin", "company_admin"), ("other", "manager")):
        user = get_user_model().objects.create_user(f"{key}-lifecycle@example.com", "password")
        member = Membership.objects.for_company(setup["company"]).create(
            company=setup["company"], user=user, role=role
        )
        people[key] = user, member
    return setup, people, create(setup)


def set_state(report, state, actor):
    NCR.objects.for_company(report.company_id).filter(pk=report.pk).update(
        status=state,
        review_started_at=None if state == "open" else timezone.now(),
        closed_at=timezone.now() if state == "closed" else None,
        closed_by=actor if state == "closed" else None,
        cancelled_at=timezone.now() if state == "cancelled" else None,
        cancelled_by=actor if state == "cancelled" else None,
    )
    report.refresh_from_db()


def act(report, people, key, method, **extra):
    user, membership = people[key]
    return getattr(report, method)(by=user, actor_membership=membership, **extra)


def events(report):
    return AuditEntry.objects.for_company(report.company_id).filter(
        ncr=report, action="status_changed"
    )


@pytest.mark.parametrize(
    "method,source,target,allowed",
    [
        ("start_review", "open", "in_review", {"manager", "quality"}),
        ("close", "in_review", "closed", {"quality"}),
        ("cancel", "open", "cancelled", {"quality"}),
        ("cancel", "in_review", "cancelled", {"quality"}),
    ],
)
@pytest.mark.parametrize("key", ["originator", "manager", "quality", "admin", "other"])
@freeze_time("2026-11-15 10:00:00")
def test_transition_role_matrix(world, method, source, target, allowed, key):
    setup, people, report = world
    set_state(report, source, people["quality"][0])
    review_time = report.review_started_at
    if key not in allowed:
        with pytest.raises(PermissionDenied):
            act(report, people, key, method)
        report.refresh_from_db()
        assert report.status == source and not events(report).exists()
        return
    assert act(report, people, key, method) is report
    report.refresh_from_db()
    assert report.status == target
    entry = events(report).get()
    assert (entry.old_value, entry.new_value, entry.field_name) == (source, target, "status")
    assert entry.actor_user_id == people[key][0].pk
    assert entry.object_id == report.pk and entry.object_label == report.number
    if method == "start_review":
        assert report.review_started_at == timezone.now()
    elif method == "close":
        assert report.closed_at == timezone.now() and report.closed_by == people[key][0]
    else:
        assert report.cancelled_at == timezone.now() and report.cancelled_by == people[key][0]
        assert report.cancellation_reason == entry.comment == ""
    if method != "start_review":
        assert report.review_started_at == review_time


@pytest.mark.parametrize(
    "method,states",
    [
        ("start_review", ["in_review", "closed", "cancelled"]),
        ("close", ["open", "closed", "cancelled"]),
        ("cancel", ["closed", "cancelled"]),
    ],
)
def test_forbidden_states_and_stale_instances(world, method, states):
    _, people, report = world
    for state in states:
        set_state(report, state, people["quality"][0])
        report.status = "open" if method != "close" else "in_review"
        with pytest.raises(ValidationError):
            act(report, people, "quality", method)
        report.refresh_from_db()
        assert report.status == state and not events(report).exists()


@pytest.mark.parametrize("method", ["start_review", "close", "cancel"])
def test_foreign_company_and_forged_membership_refused(world, method):
    _, people, report = world
    foreign = setup_company("foreign-lifecycle")
    set_state(report, "in_review" if method == "close" else "open", people["quality"][0])
    for user, membership in (
        (foreign["user"], foreign["member"]),
        (people["quality"][0], foreign["member"]),
        (foreign["user"], people["quality"][1]),
        (people["quality"][0], None),
    ):
        with pytest.raises(PermissionDenied):
            getattr(report, method)(by=user, actor_membership=membership)
    assert not events(report).exists()


@pytest.mark.parametrize("change", ["role", "user", "membership", "company", "assignment"])
def test_start_review_reloads_current_authority(world, change):
    setup, people, report = world
    user, membership = people["manager"]
    if change == "role":
        Membership.objects.for_company(setup["company"]).filter(pk=membership.pk).update(
            role="originator"
        )
    elif change == "user":
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    elif change == "membership":
        Membership.objects.for_company(setup["company"]).filter(pk=membership.pk).update(
            active=False
        )
    elif change == "company":
        type(setup["company"]).objects.filter(pk=setup["company"].pk).update(active=False)
    else:
        NCR.objects.for_company(setup["company"]).filter(pk=report.pk).update(
            responsible_manager=people["other"][0]
        )
    with pytest.raises(PermissionDenied):
        report.start_review(by=user, actor_membership=membership)
    assert not events(report).exists()


@pytest.mark.parametrize("method", ["start_review", "close", "cancel"])
def test_audit_failure_rolls_back_transition_and_stamps(world, method):
    _, people, report = world
    set_state(report, "in_review" if method == "close" else "open", people["quality"][0])
    before = {field.attname: getattr(report, field.attname) for field in report._meta.fields}
    with patch.object(AuditEntry, "save", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError, match="audit unavailable"):
            act(report, people, "quality", method)
    report.refresh_from_db()
    assert before == {
        field.attname: getattr(report, field.attname) for field in report._meta.fields
    }
    assert not events(report).exists()


@pytest.mark.parametrize("method", ["close", "cancel"])
@pytest.mark.parametrize("change", ["role", "user", "membership", "company"])
def test_quality_terminal_actions_reload_current_authority(world, method, change):
    setup, people, report = world
    user, membership = people["quality"]
    set_state(report, "in_review", user)
    if change == "role":
        Membership.objects.for_company(setup["company"]).filter(pk=membership.pk).update(
            role="company_admin"
        )
    elif change == "user":
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    elif change == "membership":
        Membership.objects.for_company(setup["company"]).filter(pk=membership.pk).update(
            active=False
        )
    else:
        type(setup["company"]).objects.filter(pk=setup["company"].pk).update(active=False)
    with pytest.raises(PermissionDenied):
        getattr(report, method)(by=user, actor_membership=membership)
    report.refresh_from_db()
    assert report.status == "in_review" and not events(report).exists()


def test_platform_superuser_has_no_implicit_transition_access(world):
    _, people, report = world
    developer = get_user_model().objects.create_superuser("developer@example.com", "password")
    for membership in (None, people["quality"][1]):
        with pytest.raises(PermissionDenied):
            report.cancel(by=developer, actor_membership=membership)
    assert not events(report).exists()


def test_manager_cannot_start_review_just_because_they_raised_the_report(world):
    setup, people, _ = world
    setup["user"], setup["member"] = people["other"]
    report = create(setup)
    with pytest.raises(PermissionDenied):
        act(report, people, "other", "start_review")
    assert not events(report).exists()


def test_quality_can_act_after_manager_deactivation_and_reason_is_retained(world, mailoutbox):
    _, people, report = world
    get_user_model().objects.filter(pk=people["manager"][0].pk).update(is_active=False)
    act(report, people, "quality", "start_review")
    act(report, people, "quality", "cancel", reason="  Duplicate report  ")
    assert report.cancellation_reason == "Duplicate report"
    assert events(report).get(new_value="cancelled").comment == "Duplicate report"
    assert len(mailoutbox) == 0  # Delivery is staged for Phase 4.


def url(report, action):
    return reverse("ncr:transition", kwargs={"public_id": report.public_id, "action": action})


def logged_in(user, csrf=False):
    client = Client(enforce_csrf_checks=csrf)
    client.force_login(user)
    return client


@pytest.mark.parametrize("action", ["start-review", "close", "cancel"])
def test_transition_requires_post_and_csrf(world, action):
    _, people, report = world
    client = logged_in(people["quality"][0])
    for method in ("get", "put", "patch", "delete"):
        assert getattr(client, method)(url(report, action)).status_code == 405
    assert logged_in(people["quality"][0], csrf=True).post(url(report, action)).status_code == 403
    assert not events(report).exists()


def test_http_lifecycle_and_stale_double_submission(world):
    _, people, report = world
    client = logged_in(people["quality"][0], csrf=True)
    detail_url = reverse("ncr:detail", kwargs={"public_id": report.public_id})
    response = client.get(detail_url)
    assert b"Start review" in response.content and b"Cancel NCR" in response.content
    token = client.cookies["csrftoken"].value
    assert (
        client.post(url(report, "start-review"), {"csrfmiddlewaretoken": token}).status_code == 302
    )
    assert (
        client.post(url(report, "start-review"), {"csrfmiddlewaretoken": token}).status_code == 409
    )
    assert events(report).count() == 1
    assert (
        client.post(
            url(report, "close"), {"csrfmiddlewaretoken": token, "status": "open"}
        ).status_code
        == 302
    )
    report.refresh_from_db()
    assert report.status == "closed" and events(report).count() == 2
    final = client.get(detail_url)
    assert not final.context["ncr_actions"]
    assert b"Status changed" in final.content


def test_http_read_visibility_does_not_grant_write_authority(world):
    _, people, report = world
    for key in ("originator", "admin"):
        client = logged_in(people[key][0])
        for action in ("start-review", "close", "cancel"):
            assert client.post(url(report, action)).status_code == 403
    foreign = setup_company("foreign-http")
    assert logged_in(foreign["user"]).post(url(report, "cancel")).status_code == 404
    assert logged_in(people["quality"][0]).post(url(report, "unknown")).status_code == 404
    assert not events(report).exists()


@pytest.mark.parametrize(
    "key,expected",
    [
        ("originator", ()),
        ("admin", ()),
        ("other", ()),
        ("manager", ("start-review",)),
        ("quality", ("start-review", "cancel")),
    ],
)
def test_available_actions_follow_transition_roles(world, key, expected):
    _, people, report = world
    user, membership = people[key]
    assert ncr_actions_for(by=user, actor_membership=membership, report=report) == expected

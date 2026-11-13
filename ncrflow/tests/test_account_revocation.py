import json
from unittest.mock import patch

import pytest
from django.contrib.auth import SESSION_KEY, get_user_model, login
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts.services import set_account_active
from apps.tenants.context import CURRENT_MEMBERSHIP_SESSION_KEY, select_membership
from apps.tenants.models import AuditEntry, Company, Membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def developer():
    return get_user_model().objects.create_superuser("developer@example.com", "test-password")


@pytest.fixture
def account():
    user = get_user_model().objects.create_user("person@example.com", "test-password")
    company = Company.objects.create(name="Factory")
    membership = Membership.objects.for_company(company).create(
        company=company, user=user, role=Membership.Role.COMPANY_ADMIN
    )
    return user, company, membership


def test_idle_session_stays_revoked_after_account_is_reactivated(client, account, developer):
    user, _, _ = account
    client.force_login(user)
    assert client.get(reverse("tenants:workspace")).status_code == 200
    old_key = client.session.session_key
    set_account_active(by=developer, user_id=user.pk, active=False)
    set_account_active(by=developer, user_id=user.pk, active=True)
    # No request from this browser occurred during the inactive period.
    assert not Session.objects.filter(session_key=old_key).exists()
    assert client.get(reverse("tenants:workspace")).url.startswith(reverse("accounts:login"))
    response = client.post(
        reverse("accounts:login"),
        {
            "username": user.email,
            "password": "test-password",
        },
    )
    assert response.url == reverse("tenants:workspace")
    assert client.get(response.url).status_code == 200


def test_deactivation_removes_all_target_sessions_but_keeps_other_users(account, developer):
    user, _, _ = account
    first, second, other = Client(), Client(), Client()
    first.force_login(user)
    second.force_login(user)
    other.force_login(developer)
    keys = [first.session.session_key, second.session.session_key]
    other_key = other.session.session_key
    set_account_active(by=developer, user_id=user.pk, active=False)
    assert not Session.objects.filter(session_key__in=keys).exists()
    assert Session.objects.filter(session_key=other_key).exists()


def test_account_status_is_audited_in_each_membership_company(account, developer):
    user, company, _ = account
    other_company = Company.objects.create(name="Second Factory")
    Membership.objects.for_company(other_company).create(
        company=other_company, user=user, role=Membership.Role.QUALITY
    )
    set_account_active(by=developer, user_id=user.pk, active=False)
    for tenant in (company, other_company):
        entry = AuditEntry.objects.for_company(tenant).get(
            object_type="user", object_id=user.pk, field_name="is_active"
        )
        assert entry.actor_user_id == developer.pk
        assert json.loads(entry.old_value) is True
        assert json.loads(entry.new_value) is False
        assert other_company.name not in entry.new_value


def test_company_admin_cannot_change_global_account_status(account):
    user, _, _ = account
    with pytest.raises(PermissionDenied):
        set_account_active(by=user, user_id=user.pk, active=False)
    user.refresh_from_db()
    assert user.is_active


def test_status_service_rechecks_platform_privileges(account, developer):
    get_user_model().objects.filter(pk=developer.pk).update(is_staff=False, is_superuser=False)
    with pytest.raises(PermissionDenied):
        set_account_active(by=developer, user_id=account[0].pk, active=False)


def test_failed_audit_rolls_back_status_and_session_revocation(client, account, developer):
    user, _, _ = account
    client.force_login(user)
    key = client.session.session_key
    with patch.object(AuditEntry.objects, "for_company") as audit_scope:
        audit_scope.return_value.create.side_effect = RuntimeError("audit failed")
        with pytest.raises(RuntimeError):
            set_account_active(by=developer, user_id=user.pk, active=False)
    user.refresh_from_db()
    assert user.is_active
    assert Session.objects.filter(session_key=key).exists()
    assert client.get(reverse("tenants:workspace")).status_code == 200


def test_rejected_login_discards_staged_authentication(account):
    stale_user, _, _ = account
    get_user_model().objects.filter(pk=stale_user.pk).update(is_active=False)
    request = RequestFactory().post(reverse("accounts:login"))
    request.user = AnonymousUser()
    request.session = SessionStore()
    with pytest.raises(PermissionDenied):
        login(request, stale_user, backend="django.contrib.auth.backends.ModelBackend")
    assert SESSION_KEY not in request.session
    assert request.session.session_key is None
    assert all(
        session.get_decoded().get(SESSION_KEY) != str(stale_user.pk)
        for session in Session.objects.all()
    )


def test_login_persists_authentication_before_response_middleware(account):
    user, _, _ = account
    request = RequestFactory().post(reverse("accounts:login"))
    request.user = AnonymousUser()
    request.session = SessionStore()
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    persisted = Session.objects.get(session_key=request.session.session_key).get_decoded()
    assert persisted[SESSION_KEY] == str(user.pk)


def test_stale_company_switch_cannot_recreate_a_revoked_session(client, account, developer):
    user, _, membership = account
    client.force_login(user)
    request = RequestFactory().post(reverse("tenants:select"))
    request.user = user
    request.session = client.session
    dict(request.session)  # Cache authenticated state, as an in-flight request would.
    old_key = request.session.session_key
    set_account_active(by=developer, user_id=user.pk, active=False)
    set_account_active(by=developer, user_id=user.pk, active=True)
    with pytest.raises(PermissionDenied):
        select_membership(request, membership.pk)
    assert request.session.session_key is None
    assert SESSION_KEY not in request.session
    assert not Session.objects.filter(session_key=old_key).exists()


def test_selected_membership_is_persisted_before_response_middleware(client, account):
    user, _, membership = account
    client.force_login(user)
    request = RequestFactory().post(reverse("tenants:select"))
    request.user = user
    request.session = client.session
    old_key = request.session.session_key
    select_membership(request, membership.pk)
    assert request.session.session_key != old_key
    persisted = Session.objects.get(session_key=request.session.session_key).get_decoded()
    assert persisted[SESSION_KEY] == str(user.pk)
    assert persisted[CURRENT_MEMBERSHIP_SESSION_KEY] == membership.pk


def test_an_inflight_response_cannot_save_a_deleted_session(client, account, developer):
    user, _, _ = account
    client.force_login(user)
    staged = SessionStore(session_key=client.session.session_key)
    dict(staged)
    set_account_active(by=developer, user_id=user.pk, active=False)
    set_account_active(by=developer, user_id=user.pk, active=True)
    staged["old_request"] = "must not revive identity"
    with pytest.raises(UpdateError):
        staged.save()


def test_unsupported_session_backend_fails_before_status_changes(settings, account, developer):
    settings.SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
    with pytest.raises(ImproperlyConfigured, match="database session backend"):
        set_account_active(by=developer, user_id=account[0].pk, active=False)
    account[0].refresh_from_db()
    assert account[0].is_active

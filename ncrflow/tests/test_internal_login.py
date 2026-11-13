from unittest.mock import patch

import pytest
from django.contrib.auth import SESSION_KEY, get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.tenants.context import CURRENT_MEMBERSHIP_SESSION_KEY, select_membership
from apps.tenants.models import Company, Membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    user = get_user_model().objects.create_user(
        "person@example.com", "test-password", full_name="Test Person"
    )
    company = Company.objects.create(name="Factory A")
    membership = Membership.objects.for_company(company).create(
        company=company, user=user, role=Membership.Role.ORIGINATOR
    )
    return user, company, membership


@pytest.fixture
def second_membership(account):
    user, _, _ = account
    company = Company.objects.create(name="Factory B", timezone="Europe/London")
    return Membership.objects.for_company(company).create(
        company=company, user=user, role=Membership.Role.QUALITY
    )


@pytest.fixture
def foreign_membership():
    user = get_user_model().objects.create_user("other@example.com", "test-password")
    company = Company.objects.create(name="Private Other Factory")
    return Membership.objects.for_company(company).create(
        company=company, user=user, role=Membership.Role.COMPANY_ADMIN
    )


def sign_in(client, email="person@example.com", password="test-password", **extra):
    return client.post(
        reverse("accounts:login"),
        {
            "username": email,
            "password": password,
            **extra,
        },
    )


def test_sign_in_uses_email_and_selects_the_only_company(client, account):
    user, company, membership = account
    response = sign_in(client, email="PERSON@EXAMPLE.COM")
    assert response.url == reverse("tenants:workspace")
    assert client.session[SESSION_KEY] == str(user.pk)
    assert client.session[CURRENT_MEMBERSHIP_SESSION_KEY] == membership.pk
    page = client.get(response.url)
    assert page.status_code == 200
    assert page.wsgi_request.company.pk == company.pk
    assert page.wsgi_request.membership.pk == membership.pk
    assert b"Test Person" in page.content
    assert b"Originator" in page.content


@pytest.mark.parametrize("password", ["wrong-password", ""])
def test_wrong_credentials_do_not_create_an_authenticated_session(client, account, password):
    response = sign_in(client, password=password)
    assert response.status_code == 200
    assert response.context["form"].errors
    assert SESSION_KEY not in client.session
    assert CURRENT_MEMBERSHIP_SESSION_KEY not in client.session


def test_inactive_user_cannot_sign_in(client, account):
    user, _, _ = account
    get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    response = sign_in(client)
    assert response.status_code == 200
    assert SESSION_KEY not in client.session


def test_multiple_memberships_require_selection(client, account, second_membership):
    response = sign_in(client)
    assert response.url == reverse("tenants:select")
    assert CURRENT_MEMBERSHIP_SESSION_KEY not in client.session
    workspace = client.get(reverse("tenants:workspace"))
    assert workspace.url == reverse("tenants:select")
    assert workspace.wsgi_request.company is None


def test_selection_lists_only_own_active_companies(
    client, account, second_membership, foreign_membership
):
    sign_in(client)
    response = client.get(reverse("tenants:select"))
    assert response.status_code == 200
    assert b"Factory A" in response.content and b"Factory B" in response.content
    assert b"Private Other Factory" not in response.content
    assert b"other@example.com" not in response.content
    choices = response.context["form"].fields["membership"].queryset
    assert set(choices.values_list("pk", flat=True)) == {account[2].pk, second_membership.pk}


def test_selecting_and_switching_company_verifies_and_rotates_session(
    client, account, second_membership
):
    user, company, membership = account
    sign_in(client)
    old_session_key = client.session.session_key
    response = client.post(reverse("tenants:select"), {"membership": second_membership.pk})
    assert response.url == reverse("tenants:workspace")
    assert client.session.session_key != old_session_key
    assert client.session[SESSION_KEY] == str(user.pk)
    assert client.session[CURRENT_MEMBERSHIP_SESSION_KEY] == second_membership.pk
    workspace = client.get(response.url)
    assert workspace.wsgi_request.company.pk == second_membership.company_id
    assert b"Factory B" in workspace.content and b"Quality" in workspace.content
    assert company.name.encode() not in workspace.content
    client.post(reverse("tenants:select"), {"membership": membership.pk})
    assert client.get(reverse("tenants:workspace")).wsgi_request.company.pk == company.pk


def test_cannot_select_another_users_membership(client, account, foreign_membership):
    sign_in(client)
    response = client.post(reverse("tenants:select"), {"membership": foreign_membership.pk})
    assert response.status_code == 200
    assert response.context["form"].errors
    assert b"Private Other Factory" not in response.content
    assert client.session[CURRENT_MEMBERSHIP_SESSION_KEY] == account[2].pk


def test_cannot_select_a_colleagues_membership_in_same_company(client, account):
    _, company, membership = account
    colleague = get_user_model().objects.create_user("colleague@example.com")
    colleague_membership = Membership.objects.for_company(company).create(
        company=company, user=colleague, role=Membership.Role.COMPANY_ADMIN
    )
    sign_in(client)
    response = client.post(reverse("tenants:select"), {"membership": colleague_membership.pk})
    assert response.context["form"].errors
    assert client.session[CURRENT_MEMBERSHIP_SESSION_KEY] == membership.pk


@pytest.mark.parametrize("revoked", ["company", "membership"])
def test_inactive_companies_and_memberships_are_not_selectable(
    client, account, second_membership, revoked
):
    if revoked == "company":
        Company.objects.filter(pk=second_membership.company_id).update(active=False)
    else:
        Membership.objects.for_company(second_membership.company_id).filter(
            pk=second_membership.pk
        ).update(active=False)
    sign_in(client)
    response = client.get(reverse("tenants:select"))
    assert b"Factory B" not in response.content
    response = client.post(reverse("tenants:select"), {"membership": second_membership.pk})
    assert response.context["form"].errors
    assert client.session[CURRENT_MEMBERSHIP_SESSION_KEY] == account[2].pk


@pytest.mark.parametrize("value", [None, True, False, "bad-id", [], {}, -1, 2**100])
def test_malformed_session_selection_is_cleared_without_fallback(client, account, value):
    sign_in(client)
    session = client.session
    session[CURRENT_MEMBERSHIP_SESSION_KEY] = value
    session.save()
    response = client.get(reverse("tenants:workspace"))
    assert response.url == reverse("tenants:select")
    assert response.wsgi_request.company is None
    assert CURRENT_MEMBERSHIP_SESSION_KEY not in client.session


def test_foreign_membership_in_session_cannot_enter_that_company(
    client, account, foreign_membership
):
    sign_in(client)
    session = client.session
    session[CURRENT_MEMBERSHIP_SESSION_KEY] = foreign_membership.pk
    session.save()
    response = client.get(reverse("tenants:workspace"))
    assert response.url == reverse("tenants:select")
    assert response.wsgi_request.membership is None
    assert b"Private Other Factory" not in response.content


def test_query_parameters_cannot_change_company_or_role(client, account, foreign_membership):
    sign_in(client)
    response = client.get(
        reverse("tenants:workspace"),
        {
            "company_id": foreign_membership.company_id,
            "membership": foreign_membership.pk,
            "role": "company_admin",
        },
    )
    assert response.status_code == 200
    assert response.wsgi_request.company.pk == account[1].pk
    assert response.wsgi_request.membership.role == Membership.Role.ORIGINATOR
    assert b"Private Other Factory" not in response.content


def test_posted_company_and_role_cannot_override_selected_membership(
    client, account, foreign_membership
):
    sign_in(client)
    response = client.post(
        reverse("tenants:select"),
        {
            "membership": account[2].pk,
            "company_id": foreign_membership.company_id,
            "role": "company_admin",
            "next": "https://evil.example/",
        },
    )
    assert response.url == reverse("tenants:workspace")
    workspace = client.get(response.url)
    assert workspace.wsgi_request.membership.role == Membership.Role.ORIGINATOR
    assert workspace.wsgi_request.company.pk == account[1].pk


@pytest.mark.parametrize("revoked", ["company", "membership"])
def test_access_revocation_is_enforced_on_next_request(client, account, revoked):
    _, company, membership = account
    sign_in(client)
    if revoked == "company":
        Company.objects.filter(pk=company.pk).update(active=False)
    else:
        Membership.objects.for_company(company).filter(pk=membership.pk).update(active=False)
    response = client.get(reverse("tenants:workspace"))
    assert response.url == reverse("tenants:no-access")
    assert CURRENT_MEMBERSHIP_SESSION_KEY not in client.session
    page = client.get(response.url)
    assert page.status_code == 403
    assert company.name.encode() not in page.content


def test_user_deactivation_ends_session_even_if_later_reactivated(client, account):
    user, _, _ = account
    sign_in(client)
    get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    response = client.get(reverse("tenants:workspace"))
    assert response.url.startswith(reverse("accounts:login"))
    assert SESSION_KEY not in client.session
    assert CURRENT_MEMBERSHIP_SESSION_KEY not in client.session
    get_user_model().objects.filter(pk=user.pk).update(is_active=True)
    assert client.get(reverse("tenants:workspace")).url.startswith(reverse("accounts:login"))


def test_role_is_reloaded_each_request(client, account):
    _, company, membership = account
    sign_in(client)
    Membership.objects.for_company(company).filter(pk=membership.pk).update(role="manager")
    response = client.get(reverse("tenants:workspace"))
    assert response.wsgi_request.membership.role == Membership.Role.MANAGER
    assert b"Manager" in response.content


def test_selection_is_rechecked_after_form_validation(client, account, second_membership):
    sign_in(client)

    def revoke_then_select(request, membership_id):
        Membership.objects.for_company(second_membership.company_id).filter(
            pk=membership_id
        ).update(active=False)
        return select_membership(request, membership_id)

    with patch("apps.tenants.views.select_membership", side_effect=revoke_then_select):
        response = client.post(reverse("tenants:select"), {"membership": second_membership.pk})
    assert response.status_code == 403
    assert client.session.get(CURRENT_MEMBERSHIP_SESSION_KEY) != second_membership.pk


def test_user_without_memberships_gets_friendly_denial(client):
    get_user_model().objects.create_user("person@example.com", "test-password")
    response = sign_in(client)
    assert response.url == reverse("tenants:no-access")
    page = client.get(response.url)
    assert page.status_code == 403
    assert b"No company available" in page.content
    assert b"company administrator" in page.content


@pytest.mark.parametrize("view", ["tenants:workspace", "tenants:select", "tenants:no-access"])
def test_anonymous_users_cannot_enter_internal_pages(client, view):
    response = client.get(reverse(view))
    assert response.url.startswith(reverse("accounts:login"))


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/admin/"])
def test_login_does_not_follow_posted_redirect_targets(client, account, target):
    assert sign_in(client, next=target).url == reverse("tenants:workspace")


def test_logout_requires_post_and_clears_identity_and_company(client, account):
    sign_in(client)
    assert client.get(reverse("accounts:logout")).status_code == 405
    assert SESSION_KEY in client.session
    response = client.post(reverse("accounts:logout"))
    assert response.url == reverse("home")
    assert (
        SESSION_KEY not in client.session and CURRENT_MEMBERSHIP_SESSION_KEY not in client.session
    )
    assert client.get(reverse("tenants:workspace")).url.startswith(reverse("accounts:login"))


def test_login_requires_csrf_token(account):
    client = Client(enforce_csrf_checks=True)
    assert sign_in(client).status_code == 403
    client.get(reverse("accounts:login"))
    token = client.cookies["csrftoken"].value
    assert sign_in(client, csrfmiddlewaretoken=token).url == reverse("tenants:workspace")


def test_company_selection_and_logout_require_csrf(account, second_membership):
    client = Client(enforce_csrf_checks=True)
    client.force_login(account[0])
    assert (
        client.post(reverse("tenants:select"), {"membership": second_membership.pk}).status_code
        == 403
    )
    assert client.post(reverse("accounts:logout")).status_code == 403
    client.get(reverse("tenants:select"))
    token = client.cookies["csrftoken"].value
    response = client.post(
        reverse("tenants:select"),
        {
            "membership": second_membership.pk,
            "csrfmiddlewaretoken": token,
        },
    )
    assert response.url == reverse("tenants:workspace")


def test_private_workspace_is_not_cacheable(client, account):
    sign_in(client)
    response = client.get(reverse("tenants:workspace"))
    assert "no-store" in response["Cache-Control"]
    assert "private" in response["Cache-Control"]


def test_timezone_applies_only_during_the_selected_company_request(client, account):
    sign_in(client)
    observed = []
    from django.shortcuts import render

    def observe_zone(*args, **kwargs):
        observed.append(timezone.get_current_timezone_name())
        return render(*args, **kwargs)

    with patch("apps.tenants.views.render", side_effect=observe_zone):
        client.get(reverse("tenants:workspace"))
    assert observed == ["Africa/Johannesburg"]
    assert timezone.get_current_timezone_name() == "UTC"


@pytest.mark.parametrize("role", Membership.Role.values)
def test_each_company_role_can_enter_its_own_workspace(client, account, role):
    _, company, membership = account
    Membership.objects.for_company(company).filter(pk=membership.pk).update(role=role)
    sign_in(client)
    assert client.get(reverse("tenants:workspace")).status_code == 200


def test_platform_superuser_uses_admin_without_automatic_tenant_access(client, foreign_membership):
    developer = get_user_model().objects.create_superuser("developer@example.com", "test-password")
    client.force_login(developer)
    assert client.get(reverse("admin:index")).status_code == 200
    assert client.get(reverse("tenants:workspace")).url == reverse("tenants:no-access")
    page = client.get(reverse("tenants:no-access"))
    assert b"Private Other Factory" not in page.content
    assert b"Open platform admin" in page.content


def test_company_admin_is_not_platform_staff(client, account):
    _, company, membership = account
    Membership.objects.for_company(company).filter(pk=membership.pk).update(role="company_admin")
    sign_in(client)
    response = client.get(reverse("admin:index"))
    assert response.url.startswith(reverse("admin:login"))


def test_company_name_is_escaped_in_workspace(client, account):
    _, company, _ = account
    Company.objects.filter(pk=company.pk).update(name="<script>alert(1)</script>")
    sign_in(client)
    response = client.get(reverse("tenants:workspace"))
    assert b"<script>alert(1)</script>" not in response.content
    assert b"&lt;script&gt;" in response.content

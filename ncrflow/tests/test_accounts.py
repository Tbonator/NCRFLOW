"""Check the global identity before adding company membership permissions."""

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

pytestmark = pytest.mark.django_db


def test_email_is_the_login_and_password_is_hashed():
    user = get_user_model().objects.create_user(
        "  Person@Example.COM  ", "test-password", full_name="Test Person"
    )
    user.refresh_from_db()
    assert user.email == "person@example.com"
    assert user.password != "test-password"
    assert user.check_password("test-password")
    assert not user.is_staff
    assert not user.is_superuser
    assert user.created_at is not None
    assert user.updated_at is not None
    assert authenticate(username="PERSON@EXAMPLE.COM", password="test-password") == user
    assert authenticate(email="person@example.com", password="incorrect") is None


def test_email_duplicates_are_rejected_after_normalisation():
    users = get_user_model().objects
    users.create_user("person@example.com", "test-password")
    with pytest.raises(IntegrityError), transaction.atomic():
        users.create_user("PERSON@EXAMPLE.COM", "other-password")


@pytest.mark.parametrize("email", ["", "   ", None])
def test_email_is_required(email):
    with pytest.raises(ValueError, match="email address is required"):
        get_user_model().objects.create_user(email, "test-password")


def test_invalid_email_is_refused():
    with pytest.raises(ValidationError):
        get_user_model().objects.create_user("not-an-email", "test-password")


def test_passwordless_account_has_an_unusable_password():
    user = get_user_model().objects.create_user("person@example.com")
    assert not user.has_usable_password()


def test_superuser_can_be_created_for_platform_administration():
    user = get_user_model().objects.create_superuser("DEV@EXAMPLE.COM", "test-password")
    assert user.email == "dev@example.com"
    assert user.is_active and user.is_staff and user.is_superuser
    assert user.has_perm("accounts.change_user")


@pytest.mark.parametrize("field", ["is_staff", "is_superuser", "is_active"])
def test_superuser_flags_cannot_be_disabled_at_creation(field):
    with pytest.raises(ValueError, match=field):
        get_user_model().objects.create_superuser(
            "dev@example.com", "test-password", **{field: False}
        )


def test_superuser_requires_a_password():
    with pytest.raises(ValueError, match="password is required"):
        get_user_model().objects.create_superuser("dev@example.com")


def test_inactive_user_cannot_authenticate():
    get_user_model().objects.create_user("person@example.com", "test-password", is_active=False)
    assert authenticate(email="person@example.com", password="test-password") is None


def test_database_rejects_uppercase_email_when_save_is_bypassed():
    user = get_user_model().objects.create_user("person@example.com", "test-password")
    with pytest.raises(IntegrityError), transaction.atomic():
        get_user_model().objects.filter(pk=user.pk).update(email="UPPER@EXAMPLE.COM")


@pytest.mark.parametrize("field", ["is_staff", "is_superuser"])
def test_database_rejects_mismatched_platform_flags(field):
    user = get_user_model().objects.create_user("person@example.com", "test-password")
    with pytest.raises(IntegrityError), transaction.atomic():
        get_user_model().objects.filter(pk=user.pk).update(**{field: True})


def test_normal_user_has_no_platform_permissions():
    user = get_user_model().objects.create_user("person@example.com", "test-password")
    assert not user.has_perm("accounts.change_user")
    assert not user.has_module_perms("accounts")

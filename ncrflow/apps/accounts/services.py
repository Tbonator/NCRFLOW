"""Platform account status changes and coordinated database-session persistence."""

import json

from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model, logout
from django.contrib.sessions.models import Session
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.tenants.models import AuditEntry, Membership

from .forms import AccountStatusForm


def require_database_sessions():
    if settings.SESSION_ENGINE != "django.contrib.sessions.backends.db":
        raise ImproperlyConfigured("Account revocation requires the database session backend.")


def persist_authenticated_session(request, user):
    """Cover Django internal and admin logins through user_logged_in."""
    require_database_sessions()
    denied = False
    with transaction.atomic():
        current = get_user_model().objects.select_for_update().filter(pk=user.pk).first()
        if current is None or not current.is_active:
            # Django has already staged auth keys before this signal is sent.
            # Commit their removal before raising, so middleware cannot save them.
            logout(request)
            denied = True
        else:
            request.session.save()
    if denied:
        raise PermissionDenied("This account cannot sign in.")


@transaction.atomic
def set_account_active(*, by, user_id, active):
    """Change a global account's status; never use a company role for this."""
    require_database_sessions()
    form = AccountStatusForm({"user_id": user_id, "active": active})
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    user_id, active = form.cleaned_data["user_id"], form.cleaned_data["active"]
    users = {
        user.pk: user
        for user in get_user_model()
        # Serialize status changes with login without blocking audit FK key-share locks.
        .objects.select_for_update(no_key=True)
        .filter(pk__in=[getattr(by, "pk", None), user_id])
        .order_by("pk")
    }
    actor = users.get(getattr(by, "pk", None))
    if actor is None or not (actor.is_active and actor.is_staff and actor.is_superuser):
        raise PermissionDenied("Only the platform developer may change global account status.")
    user = users.get(user_id)
    if user is None:
        raise get_user_model().DoesNotExist("Account not found.")

    if user.is_active != active:
        # Global status changes propagate history to this user's companies only.
        # Audit first so self-deactivation still records an active platform actor.
        company_ids = Membership.objects.filter(user_id=user.pk).values_list(
            "company_id", flat=True
        )
        for company_id in company_ids:
            AuditEntry.objects.for_company(company_id).create(
                company_id=company_id,
                actor_user=actor,
                object_type="user",
                object_id=user.pk,
                object_label=user.email[:100],
                action=AuditEntry.Action.FIELD_CHANGED,
                field_name="is_active",
                old_value=json.dumps(user.is_active),
                new_value=json.dumps(active),
            )
        user.is_active = active
        user.save(update_fields=["is_active", "updated_at"])

    if not active:
        # Framework sessions are global identity infrastructure, not tenant data.
        # Login and company-switch session creation hold the same User row lock.
        session_keys = [
            session.session_key
            for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator()
            if session.get_decoded().get(SESSION_KEY) == str(user.pk)
        ]
        Session.objects.filter(session_key__in=session_keys).delete()
    return user

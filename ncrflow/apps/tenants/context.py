"""Verified membership resolution; the narrow tenant-bootstrap exception."""

from django.contrib.auth import SESSION_KEY, get_user_model, logout
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .models import Membership

CURRENT_MEMBERSHIP_SESSION_KEY = "current_membership_id"


def eligible_memberships(user):
    if not user.is_authenticated or not user.is_active:
        return Membership.objects.none()
    # Before a tenant exists, enumerate only this authenticated user's memberships.
    return (
        Membership.objects.filter(
            user_id=user.pk, user__is_active=True, active=True, company__active=True
        )
        .select_related("company")
        .order_by("company__name", "pk")
    )


def membership_from_session(user, membership_id):
    if (
        not isinstance(membership_id, int)
        or isinstance(membership_id, bool)
        or not 0 < membership_id <= 2**63 - 1
    ):
        return None
    return eligible_memberships(user).filter(pk=membership_id).first()


def select_membership(request, membership_id):
    from apps.accounts.services import require_database_sessions

    require_database_sessions()
    denied = False
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().filter(pk=request.user.pk).first()
        stored = Session.objects.filter(
            session_key=request.session.session_key, expire_date__gt=timezone.now()
        ).first()
        if (
            user is None
            or not user.is_active
            or stored is None
            or stored.get_decoded().get(SESSION_KEY) != str(user.pk)
        ):
            # Never copy a deleted session's cached auth dictionary into a new key.
            logout(request)
            denied = True
        else:
            # A form selection remains only a candidate until this fresh check.
            membership = membership_from_session(user, membership_id)
            if membership is None:
                raise PermissionDenied("This company is not available to your account.")
            request.session.cycle_key()
            request.session[CURRENT_MEMBERSHIP_SESSION_KEY] = membership.pk
            request.session.save()
            request.membership = membership
            request.company = membership.company
    if denied:
        request.membership = None
        request.company = None
        raise PermissionDenied("Your session has ended. Sign in again.")
    return membership

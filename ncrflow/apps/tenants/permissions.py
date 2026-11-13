from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from .models import Company, Membership


def require_company_role(*, by, actor_membership, role, lock=False):
    """Reload company/account/role authority, locking company then membership for writes."""
    companies = Company.objects.all()
    if lock:
        companies = companies.select_for_update()
    company = companies.filter(
        pk=getattr(actor_membership, "company_id", None), active=True
    ).first()
    actor = get_user_model().objects.filter(pk=getattr(by, "pk", None), is_active=True).first()
    if company is None or actor is None:
        raise PermissionDenied("An active company membership with the required role is needed.")
    memberships = Membership.objects.for_company(company)
    if lock:
        memberships = memberships.select_for_update()
    memberships = memberships.filter(
        pk=getattr(actor_membership, "pk", None), user_id=actor.pk, active=True
    )
    if role is not None:
        memberships = memberships.filter(role=role)
    membership = memberships.first()
    if membership is None:
        raise PermissionDenied("An active company membership with the required role is needed.")
    return company, actor

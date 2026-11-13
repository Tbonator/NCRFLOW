from django.conf import settings
from django.contrib import messages
from django.contrib.auth import SESSION_KEY, logout
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.cache import patch_cache_control

from .context import (
    CURRENT_MEMBERSHIP_SESSION_KEY,
    eligible_memberships,
    membership_from_session,
)


class TenantMiddleware:
    public_views = {
        "home",
        "accounts:login",
        "accounts:logout",
        "tenants:select",
        "tenants:no-access",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.membership = None
        request.company = None
        has_selection = CURRENT_MEMBERSHIP_SESSION_KEY in request.session
        selected = request.session.get(CURRENT_MEMBERSHIP_SESSION_KEY)
        if request.user.is_authenticated:
            if has_selection:
                request.membership = membership_from_session(request.user, selected)
                if request.membership is None:
                    request.session.pop(CURRENT_MEMBERSHIP_SESSION_KEY, None)
                    messages.warning(
                        request, "Your company access has changed. Choose an available company."
                    )
            else:
                memberships = list(eligible_memberships(request.user)[:2])
                if len(memberships) == 1:
                    request.membership = memberships[0]
                    request.session[CURRENT_MEMBERSHIP_SESSION_KEY] = request.membership.pk
            if request.membership is not None:
                request.company = request.membership.company
        elif SESSION_KEY in request.session:
            # An inactive/deleted user must not regain this session on reactivation.
            logout(request)
            messages.warning(request, "Your session has ended. Sign in again.")
        elif has_selection:
            request.session.pop(CURRENT_MEMBERSHIP_SESSION_KEY, None)

        zone = request.company.timezone if request.company else None
        with timezone.override(zone):
            response = self.get_response(request)
        if request.user.is_authenticated:
            patch_cache_control(response, private=True, no_store=True, no_cache=True)
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        if match.view_name in self.public_views or match.app_name == "admin":
            return None
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if request.membership is None:
            if eligible_memberships(request.user).exists():
                return redirect("tenants:select")
            return redirect("tenants:no-access")
        return None

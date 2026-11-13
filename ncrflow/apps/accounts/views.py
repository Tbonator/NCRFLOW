from django.contrib.auth.views import LoginView
from django.urls import reverse

from apps.tenants.context import CURRENT_MEMBERSHIP_SESSION_KEY, eligible_memberships

from .forms import EmailAuthenticationForm


class InternalLoginView(LoginView):
    authentication_form = EmailAuthenticationForm
    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        self.request.session.pop(CURRENT_MEMBERSHIP_SESSION_KEY, None)
        self.request.membership = None
        self.request.company = None
        return super().form_valid(form)

    def get_success_url(self):
        # Always resolve tenant access before proceeding, ignoring posted next URLs.
        if getattr(self.request, "membership", None) is not None:
            return reverse("tenants:workspace")
        memberships = list(eligible_memberships(self.request.user)[:2])
        if len(memberships) == 1:
            self.request.session[CURRENT_MEMBERSHIP_SESSION_KEY] = memberships[0].pk
            return reverse("tenants:workspace")
        if memberships:
            return reverse("tenants:select")
        return reverse("tenants:no-access")

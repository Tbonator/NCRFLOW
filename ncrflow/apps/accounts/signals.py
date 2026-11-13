from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from .services import persist_authenticated_session


@receiver(user_logged_in, dispatch_uid="accounts.persist_authenticated_session")
def persist_login_session(sender, request, user, **kwargs):
    persist_authenticated_session(request, user)

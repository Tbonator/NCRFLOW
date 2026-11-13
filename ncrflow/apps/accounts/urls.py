from django.contrib.auth.views import LogoutView
from django.urls import path

from .views import InternalLoginView

app_name = "accounts"

urlpatterns = [
    path("login/", InternalLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(next_page="home"), name="logout"),
]

from django import forms
from django.contrib.auth.forms import AuthenticationForm


class EmailAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(
        label="Email address",
        max_length=254,
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "username"}),
    )

    error_messages = {
        "invalid_login": "The email address or password is incorrect.",
        "inactive": "This account cannot sign in. Contact your company administrator.",
    }


class AccountStatusForm(forms.Form):
    user_id = forms.IntegerField(min_value=1, max_value=2**63 - 1)
    active = forms.TypedChoiceField(
        choices=[(True, "Active"), (False, "Inactive")], coerce=lambda value: value == "True"
    )

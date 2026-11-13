from django import forms
from django.contrib.auth import get_user_model, password_validation

from .context import eligible_memberships
from .models import Company, Membership


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = ["name", "timezone", "escalation_tier_one_days", "escalation_tier_two_days"]


class MemberProvisionForm(forms.Form):
    email = forms.EmailField(max_length=254)
    full_name = forms.CharField(max_length=200, required=False)
    role = forms.ChoiceField(choices=Membership.Role.choices)
    password = forms.CharField(required=False, strip=False, widget=forms.PasswordInput)

    def clean_email(self):
        return get_user_model().objects.normalize_email(self.cleaned_data["email"])

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            user = get_user_model()(
                email=cleaned.get("email", ""), full_name=cleaned.get("full_name", "")
            )
            password_validation.validate_password(password, user=user)
        return cleaned


class MembershipChangeForm(forms.Form):
    role = forms.ChoiceField(choices=Membership.Role.choices)
    active = forms.TypedChoiceField(
        choices=[(True, "Active"), (False, "Inactive")], coerce=lambda x: x == "True"
    )


class MembershipChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, membership):
        return f"{membership.company.name} — {membership.get_role_display()}"


class CompanySelectionForm(forms.Form):
    membership = MembershipChoiceField(
        label="Company", queryset=Membership.objects.none(), empty_label="Choose a company"
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["membership"].queryset = eligible_memberships(user)

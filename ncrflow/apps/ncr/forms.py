from django import forms
from django.contrib.auth import get_user_model

from apps.tenants.models import Discipline, Membership, Region

from .models import NCR, Classification


class ManagerChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        return user.get_full_name() or user.email


class NCRCreateForm(forms.Form):
    subject = forms.CharField(max_length=200)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 5}))
    classification = forms.ModelChoiceField(queryset=Classification.objects.none())
    region = forms.ModelChoiceField(queryset=Region.objects.none())
    discipline = forms.ModelChoiceField(queryset=Discipline.objects.none())
    responsible_manager = ManagerChoiceField(queryset=get_user_model().objects.none())
    part_number = forms.CharField(max_length=100, required=False)
    job_number = forms.CharField(max_length=100, required=False)

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        for field, model in (
            ("classification", Classification),
            ("region", Region),
            ("discipline", Discipline),
        ):
            self.fields[field].queryset = (
                model.objects.for_company(company).filter(active=True).order_by("name", "pk")
            )
        managers = (
            Membership.objects.for_company(company)
            .filter(active=True, role=Membership.Role.MANAGER, user__is_active=True)
            .values("user_id")
        )
        self.fields["responsible_manager"].queryset = (
            get_user_model()
            .objects.filter(pk__in=managers, is_active=True)
            .order_by("full_name", "email")
        )


class NCRStatusFilterForm(forms.Form):
    status = forms.ChoiceField(choices=[("", "All statuses"), *NCR.Status.choices], required=False)

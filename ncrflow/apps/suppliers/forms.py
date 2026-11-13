from django import forms
from django.db.models import Q

from apps.tenants.models import Region

from .models import Supplier, SupplierContact


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "region", "active"]

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        # Only this company's active regions, plus the supplier's current one if inactive.
        self.fields["region"].queryset = (
            Region.objects.for_company(company)
            .filter(Q(active=True) | Q(pk=self.instance.region_id))
            .order_by("name", "pk")
        )
        self.fields["region"].empty_label = "No region"


class SupplierContactForm(forms.ModelForm):
    class Meta:
        model = SupplierContact
        fields = ["name", "email", "active"]
        labels = {"name": "Name (optional)"}

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class ActiveStatusForm(forms.Form):
    active = forms.TypedChoiceField(
        choices=[(True, "Active"), (False, "Inactive")], coerce=lambda value: value == "True"
    )

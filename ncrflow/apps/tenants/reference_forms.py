from django import forms

from apps.ncr.models import Classification

from .models import Discipline, Region


class RegionForm(forms.ModelForm):
    class Meta:
        model = Region
        fields = ["name", "active"]


class DisciplineForm(forms.ModelForm):
    class Meta:
        model = Discipline
        fields = ["name", "active"]


class ClassificationForm(forms.ModelForm):
    class Meta:
        model = Classification
        fields = ["name", "sla_days", "active"]
        labels = {"sla_days": "SLA days (calendar days)"}


class ReferenceStatusForm(forms.Form):
    active = forms.TypedChoiceField(
        choices=[(True, "Active"), (False, "Inactive")], coerce=lambda value: value == "True"
    )

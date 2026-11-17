from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.suppliers.models import Supplier
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


class ExternalNCRCreateForm(NCRCreateForm):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none())

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, company=company, **kwargs)
        self.fields["supplier"].queryset = (
            Supplier.objects.for_company(company).filter(active=True).order_by("name", "pk")
        )
        self.order_fields(
            (
                "subject",
                "description",
                "supplier",
                "classification",
                "region",
                "discipline",
                "responsible_manager",
                "part_number",
                "job_number",
            )
        )


class NCRStatusFilterForm(forms.Form):
    status = forms.ChoiceField(choices=[("", "All statuses"), *NCR.Status.choices], required=False)


class NCRTransitionForm(forms.Form):
    reason = forms.CharField(
        label="Cancellation reason (optional)", required=False, widget=forms.Textarea
    )


DESCRIPTIVE_FIELDS = ("subject", "description", "part_number", "job_number")
QUALITY_EDIT_FIELDS = ("region", "discipline", "responsible_manager")


class NCREditForm(forms.Form):
    subject = forms.CharField(max_length=200)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 5}))
    part_number = forms.CharField(max_length=100, required=False)
    job_number = forms.CharField(max_length=100, required=False)
    region = forms.ModelChoiceField(queryset=Region.objects.none())
    discipline = forms.ModelChoiceField(queryset=Discipline.objects.none())
    responsible_manager = ManagerChoiceField(queryset=get_user_model().objects.none())
    revision = forms.DateTimeField(widget=forms.HiddenInput)

    def __init__(self, *args, company, report, editable_fields, **kwargs):
        allowed = tuple(editable_fields)
        if not set(allowed) <= set(DESCRIPTIVE_FIELDS + QUALITY_EDIT_FIELDS):
            raise ValueError("Unknown NCR edit field.")
        initial = {
            field: getattr(report, f"{field}_id" if field in QUALITY_EDIT_FIELDS else field)
            for field in allowed
        }
        initial["revision"] = report.updated_at.isoformat()
        super().__init__(*args, initial=initial, **kwargs)
        for field in tuple(self.fields):
            if field not in (*allowed, "revision"):
                self.fields.pop(field)
        for field, model in (("region", Region), ("discipline", Discipline)):
            if field in self.fields:
                self.fields[field].queryset = (
                    model.objects.for_company(company)
                    .filter(Q(active=True) | Q(pk=getattr(report, f"{field}_id")))
                    .order_by("name", "pk")
                )
        if "responsible_manager" in self.fields:
            managers = (
                Membership.objects.for_company(company)
                .filter(
                    Q(active=True, role=Membership.Role.MANAGER, user__is_active=True)
                    | Q(user_id=report.responsible_manager_id)
                )
                .values("user_id")
            )
            self.fields["responsible_manager"].queryset = (
                get_user_model().objects.filter(pk__in=managers).order_by("full_name", "email")
            )
        for field in QUALITY_EDIT_FIELDS:
            if field in self.fields:
                self.fields[
                    field
                ].help_text = (
                    "A current inactive entry can be retained. Choose an active entry to change it."
                )

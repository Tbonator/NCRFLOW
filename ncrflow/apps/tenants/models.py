"""Company ownership, memberships and append-only business history."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Lower


def validate_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValidationError("Choose a valid IANA timezone.") from error


class RetainedQuerySet(models.QuerySet):
    def delete(self):
        raise ValidationError("Business records must be deactivated, not deleted.")


class CompanyQuerySet(RetainedQuerySet):
    pass


class Company(models.Model):
    name = models.CharField(max_length=200)
    timezone = models.CharField(
        max_length=64, default="Africa/Johannesburg", validators=[validate_timezone]
    )
    escalation_tier_one_days = models.IntegerField(default=3, validators=[MinValueValidator(1)])
    escalation_tier_two_days = models.IntegerField(default=7)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = CompanyQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(escalation_tier_one_days__gte=1),
                name="tenants_company_tier_one_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    escalation_tier_two_days__gt=models.F("escalation_tier_one_days")
                ),
                name="tenants_company_tier_two_greater",
            ),
        ]

    def delete(self, *args, **kwargs):
        raise ValidationError("Companies must be deactivated, not deleted.")

    def __str__(self):
        return self.name


class TenantQuerySet(RetainedQuerySet):
    def for_company(self, company):
        company_id = getattr(company, "pk", company)
        if company_id is None:
            raise ValueError("A saved company is required.")
        return self.filter(company_id=company_id)


class TenantModel(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):
        raise ValidationError("Business records must be deactivated, not deleted.")


class Region(TenantModel):
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint("company", Lower("name"), name="tenants_region_name_unique"),
            models.UniqueConstraint(fields=["company", "id"], name="tenants_region_tenant_id"),
        ]

    def __str__(self):
        return self.name


class Discipline(TenantModel):
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "company", Lower("name"), name="tenants_discipline_name_unique"
            ),
            models.UniqueConstraint(fields=["company", "id"], name="tenants_discipline_tenant_id"),
        ]

    def __str__(self):
        return self.name


class NumberSequence(TenantModel):
    sequence_name = models.CharField(
        max_length=20, choices=[("ncr", "NCR"), ("supplier_ncr", "SNCR")]
    )
    last_number = models.IntegerField(default=0, validators=[MinValueValidator(0)])

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "sequence_name"], name="tenants_sequence_series_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(sequence_name__in=["ncr", "supplier_ncr"]),
                name="tenants_sequence_name_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(last_number__gte=0), name="tenants_sequence_nonnegative"
            ),
        ]


class Membership(TenantModel):
    class Role(models.TextChoices):
        ORIGINATOR = "originator", "Originator"
        MANAGER = "manager", "Manager"
        QUALITY = "quality", "Quality"
        COMPANY_ADMIN = "company_admin", "Company admin"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    role = models.CharField(max_length=20, choices=Role.choices)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "user"], name="tenants_membership_company_user_unique"
            ),
            models.UniqueConstraint(
                fields=["company", "id"], name="tenants_membership_company_id_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=["originator", "manager", "quality", "company_admin"]),
                name="tenants_membership_role_valid",
            ),
        ]
        indexes = [models.Index(fields=["user", "active"], name="tenants_member_user_active")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = (
                type(self)
                .objects.for_company(self.company_id)
                .filter(pk=self.pk, user_id=self.user_id)
            )
            if not stored.exists():
                raise ValidationError("A membership's company and user cannot change.")
        return super().save(*args, **kwargs)


class AuditQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Audit entries are append-only.")

    def bulk_update(self, *args, **kwargs):
        raise ValidationError("Audit entries are append-only.")


class AuditEntry(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        STATUS_CHANGED = "status_changed", "Status changed"
        REVIEW_DECISION = "review_decision", "Review decision"
        FIELD_CHANGED = "field_changed", "Field changed"

    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True
    )
    ncr = models.ForeignKey(
        "ncr.NCR", on_delete=models.PROTECT, null=True, blank=True, related_name="audit_entries"
    )
    object_type = models.CharField(max_length=50)
    object_id = models.BigIntegerField()
    object_label = models.CharField(max_length=100, blank=True, default="")
    action = models.CharField(max_length=30, choices=Action.choices)
    field_name = models.CharField(max_length=50, blank=True, default="")
    old_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)
    comment = models.TextField(blank=True, default="")

    objects = AuditQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(
                fields=["company", "object_type", "object_id"],
                name="tenants_audit_object_history",
            ),
            models.Index(fields=["company", "ncr", "id"], name="tenants_audit_ncr_history"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    action__in=["created", "status_changed", "review_decision", "field_changed"]
                ),
                name="tenants_audit_action_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    object_type__in=[
                        "company",
                        "user",
                        "membership",
                        "audit_entry",
                        "region",
                        "discipline",
                        "classification",
                        "number_sequence",
                        "ncr",
                        "supplier",
                        "supplier_contact",
                        "supplier_response",
                        "evidence",
                        "notification",
                        "escalation_run",
                        "quality_route",
                        "supplier_ncr",
                    ]
                ),
                name="tenants_audit_object_type_valid",
            ),
            models.UniqueConstraint(
                fields=["company", "id"], name="tenants_audit_company_id_unique"
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Audit entries are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit entries are append-only.")

import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from apps.tenants.models import TenantModel


class Classification(TenantModel):
    name = models.CharField(max_length=100)
    sla_days = models.IntegerField(validators=[MinValueValidator(0)])
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "company", Lower("name"), name="ncr_classification_name_unique"
            ),
            models.UniqueConstraint(fields=["company", "id"], name="ncr_classification_tenant_id"),
            models.CheckConstraint(
                condition=models.Q(sla_days__gte=0), name="ncr_classification_sla_nonnegative"
            ),
        ]

    def __str__(self):
        return self.name


class NCR(TenantModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_REVIEW = "in_review", "In Review"
        AWAITING_SUPPLIER = "awaiting_supplier", "Awaiting Supplier"
        AWAITING_INTERNAL_REVIEW = "awaiting_internal_review", "Awaiting Internal Review"
        CLOSED = "closed", "Closed"
        CANCELLED = "cancelled", "Cancelled"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    sequence_number = models.IntegerField(validators=[MinValueValidator(1)])
    number = models.CharField(max_length=20)
    subject = models.CharField(max_length=200)
    description = models.TextField()
    direction = models.CharField(
        max_length=10, choices=[("internal", "Internal"), ("external", "External")]
    )
    classification = models.ForeignKey(Classification, on_delete=models.PROTECT)
    discipline = models.ForeignKey("tenants.Discipline", on_delete=models.PROTECT)
    region = models.ForeignKey("tenants.Region", on_delete=models.PROTECT)
    originator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="originated_ncrs"
    )
    responsible_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="assigned_ncrs"
    )
    part_number = models.CharField(max_length=100, blank=True, default="")
    job_number = models.CharField(max_length=100, blank=True, default="")
    due_date = models.DateField()
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.OPEN)
    review_started_at = models.DateTimeField(null=True, blank=True)
    sent_to_supplier_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="closed_ncrs",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cancelled_ncrs",
    )
    cancellation_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "id"], name="ncr_report_tenant_id"),
            models.UniqueConstraint(
                fields=["company", "sequence_number"], name="ncr_report_sequence_unique"
            ),
            models.UniqueConstraint(fields=["company", "number"], name="ncr_report_number_unique"),
            models.CheckConstraint(
                condition=models.Q(sequence_number__gte=1), name="ncr_sequence_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(direction="internal"), name="ncr_internal_only_phase_two"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "open",
                        "in_review",
                        "awaiting_supplier",
                        "awaiting_internal_review",
                        "closed",
                        "cancelled",
                    ]
                ),
                name="ncr_status_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(direction="internal")
                | ~models.Q(status__in=["awaiting_supplier", "awaiting_internal_review"]),
                name="ncr_internal_skips_supplier",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="closed", closed_at__isnull=False, closed_by__isnull=False)
                    | (
                        ~models.Q(status="closed")
                        & models.Q(closed_at__isnull=True, closed_by__isnull=True)
                    )
                ),
                name="ncr_closed_stamps_match",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="cancelled", cancelled_at__isnull=False, cancelled_by__isnull=False
                    )
                    | (
                        ~models.Q(status="cancelled")
                        & models.Q(cancelled_at__isnull=True, cancelled_by__isnull=True)
                    )
                ),
                name="ncr_cancelled_stamps_match",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["awaiting_supplier", "awaiting_internal_review"])
                    | models.Q(sent_to_supplier_at__isnull=False)
                ),
                name="ncr_supplier_requires_sent",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status", "due_date"], name="ncr_status_due"),
            models.Index(
                fields=["company", "due_date"],
                condition=~models.Q(status__in=["closed", "cancelled"]),
                name="ncr_open_due",
            ),
            models.Index(fields=["company", "originator"], name="ncr_originator_list"),
            models.Index(
                fields=["company", "responsible_manager", "status"], name="ncr_manager_list"
            ),
        ]

    def __str__(self):
        return self.number

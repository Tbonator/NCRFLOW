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
    # Required for an external NCR, forbidden for an internal one (see the check below).
    supplier = models.ForeignKey(
        "suppliers.Supplier", on_delete=models.PROTECT, null=True, blank=True
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
            # Technical key: the later supplier_response foreign key points at it.
            models.UniqueConstraint(
                fields=["company", "id", "supplier"], name="ncr_report_supplier_id"
            ),
            models.CheckConstraint(
                condition=models.Q(sequence_number__gte=1), name="ncr_sequence_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(direction__in=["internal", "external"]),
                name="ncr_direction_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(direction="internal", supplier__isnull=True)
                    | models.Q(direction="external", supplier__isnull=False)
                ),
                name="ncr_direction_supplier_match",
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
            models.Index(fields=["company", "supplier", "status"], name="ncr_supplier_status"),
        ]

    def __str__(self):
        return self.number

    def start_review(self, *, by, actor_membership):
        return self._transition("start-review", by=by, actor_membership=actor_membership)

    def close(self, *, by, actor_membership):
        return self._transition("close", by=by, actor_membership=actor_membership)

    def cancel(self, *, by, actor_membership, reason=""):
        return self._transition("cancel", by=by, actor_membership=actor_membership, reason=reason)

    def _transition(self, action, *, by, actor_membership, reason=""):
        from .services import _transition_ncr

        report = _transition_ncr(
            report_id=self.pk,
            action=action,
            by=by,
            actor_membership=actor_membership,
            reason=reason,
        )
        for field in (
            "status",
            "review_started_at",
            "closed_at",
            "closed_by_id",
            "cancelled_at",
            "cancelled_by_id",
            "cancellation_reason",
            "updated_at",
        ):
            setattr(self, field, getattr(report, field))
        return self


class SupplierResponse(TenantModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        UNDER_REVIEW = "under_review", "Under Review"
        ACCEPTED = "accepted", "Accepted"
        REVISION_REQUIRED = "revision_required", "Revision Required"

    class Decision(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        REVISION_REQUIRED = "revision_required", "Revision Required"

    ncr = models.OneToOneField(NCR, on_delete=models.PROTECT, related_name="supplier_response")
    supplier = models.ForeignKey("suppliers.Supplier", on_delete=models.PROTECT)
    sent_to_supplier_contact = models.ForeignKey(
        "suppliers.SupplierContact", on_delete=models.PROTECT, related_name="dispatched_responses"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    root_cause = models.TextField(blank=True, default="")
    corrective_action = models.TextField(blank=True, default="")
    preventive_action = models.TextField(blank=True, default="")
    comments = models.TextField(blank=True, default="")
    submission_count = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    first_submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by_supplier_contact = models.ForeignKey(
        "suppliers.SupplierContact",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submitted_responses",
    )
    review_started_at = models.DateTimeField(null=True, blank=True)
    review_decision = models.CharField(
        max_length=20, choices=Decision.choices, null=True, blank=True
    )
    review_comment = models.TextField(blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviewed_supplier_responses",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "id"], name="ncr_response_tenant_id"),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "draft",
                        "submitted",
                        "under_review",
                        "accepted",
                        "revision_required",
                    ]
                ),
                name="ncr_response_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(review_decision__isnull=True)
                | models.Q(review_decision__in=["accepted", "revision_required"]),
                name="ncr_response_decision_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(submission_count__gte=0),
                name="ncr_response_count_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(status="draft") | models.Q(submission_count__gte=1),
                name="ncr_response_non_draft_submitted",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    submission_count=0,
                    first_submitted_at__isnull=True,
                    submitted_at__isnull=True,
                    submitted_by_supplier_contact__isnull=True,
                )
                | models.Q(
                    submission_count__gte=1,
                    first_submitted_at__isnull=False,
                    submitted_at__isnull=False,
                    submitted_by_supplier_contact__isnull=False,
                ),
                name="ncr_response_submission_stamps",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status="accepted",
                    review_decision="accepted",
                    review_decision__isnull=False,
                    accepted_at__isnull=False,
                )
                | (
                    ~models.Q(status="accepted")
                    & ~models.Q(review_decision="accepted")
                    & models.Q(accepted_at__isnull=True)
                ),
                name="ncr_response_accepted_matches",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="revision_required")
                | models.Q(review_decision="revision_required", review_decision__isnull=False),
                name="ncr_response_revision_decision",
            ),
            models.CheckConstraint(
                condition=~models.Q(review_decision="revision_required")
                | models.Q(review_comment__regex=r"\S"),
                name="ncr_response_revision_comment",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    review_decision__isnull=True, reviewed_at__isnull=True, reviewed_by__isnull=True
                )
                | models.Q(
                    review_decision__isnull=False,
                    reviewed_at__isnull=False,
                    reviewed_by__isnull=False,
                ),
                name="ncr_response_review_stamps",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="under_review")
                | models.Q(review_started_at__isnull=False),
                name="ncr_response_review_started",
            ),
        ]

    def __str__(self):
        return f"Supplier response for NCR {self.ncr_id}"

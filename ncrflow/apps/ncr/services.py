import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import BooleanField, Case, Q, Value, When
from django.utils import timezone

from apps.tenants.models import AuditEntry, Membership, NumberSequence
from apps.tenants.permissions import require_company_role

from .forms import NCRCreateForm
from .models import NCR


def _created(company, actor, obj, object_type, values, *, ncr=None):
    AuditEntry.objects.for_company(company).create(
        company=company,
        actor_user=actor,
        object_type=object_type,
        object_id=obj.pk,
        object_label=obj.number if object_type == "ncr" else obj.sequence_name,
        ncr=ncr,
        action=AuditEntry.Action.CREATED,
        new_value=json.dumps(values, sort_keys=True, cls=DjangoJSONEncoder),
    )


@transaction.atomic
def create_internal_ncr(*, by, actor_membership, data):
    company, actor = require_company_role(
        by=by, actor_membership=actor_membership, role=None, lock=True
    )
    form = NCRCreateForm(data, company=company)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    values = dict(form.cleaned_data)
    # Company locks serialize the approved reference-data services; reload rows too.
    for field in ("classification", "region", "discipline"):
        model = type(values[field])
        reference = (
            model.objects.for_company(company)
            .select_for_update()
            .filter(pk=values[field].pk, active=True)
            .first()
        )
        if reference is None:
            raise ValidationError({field: "This entry is no longer available."})
        values[field] = reference
    manager = (
        Membership.objects.for_company(company)
        .select_for_update(of=("self",))
        .filter(
            user_id=values["responsible_manager"].pk,
            role=Membership.Role.MANAGER,
            active=True,
            user__is_active=True,
        )
        .first()
    )
    if manager is None:
        raise ValidationError({"responsible_manager": "Choose an active company Manager."})

    instant = timezone.now()
    try:
        due_date = timezone.localtime(instant, ZoneInfo(company.timezone)).date() + timedelta(
            days=values["classification"].sla_days
        )
    except OverflowError as error:
        raise ValidationError(
            {"classification": "The SLA exceeds the supported date range."}
        ) from error
    counter, counter_created = NumberSequence.objects.for_company(company).get_or_create(
        company=company, sequence_name="ncr"
    )
    counter = NumberSequence.objects.for_company(company).select_for_update().get(pk=counter.pk)
    if counter.last_number >= 2**31 - 1:
        raise ValidationError("The NCR number series is exhausted.")
    counter.last_number += 1
    counter.save(update_fields=["last_number", "updated_at"])
    if counter_created:
        _created(
            company,
            actor,
            counter,
            "number_sequence",
            {
                "sequence_name": "ncr",
                "last_number": counter.last_number,
            },
        )
    report = NCR.objects.for_company(company).create(
        company=company,
        originator=actor,
        direction="internal",
        status=NCR.Status.OPEN,
        sequence_number=counter.last_number,
        number=f"NCR-{counter.last_number:05d}",
        due_date=due_date,
        created_at=instant,
        **values,
    )
    snapshot = {
        field: getattr(report, field)
        for field in (
            "number",
            "public_id",
            "sequence_number",
            "subject",
            "description",
            "direction",
            "classification_id",
            "region_id",
            "discipline_id",
            "originator_id",
            "responsible_manager_id",
            "part_number",
            "job_number",
            "status",
            "due_date",
            "created_at",
        )
    }
    _created(company, actor, report, "ncr", snapshot, ncr=report)
    return report


def company_today(company):
    """Today's calendar date in the company's own timezone."""
    return timezone.localtime(timezone.now(), ZoneInfo(company.timezone)).date()


def visible_ncrs_for(*, by, actor_membership):
    """The only place that decides which NCRs a user may see in the current company."""
    company, actor = require_company_role(by=by, actor_membership=actor_membership, role=None)
    # Read the role fresh from the database rather than trusting the request's copy.
    role = (
        Membership.objects.for_company(company)
        .filter(pk=actor_membership.pk, user_id=actor.pk, active=True)
        .values_list("role", flat=True)
        .first()
    )
    reports = NCR.objects.for_company(company)
    if role in (Membership.Role.QUALITY, Membership.Role.COMPANY_ADMIN):
        return reports
    if role == Membership.Role.MANAGER:
        return reports.filter(Q(responsible_manager_id=actor.pk) | Q(originator_id=actor.pk))
    if role == Membership.Role.ORIGINATOR:
        return reports.filter(originator_id=actor.pk)
    return reports.none()


def _with_overdue_flag(reports, *, today):
    """Add is_overdue: due before today and neither closed nor cancelled."""
    return reports.annotate(
        is_overdue=Case(
            When(
                Q(due_date__lt=today) & ~Q(status__in=[NCR.Status.CLOSED, NCR.Status.CANCELLED]),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(),
        )
    )


def ncr_list_for(*, by, actor_membership, today, status=""):
    """Visible NCRs for the list screen, optionally narrowed to one status."""
    reports = visible_ncrs_for(by=by, actor_membership=actor_membership)
    if status:
        reports = reports.filter(status=status)
    return (
        _with_overdue_flag(reports, today=today)
        .select_related("classification", "responsible_manager")
        .order_by("due_date", "sequence_number")
    )


def ncr_details_for(*, by, actor_membership, today):
    """Visible NCRs with everything the detail screen displays."""
    reports = visible_ncrs_for(by=by, actor_membership=actor_membership)
    return _with_overdue_flag(reports, today=today).select_related(
        "classification", "region", "discipline", "originator", "responsible_manager"
    )


def audit_history_for(*, by, actor_membership, report):
    """Audit entries of one NCR, oldest first; empty unless the actor may see that NCR."""
    company, _ = require_company_role(by=by, actor_membership=actor_membership, role=None)
    entries = AuditEntry.objects.for_company(company)
    visible = visible_ncrs_for(by=by, actor_membership=actor_membership)
    if not visible.filter(pk=report.pk).exists():
        return entries.none()
    return (
        entries.filter(ncr_id=report.pk).select_related("actor_user").order_by("created_at", "pk")
    )

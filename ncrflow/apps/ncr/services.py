import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import BooleanField, Case, Q, Value, When
from django.utils import timezone

from apps.suppliers.models import Supplier
from apps.tenants.models import AuditEntry, Membership, NumberSequence
from apps.tenants.permissions import require_company_role

from .forms import (
    DESCRIPTIVE_FIELDS,
    QUALITY_EDIT_FIELDS,
    ExternalNCRCreateForm,
    NCRCreateForm,
    NCREditForm,
    NCRTransitionForm,
)
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
    return _create_ncr(
        by=by,
        actor_membership=actor_membership,
        data=data,
        form_class=NCRCreateForm,
        direction="internal",
    )


@transaction.atomic
def create_external_ncr(*, by, actor_membership, data):
    return _create_ncr(
        by=by,
        actor_membership=actor_membership,
        data=data,
        form_class=ExternalNCRCreateForm,
        direction="external",
    )


def _create_ncr(*, by, actor_membership, data, form_class, direction):
    company, actor = require_company_role(
        by=by, actor_membership=actor_membership, role=None, lock=True
    )
    form = form_class(data, company=company)
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
    if direction == "external":
        supplier = (
            Supplier.objects.for_company(company)
            .select_for_update()
            .filter(pk=values["supplier"].pk, active=True)
            .first()
        )
        if supplier is None:
            raise ValidationError({"supplier": "Choose an active supplier in this company."})
        values["supplier"] = supplier

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
        direction=direction,
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
            "supplier_id",
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
        .select_related("classification", "responsible_manager", "supplier")
        .order_by("due_date", "sequence_number")
    )


def ncr_details_for(*, by, actor_membership, today):
    """Visible NCRs with everything the detail screen displays."""
    reports = visible_ncrs_for(by=by, actor_membership=actor_membership)
    return _with_overdue_flag(reports, today=today).select_related(
        "classification", "region", "discipline", "originator", "responsible_manager", "supplier"
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


def _may_act(report, actor, role, action):
    if action == "start-review":
        return role == Membership.Role.QUALITY or (
            role == Membership.Role.MANAGER and report.responsible_manager_id == actor.pk
        )
    if action == "close":
        return report.direction == "internal" and role == Membership.Role.QUALITY
    return action == "cancel" and role == Membership.Role.QUALITY


def _source_statuses(action):
    return {
        "start-review": (NCR.Status.OPEN,),
        "close": (NCR.Status.IN_REVIEW,),
        "cancel": (
            NCR.Status.OPEN,
            NCR.Status.IN_REVIEW,
            NCR.Status.AWAITING_SUPPLIER,
            NCR.Status.AWAITING_INTERNAL_REVIEW,
        ),
    }.get(action, ())


def ncr_actions_for(*, by, actor_membership, report):
    """Display hints only; writes repeat all checks against the locked report."""
    company, actor = require_company_role(by=by, actor_membership=actor_membership, role=None)
    role = Membership.objects.for_company(company).get(pk=actor_membership.pk).role
    if report.company_id != company.pk:
        return ()
    return tuple(
        action
        for action in ("start-review", "close", "cancel")
        if _may_act(report, actor, role, action) and report.status in _source_statuses(action)
    )


@transaction.atomic
def _transition_ncr(*, report_id, action, by, actor_membership, reason=""):
    company, actor = require_company_role(
        by=by, actor_membership=actor_membership, role=None, lock=True
    )
    role = Membership.objects.for_company(company).get(pk=actor_membership.pk).role
    report = NCR.objects.for_company(company).select_for_update().filter(pk=report_id).first()
    if report is None or not _may_act(report, actor, role, action):
        raise PermissionDenied("You cannot perform this NCR action.")
    if report.status not in _source_statuses(action):
        raise ValidationError("This action is no longer available. Reload the NCR and try again.")
    form = NCRTransitionForm({"reason": reason} if action == "cancel" else {})
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    old_status = report.status
    instant = timezone.now()
    fields = ["status", "updated_at"]
    if action == "start-review":
        report.status = NCR.Status.IN_REVIEW
        report.review_started_at = instant
        fields.append("review_started_at")
    elif action == "close":
        report.status = NCR.Status.CLOSED
        report.closed_at, report.closed_by = instant, actor
        fields.extend(["closed_at", "closed_by"])
    else:
        report.status = NCR.Status.CANCELLED
        report.cancelled_at, report.cancelled_by = instant, actor
        report.cancellation_reason = form.cleaned_data["reason"]
        fields.extend(["cancelled_at", "cancelled_by", "cancellation_reason"])
    report.save(update_fields=fields)
    AuditEntry.objects.for_company(company).create(
        company=company,
        actor_user=actor,
        ncr=report,
        object_type="ncr",
        object_id=report.pk,
        object_label=report.number,
        action=AuditEntry.Action.STATUS_CHANGED,
        field_name="status",
        old_value=old_status,
        new_value=report.status,
        comment=report.cancellation_reason if action == "cancel" else "",
    )
    # All status-email delivery is implemented in Phase 4 (CLAUDE.md section 10).
    return report


def _editable_fields(report, actor, role):
    if report.status in (NCR.Status.AWAITING_SUPPLIER, NCR.Status.AWAITING_INTERNAL_REVIEW):
        return ("responsible_manager",) if role == Membership.Role.QUALITY else ()
    if report.status not in (
        NCR.Status.OPEN,
        NCR.Status.IN_REVIEW,
    ):
        return ()
    if role == Membership.Role.QUALITY:
        return DESCRIPTIVE_FIELDS + QUALITY_EDIT_FIELDS
    if (report.status == NCR.Status.OPEN and report.originator_id == actor.pk) or (
        role == Membership.Role.MANAGER and report.responsible_manager_id == actor.pk
    ):
        return DESCRIPTIVE_FIELDS
    return ()


def editable_ncr_fields_for(*, by, actor_membership, report):
    """Form/display hint only; saving repeats the checks against the locked row."""
    company, actor = require_company_role(by=by, actor_membership=actor_membership, role=None)
    if report.company_id != company.pk:
        return ()
    role = Membership.objects.for_company(company).get(pk=actor_membership.pk).role
    return _editable_fields(report, actor, role)


@transaction.atomic
def update_ncr(*, by, actor_membership, report_id, data):
    company, actor = require_company_role(
        by=by, actor_membership=actor_membership, role=None, lock=True
    )
    role = Membership.objects.for_company(company).get(pk=actor_membership.pk).role
    report = NCR.objects.for_company(company).select_for_update().filter(pk=report_id).first()
    if report is None:
        raise PermissionDenied("You cannot edit this NCR.")
    if report.status in (NCR.Status.CLOSED, NCR.Status.CANCELLED):
        raise ValidationError(
            "This NCR is no longer editable. Reload it to see its current status."
        )
    allowed = _editable_fields(report, actor, role)
    if not allowed:
        raise PermissionDenied("You cannot edit this NCR.")
    form = NCREditForm(data, company=company, report=report, editable_fields=allowed)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    if form.cleaned_data["revision"] != report.updated_at:
        raise ValidationError("This NCR changed while you were editing. Reload it before saving.")
    changes = []
    for field in allowed:
        value = form.cleaned_data[field]
        attribute = f"{field}_id" if field in QUALITY_EDIT_FIELDS else field
        old_value = getattr(report, attribute)
        new_value = value.pk if field in QUALITY_EDIT_FIELDS else value
        if old_value == new_value:
            continue
        if field in ("region", "discipline"):
            if (
                not type(value)
                .objects.for_company(company)
                .select_for_update()
                .filter(pk=value.pk, active=True)
                .exists()
            ):
                raise ValidationError({field: "Choose an active entry in this company."})
        elif field == "responsible_manager":
            manager = (
                Membership.objects.for_company(company)
                .select_for_update(of=("self",))
                .filter(
                    user_id=value.pk,
                    active=True,
                    role=Membership.Role.MANAGER,
                    user__is_active=True,
                )
                .first()
            )
            if manager is None:
                raise ValidationError({field: "Choose an active company Manager."})
        setattr(report, attribute, new_value)
        changes.append((attribute, old_value, new_value))
    if not changes:
        return report
    report.save(update_fields=[*(field for field, _, _ in changes), "updated_at"])
    for field, old_value, new_value in changes:
        AuditEntry.objects.for_company(company).create(
            company=company,
            actor_user=actor,
            ncr=report,
            object_type="ncr",
            object_id=report.pk,
            object_label=report.number,
            action=AuditEntry.Action.FIELD_CHANGED,
            field_name=field,
            old_value=json.dumps(old_value, cls=DjangoJSONEncoder),
            new_value=json.dumps(new_value, cls=DjangoJSONEncoder),
        )
    return report

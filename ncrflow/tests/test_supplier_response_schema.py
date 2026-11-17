"""Approved SupplierResponse schema and direct database integrity backstops."""

import itertools
from datetime import timedelta

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.ncr.models import SupplierResponse
from apps.ncr.services import create_external_ncr, create_internal_ncr
from apps.suppliers.models import Supplier, SupplierContact
from tests.test_ncr_creation import data, setup_company

pytestmark = pytest.mark.django_db


def build_graph():
    setup = setup_company("response-schema")
    other = setup_company("response-schema-other")
    company = setup["company"]
    supplier = Supplier.objects.for_company(company).create(company=company, name="First supplier")
    second = Supplier.objects.for_company(company).create(company=company, name="Second supplier")
    foreign = Supplier.objects.for_company(other["company"]).create(
        company=other["company"], name="Foreign supplier"
    )
    contacts = []
    for label, parent in (
        ("first", supplier),
        ("colleague", supplier),
        ("second", second),
        ("foreign", foreign),
    ):
        contacts.append(
            SupplierContact.objects.for_company(parent.company_id).create(
                company_id=parent.company_id,
                supplier=parent,
                name=label,
                email=f"{label}@example.com",
            )
        )
    report = create_external_ncr(
        by=setup["user"], actor_membership=setup["member"], data=data(setup, supplier=supplier.pk)
    )
    next_report = create_external_ncr(
        by=setup["user"], actor_membership=setup["member"], data=data(setup, supplier=supplier.pk)
    )
    return {
        **setup,
        "other": other,
        "supplier": supplier,
        "second_supplier": second,
        "contact": contacts[0],
        "colleague": contacts[1],
        "wrong_contact": contacts[2],
        "foreign_contact": contacts[3],
        "report": report,
        "next_report": next_report,
    }


@pytest.fixture
def graph():
    return build_graph()


def values(graph, status="draft", **changes):
    now = timezone.now()
    result = {
        "company": graph["company"],
        "ncr": graph["report"],
        "supplier": graph["supplier"],
        "sent_to_supplier_contact": graph["contact"],
        "status": status,
        "root_cause": "Recorded cause",
        "corrective_action": "Recorded action",
    }
    if status != "draft":
        result.update(
            submission_count=1,
            first_submitted_at=now,
            submitted_at=now,
            submitted_by_supplier_contact=graph["contact"],
        )
    if status == "under_review":
        result["review_started_at"] = now
    if status in ("accepted", "revision_required"):
        result.update(
            review_decision=status,
            reviewed_at=now,
            reviewed_by=graph["manager"],
            review_comment="Please add the missing detail.",
            review_started_at=now,
        )
    if status == "accepted":
        result["accepted_at"] = now
    return {**result, **changes}


def insert(graph, status="draft", **changes):
    return SupplierResponse.objects.for_company(graph["company"]).create(
        **values(graph, status, **changes)
    )


def reject_insert(graph, status="draft", **changes):
    with pytest.raises(IntegrityError), transaction.atomic():
        insert(graph, status, **changes)


def update(graph, report, **changes):
    return (
        SupplierResponse.objects.for_company(graph["company"])
        .filter(pk=report.pk)
        .update(**changes)
    )


@pytest.mark.parametrize("status", SupplierResponse.Status.values)
def test_valid_states_and_default_columns(graph, status):
    response = insert(graph, status)
    assert response.ncr_id == graph["report"].pk and response.supplier_id == graph["supplier"].pk
    assert response.preventive_action == response.comments == ""
    if status == "draft":
        assert response.submission_count == 0 and response.submitted_at is None
    assert not admin.site.is_registered(SupplierResponse)


def test_only_one_response_per_ncr(graph):
    insert(graph)
    reject_insert(graph)


def test_internal_ncr_cannot_have_supplier_response(graph):
    internal = create_internal_ncr(
        by=graph["user"], actor_membership=graph["member"], data=data(graph)
    )
    reject_insert(graph, ncr=internal)


@pytest.mark.parametrize(
    "field",
    [
        "company",
        "supplier",
        "ncr",
        "sent_to_supplier_contact",
        "submitted_by_supplier_contact",
        "reviewed_by",
    ],
)
def test_foreign_company_or_supplier_references_are_rejected(graph, field):
    changes = {
        "company": graph["other"]["company"],
        "supplier": graph["second_supplier"],
        "ncr": create_external_ncr(
            by=graph["other"]["user"],
            actor_membership=graph["other"]["member"],
            data=data(graph["other"], supplier=graph["foreign_contact"].supplier_id),
        ),
        "sent_to_supplier_contact": graph["wrong_contact"],
        "submitted_by_supplier_contact": graph["foreign_contact"],
        "reviewed_by": graph["other"]["manager"],
    }
    reject_insert(graph, "accepted", **{field: changes[field]})


def test_another_contact_of_same_supplier_can_be_the_submitter(graph):
    response = insert(graph, "submitted", submitted_by_supplier_contact=graph["colleague"])
    assert response.submitted_by_supplier_contact_id == graph["colleague"].pk
    assert response.sent_to_supplier_contact_id == graph["contact"].pk


def test_update_cannot_attach_foreign_submitter_or_reviewer(graph):
    response = insert(graph)
    now = timezone.now()
    with pytest.raises(IntegrityError), transaction.atomic():
        update(
            graph,
            response,
            status="submitted",
            submission_count=1,
            first_submitted_at=now,
            submitted_at=now,
            submitted_by_supplier_contact_id=graph["wrong_contact"].pk,
        )
    assert (
        update(
            graph,
            response,
            status="submitted",
            submission_count=1,
            first_submitted_at=now,
            submitted_at=now,
            submitted_by_supplier_contact_id=graph["contact"].pk,
        )
        == 1
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        update(
            graph,
            response,
            status="revision_required",
            review_decision="revision_required",
            review_comment="Need more detail",
            reviewed_at=now,
            reviewed_by_id=graph["other"]["manager"].pk,
        )


@pytest.mark.parametrize("status", ["submitted", "under_review", "accepted", "revision_required"])
def test_non_draft_states_require_a_submission(graph, status):
    reject_insert(
        graph,
        status,
        submission_count=0,
        first_submitted_at=None,
        submitted_at=None,
        submitted_by_supplier_contact=None,
    )


@pytest.mark.parametrize("count", [0, 1])
@pytest.mark.parametrize("stamps", list(itertools.product([False, True], repeat=3)))
def test_submission_stamp_combinations(graph, count, stamps):
    now = timezone.now()
    changes = {
        "submission_count": count,
        "first_submitted_at": now if stamps[0] else None,
        "submitted_at": now if stamps[1] else None,
        "submitted_by_supplier_contact": graph["contact"] if stamps[2] else None,
    }
    valid = stamps == ((False, False, False) if count == 0 else (True, True, True))
    if valid:
        assert insert(graph, **changes).pk
    else:
        reject_insert(graph, **changes)


@pytest.mark.parametrize("stamps", list(itertools.product([False, True], repeat=3)))
def test_review_stamp_combinations(graph, stamps):
    changes = {
        "review_decision": "revision_required" if stamps[0] else None,
        "reviewed_at": timezone.now() if stamps[1] else None,
        "reviewed_by": graph["manager"] if stamps[2] else None,
        "review_comment": "Required detail",
    }
    if stamps in ((False, False, False), (True, True, True)):
        assert insert(graph, **changes).pk
    else:
        reject_insert(graph, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"review_decision": None, "reviewed_by": None, "reviewed_at": None},
        {"accepted_at": None},
        {"review_decision": "revision_required"},
    ],
)
def test_accepted_requires_matching_decision_and_stamp_even_with_null_decision(graph, changes):
    reject_insert(graph, "accepted", **changes)


@pytest.mark.parametrize("status", ["draft", "submitted", "under_review", "revision_required"])
def test_non_accepted_states_cannot_have_accepted_stamp(graph, status):
    reject_insert(graph, status, accepted_at=timezone.now())


@pytest.mark.parametrize("comment", ["", " ", "\t\n", " \r\n "])
def test_revision_requires_non_whitespace_comment(graph, comment):
    reject_insert(graph, "revision_required", review_comment=comment)


def test_revision_status_cannot_have_null_decision(graph):
    reject_insert(
        graph, "revision_required", review_decision=None, reviewed_at=None, reviewed_by=None
    )


def test_invalid_status_decision_count_and_missing_review_start(graph):
    invalid_status = values(graph, "submitted")
    invalid_status["status"] = "invented"
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierResponse.objects.for_company(graph["company"]).create(**invalid_status)
    reject_insert(
        graph,
        "submitted",
        review_decision="invented",
        reviewed_at=timezone.now(),
        reviewed_by=graph["manager"],
    )
    reject_insert(graph, submission_count=-1)
    reject_insert(graph, "under_review", review_started_at=None)


@pytest.mark.parametrize("status", ["submitted", "under_review", "accepted"])
@pytest.mark.parametrize(
    "field", ["root_cause", "corrective_action", "preventive_action", "comments"]
)
def test_locked_text_is_rejected_by_database_for_every_supplier_field(graph, status, field):
    response = insert(graph, status)
    with pytest.raises(IntegrityError), transaction.atomic():
        update(graph, response, **{field: "Changed supplier text"})
    response.refresh_from_db()
    assert getattr(response, field) != "Changed supplier text"


@pytest.mark.parametrize("status", ["submitted", "under_review", "accepted"])
def test_combined_unlock_and_text_update_cannot_bypass_old_status_lock(graph, status):
    response = insert(graph, status)
    changes = {
        "status": "draft",
        "root_cause": "Attempted bypass",
        "review_decision": None,
        "accepted_at": None,
        "reviewed_at": None,
        "reviewed_by_id": None,
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        update(graph, response, **changes)


@pytest.mark.parametrize("status", ["draft", "revision_required"])
def test_database_content_guard_allows_unlocked_text(graph, status):
    response = insert(graph, status)
    assert update(graph, response, root_cause="Updated unlocked text") == 1


def test_locked_response_can_keep_same_text_and_update_review_metadata(graph):
    response = insert(graph, "submitted")
    assert (
        update(
            graph,
            response,
            root_cause=response.root_cause,
            status="under_review",
            review_started_at=timezone.now(),
        )
        == 1
    )
    assert (
        update(
            graph,
            response,
            status="accepted",
            review_decision="accepted",
            reviewed_at=timezone.now(),
            reviewed_by_id=graph["manager"].pk,
            accepted_at=timezone.now(),
        )
        == 1
    )


@pytest.mark.parametrize(
    "field", ["company_id", "ncr_id", "supplier_id", "sent_to_supplier_contact_id", "created_at"]
)
def test_response_identity_cannot_be_changed(graph, field):
    response = insert(graph)
    replacements = {
        "company_id": graph["other"]["company"].pk,
        "ncr_id": graph["next_report"].pk,
        "supplier_id": graph["second_supplier"].pk,
        "sent_to_supplier_contact_id": graph["colleague"].pk,
        "created_at": response.created_at + timedelta(days=1),
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        update(graph, response, **{field: replacements[field]})


def test_first_submission_can_be_set_once_but_not_rewritten_or_cleared(graph):
    response = insert(graph)
    instant = timezone.now()
    assert (
        update(
            graph,
            response,
            status="submitted",
            submission_count=1,
            first_submitted_at=instant,
            submitted_at=instant,
            submitted_by_supplier_contact_id=graph["contact"].pk,
        )
        == 1
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        update(graph, response, first_submitted_at=instant + timedelta(seconds=1))
    with pytest.raises(IntegrityError), transaction.atomic():
        update(
            graph,
            response,
            status="draft",
            submission_count=0,
            first_submitted_at=None,
            submitted_at=None,
            submitted_by_supplier_contact_id=None,
        )
    assert update(graph, response, first_submitted_at=instant) == 1


def test_response_cannot_be_deleted_by_model_queryset_or_raw_sql(graph):
    response = insert(graph)
    with pytest.raises(ValidationError):
        response.delete()
    with pytest.raises(ValidationError):
        SupplierResponse.objects.for_company(graph["company"]).filter(pk=response.pk).delete()
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM ncr_supplierresponse WHERE company_id = %s AND id = %s",
            [graph["company"].pk, response.pk],
        )

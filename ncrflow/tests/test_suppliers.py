"""Phase 3 item 1: Supplier and SupplierContact model, database and service rules.

Written from CLAUDE.md sections 5, 6 and 12 and docs/data-model.md sections 2.10, 2.11,
4 and Appendix B.6. The screens are covered in tests/test_suppliers_http.py.

Cross-supplier isolation (supplier X cannot see supplier Y) needs a logged-in supplier
contact. Supplier sessions arrive in a later Phase 3 item, so it is not tested here.
"""

import json
import uuid
from collections import namedtuple
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from freezegun import freeze_time

from apps.suppliers import services
from apps.suppliers.models import Supplier, SupplierContact
from apps.tenants.models import AuditEntry, Company, Membership, Region

pytestmark = pytest.mark.django_db

Actor = namedtuple("Actor", "user company membership")

OTHER_ROLES = ["quality", "manager", "originator"]


def make_actor(slug, role=Membership.Role.COMPANY_ADMIN, company=None):
    user = get_user_model().objects.create_user(f"{slug}@example.com", "test-password")
    if company is None:
        company = Company.objects.create(name=f"Factory {slug}")
    membership = Membership.objects.for_company(company).create(
        company=company, user=user, role=role
    )
    return Actor(user, company, membership)


@pytest.fixture
def admin():
    return make_actor("admin-a")


@pytest.fixture
def other_admin():
    return make_actor("admin-b")


def make_region(company, name="Gauteng", active=True):
    return Region.objects.for_company(company).create(company=company, name=name, active=active)


def add_supplier(actor, name="Acme Castings", **extra):
    return services.create_supplier(
        by=actor.user,
        actor_membership=actor.membership,
        data={"name": name, "active": True, **extra},
    )


def add_contact(actor, supplier, email="buyer@acme.example", **extra):
    return services.create_supplier_contact(
        by=actor.user,
        actor_membership=actor.membership,
        supplier_id=supplier.pk,
        data={"email": email, "active": True, **extra},
    )


def edit_supplier(actor, supplier, **data):
    return services.update_supplier(
        by=actor.user, actor_membership=actor.membership, supplier_id=supplier.pk, data=data
    )


def edit_contact(actor, supplier, contact, **data):
    return services.update_supplier_contact(
        by=actor.user,
        actor_membership=actor.membership,
        supplier_id=supplier.pk,
        contact_id=contact.pk,
        data=data,
    )


def set_supplier_active(actor, supplier, active):
    return services.set_supplier_active(
        by=actor.user,
        actor_membership=actor.membership,
        supplier_id=supplier.pk,
        data={"active": active},
    )


def set_contact_active(actor, supplier, contact, active):
    return services.set_supplier_contact_active(
        by=actor.user,
        actor_membership=actor.membership,
        supplier_id=supplier.pk,
        contact_id=contact.pk,
        data={"active": active},
    )


def audits(company, object_type, record, **filters):
    return AuditEntry.objects.for_company(company).filter(
        object_type=object_type, object_id=record.pk, **filters
    )


def snapshot():
    """Every supplier, contact and audit row in the database, across all companies."""
    return (
        list(Supplier.objects.order_by("pk").values()),
        list(SupplierContact.objects.order_by("pk").values()),
        AuditEntry.objects.count(),
    )


def revoke(actor, what):
    if what == "user":
        get_user_model().objects.filter(pk=actor.user.pk).update(is_active=False)
    elif what == "company":
        Company.objects.filter(pk=actor.company.pk).update(active=False)
    else:
        Membership.objects.for_company(actor.company).filter(pk=actor.membership.pk).update(
            active=False
        )


def set_role(actor, role):
    Membership.objects.for_company(actor.company).filter(pk=actor.membership.pk).update(role=role)


# Every public service function, called as (actor, supplier, contact).
OPERATIONS = {
    "suppliers_for": lambda a, s, c: list(
        services.suppliers_for(by=a.user, actor_membership=a.membership)
    ),
    "supplier_contacts_for": lambda a, s, c: list(
        services.supplier_contacts_for(by=a.user, actor_membership=a.membership, supplier=s)
    ),
    "create_supplier": lambda a, s, c: add_supplier(a, "Brand New Supplier"),
    "update_supplier": lambda a, s, c: edit_supplier(a, s, name="Renamed", active=True),
    "set_supplier_active": lambda a, s, c: set_supplier_active(a, s, False),
    "create_supplier_contact": lambda a, s, c: add_contact(a, s, "new.person@acme.example"),
    "update_supplier_contact": lambda a, s, c: edit_contact(
        a, s, c, name="Changed", email="changed@acme.example", active=True
    ),
    "set_supplier_contact_active": lambda a, s, c: set_contact_active(a, s, c, False),
}
WRITE_OPERATIONS = [name for name in OPERATIONS if not name.endswith("_for")]


# --------------------------------------------------------------------------------------
# Supplier model and database rules (data-model.md 2.10, 4.1, 4.2)
# --------------------------------------------------------------------------------------


def test_supplier_is_created_in_the_actors_company_with_defaults(admin):
    supplier = add_supplier(admin)
    stored = Supplier.objects.for_company(admin.company).get(pk=supplier.pk)
    assert stored.company_id == admin.company.pk
    assert stored.name == "Acme Castings"
    assert stored.region_id is None
    assert stored.active is True
    assert stored.created_at is not None and stored.updated_at is not None


def test_supplier_requires_a_company():
    with pytest.raises(IntegrityError), transaction.atomic():
        Supplier(name="No company").save()


def test_supplier_name_is_unique_per_company_ignoring_case(admin, other_admin):
    add_supplier(admin, "Acme Castings")
    for duplicate in ("Acme Castings", "ACME CASTINGS", "acme castings", "  aCmE cAsTiNgS  "):
        with pytest.raises(ValidationError) as caught:
            add_supplier(admin, duplicate)
        assert "name" in caught.value.message_dict
    assert Supplier.objects.for_company(admin.company).count() == 1
    # The same name is a different supplier in another company.
    other = add_supplier(other_admin, "ACME CASTINGS")
    assert other.company_id == other_admin.company.pk
    assert Supplier.objects.for_company(other_admin.company).count() == 1


def test_database_rejects_case_duplicate_supplier_without_the_service(admin, other_admin):
    add_supplier(admin, "Acme Castings")
    with pytest.raises(IntegrityError), transaction.atomic():
        Supplier.objects.for_company(admin.company).create(
            company=admin.company, name="ACME castings"
        )
    Supplier.objects.for_company(other_admin.company).create(
        company=other_admin.company, name="ACME castings"
    )


def test_database_rejects_renaming_a_supplier_onto_another_ignoring_case(admin):
    add_supplier(admin, "Acme Castings")
    second = add_supplier(admin, "Bolt Brothers")
    with pytest.raises(IntegrityError), transaction.atomic():
        Supplier.objects.for_company(admin.company).filter(pk=second.pk).update(
            name="ACME CASTINGS"
        )


def test_renaming_onto_an_existing_supplier_is_refused_but_recasing_itself_is_allowed(admin):
    first = add_supplier(admin, "Acme Castings")
    second = add_supplier(admin, "Bolt Brothers")
    with pytest.raises(ValidationError) as caught:
        edit_supplier(admin, second, name="ACME castings", active=True)
    assert "name" in caught.value.message_dict
    second.refresh_from_db()
    assert second.name == "Bolt Brothers"
    edit_supplier(admin, first, name="ACME CASTINGS", active=True)
    first.refresh_from_db()
    assert first.name == "ACME CASTINGS"


def test_duplicate_name_check_includes_inactive_suppliers(admin):
    supplier = add_supplier(admin, "Acme Castings")
    set_supplier_active(admin, supplier, False)
    with pytest.raises(ValidationError):
        add_supplier(admin, "acme castings")


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_supplier_name_is_required_and_at_most_200_characters(admin, name):
    with pytest.raises(ValidationError) as caught:
        add_supplier(admin, name)
    assert "name" in caught.value.message_dict
    assert not Supplier.objects.for_company(admin.company).exists()


def test_supplier_name_of_200_characters_is_accepted(admin):
    assert add_supplier(admin, "x" * 200).name == "x" * 200


def test_for_company_only_returns_that_companys_suppliers_and_contacts(admin, other_admin):
    mine = add_supplier(admin, "Mine")
    theirs = add_supplier(other_admin, "Theirs")
    my_contact = add_contact(admin, mine, "me@mine.example")
    their_contact = add_contact(other_admin, theirs, "them@theirs.example")
    assert list(Supplier.objects.for_company(admin.company)) == [mine]
    assert list(Supplier.objects.for_company(other_admin.company)) == [theirs]
    assert list(SupplierContact.objects.for_company(admin.company)) == [my_contact]
    assert list(SupplierContact.objects.for_company(other_admin.company)) == [their_contact]
    with pytest.raises(ValueError):
        Supplier.objects.for_company(None)


def test_suppliers_cannot_be_deleted(admin):
    supplier = add_supplier(admin)
    with pytest.raises(ValidationError):
        supplier.delete()
    with pytest.raises(ValidationError):
        Supplier.objects.for_company(admin.company).filter(pk=supplier.pk).delete()
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute('DELETE FROM "suppliers_supplier" WHERE id = %s', [supplier.pk])
    assert Supplier.objects.for_company(admin.company).filter(pk=supplier.pk).exists()


def test_supplier_company_cannot_be_reassigned(admin, other_admin):
    supplier = add_supplier(admin)
    with pytest.raises(IntegrityError), transaction.atomic():
        Supplier.objects.for_company(admin.company).filter(pk=supplier.pk).update(
            company=other_admin.company
        )
    supplier.refresh_from_db()
    assert supplier.company_id == admin.company.pk


# --------------------------------------------------------------------------------------
# Supplier region (data-model.md 2.10 "same company", 4.2)
# --------------------------------------------------------------------------------------


def test_supplier_can_be_created_with_an_active_region_of_its_company(admin):
    region = make_region(admin.company)
    supplier = add_supplier(admin, region=region.pk)
    assert supplier.region_id == region.pk


def test_region_from_another_company_cannot_be_attached_by_the_service(admin, other_admin):
    foreign = make_region(other_admin.company, "Foreign region")
    with pytest.raises(ValidationError) as caught:
        add_supplier(admin, region=foreign.pk)
    assert "region" in caught.value.message_dict
    assert not Supplier.objects.for_company(admin.company).exists()
    supplier = add_supplier(admin)
    with pytest.raises(ValidationError):
        edit_supplier(admin, supplier, name=supplier.name, active=True, region=foreign.pk)
    supplier.refresh_from_db()
    assert supplier.region_id is None


def test_database_rejects_a_region_from_another_company(admin, other_admin):
    foreign = make_region(other_admin.company, "Foreign region")
    with pytest.raises(IntegrityError), transaction.atomic():
        Supplier.objects.for_company(admin.company).create(
            company=admin.company, name="Forged", region=foreign
        )
    supplier = add_supplier(admin)
    with pytest.raises(IntegrityError), transaction.atomic():
        Supplier.objects.for_company(admin.company).filter(pk=supplier.pk).update(region=foreign)
    supplier.refresh_from_db()
    assert supplier.region_id is None


def test_newly_chosen_region_must_be_active(admin):
    inactive = make_region(admin.company, "Retired", active=False)
    with pytest.raises(ValidationError) as caught:
        add_supplier(admin, region=inactive.pk)
    assert "region" in caught.value.message_dict
    supplier = add_supplier(admin)
    with pytest.raises(ValidationError):
        edit_supplier(admin, supplier, name=supplier.name, active=True, region=inactive.pk)
    supplier.refresh_from_db()
    assert supplier.region_id is None


def test_changing_from_one_region_to_an_inactive_region_is_refused(admin):
    current = make_region(admin.company, "Current")
    inactive = make_region(admin.company, "Retired", active=False)
    supplier = add_supplier(admin, region=current.pk)
    with pytest.raises(ValidationError):
        edit_supplier(admin, supplier, name=supplier.name, active=True, region=inactive.pk)
    supplier.refresh_from_db()
    assert supplier.region_id == current.pk


def test_a_retained_region_is_accepted_after_it_is_deactivated(admin):
    region = make_region(admin.company)
    supplier = add_supplier(admin, region=region.pk)
    Region.objects.for_company(admin.company).filter(pk=region.pk).update(active=False)
    edit_supplier(admin, supplier, name="Renamed keeps region", active=True, region=region.pk)
    supplier.refresh_from_db()
    assert supplier.name == "Renamed keeps region"
    assert supplier.region_id == region.pk
    # Only the name changed, so only the name is audited.
    changes = audits(admin.company, "supplier", supplier, action="field_changed")
    assert [entry.field_name for entry in changes] == ["name"]


def test_region_can_be_cleared(admin):
    region = make_region(admin.company)
    supplier = add_supplier(admin, region=region.pk)
    edit_supplier(admin, supplier, name=supplier.name, active=True, region="")
    supplier.refresh_from_db()
    assert supplier.region_id is None


# --------------------------------------------------------------------------------------
# SupplierContact model and database rules (data-model.md 2.11, 4.1, 4.2, 4.4)
# --------------------------------------------------------------------------------------


def test_contact_is_created_under_the_supplier_in_the_actors_company(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier, name="Thandi Buyer")
    stored = SupplierContact.objects.for_company(admin.company).get(pk=contact.pk)
    assert stored.company_id == admin.company.pk
    assert stored.supplier_id == supplier.pk
    assert stored.name == "Thandi Buyer"
    assert stored.email == "buyer@acme.example"
    assert stored.active is True
    assert stored.created_at is not None and stored.updated_at is not None


def test_contact_name_may_be_empty(admin):
    supplier = add_supplier(admin)
    without_name = add_contact(admin, supplier, "one@acme.example")
    blank_name = add_contact(admin, supplier, "two@acme.example", name="")
    assert without_name.name == "" and blank_name.name == ""
    direct = SupplierContact.objects.for_company(admin.company).create(
        company=admin.company, supplier=supplier, email="three@acme.example"
    )
    direct.refresh_from_db()
    assert direct.name == ""


def test_contact_email_is_required_and_must_be_an_email(admin):
    supplier = add_supplier(admin)
    for bad in ("", "   ", "not-an-email", "two@@acme.example"):
        with pytest.raises(ValidationError) as caught:
            add_contact(admin, supplier, bad)
        assert "email" in caught.value.message_dict
    assert not SupplierContact.objects.for_company(admin.company).exists()


def test_contact_email_is_stored_lowercase_and_trimmed(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier, "  Thandi.Buyer@ACME.Example  ")
    contact.refresh_from_db()
    assert contact.email == "thandi.buyer@acme.example"
    edit_contact(
        admin, supplier, contact, name="", email="  NEW.Address@Acme.EXAMPLE ", active=True
    )
    contact.refresh_from_db()
    assert contact.email == "new.address@acme.example"


def test_model_save_lowercases_email_and_database_rejects_mixed_case(admin):
    supplier = add_supplier(admin)
    contact = SupplierContact.objects.for_company(admin.company).create(
        company=admin.company, supplier=supplier, email="Direct.Save@ACME.Example"
    )
    contact.refresh_from_db()
    assert contact.email == "direct.save@acme.example"
    # CHECK email = lower(email) is the backstop when model code is bypassed.
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact.objects.for_company(admin.company).filter(pk=contact.pk).update(
            email="SHOUTING@ACME.EXAMPLE"
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact.objects.for_company(admin.company).bulk_create(
            [SupplierContact(company=admin.company, supplier=supplier, email="Bulk@ACME.Example")]
        )


def test_contact_email_is_unique_in_the_company_across_suppliers(admin):
    first = add_supplier(admin, "Acme Castings")
    second = add_supplier(admin, "Bolt Brothers")
    add_contact(admin, first, "shared@acme.example")
    for supplier in (first, second):
        for duplicate in ("shared@acme.example", "SHARED@Acme.Example", "  shared@acme.example "):
            with pytest.raises(ValidationError) as caught:
                add_contact(admin, supplier, duplicate)
            assert "email" in caught.value.message_dict
    assert SupplierContact.objects.for_company(admin.company).count() == 1


def test_duplicate_email_check_includes_inactive_contacts(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier, "shared@acme.example")
    set_contact_active(admin, supplier, contact, False)
    with pytest.raises(ValidationError):
        add_contact(admin, supplier, "shared@acme.example")


def test_same_email_is_allowed_in_a_different_company(admin, other_admin):
    mine = add_supplier(admin, "Acme Castings")
    theirs = add_supplier(other_admin, "Acme Castings")
    first = add_contact(admin, mine, "shared@acme.example")
    second = add_contact(other_admin, theirs, "shared@acme.example")
    assert first.pk != second.pk
    assert first.public_id != second.public_id
    assert first.company_id == admin.company.pk and second.company_id == other_admin.company.pk


def test_database_rejects_duplicate_email_in_the_company_without_the_service(admin, other_admin):
    first = add_supplier(admin, "Acme Castings")
    second = add_supplier(admin, "Bolt Brothers")
    add_contact(admin, first, "shared@acme.example")
    for supplier in (first, second):
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierContact.objects.for_company(admin.company).create(
                company=admin.company, supplier=supplier, email="shared@acme.example"
            )
    theirs = add_supplier(other_admin, "Acme Castings")
    SupplierContact.objects.for_company(other_admin.company).create(
        company=other_admin.company, supplier=theirs, email="shared@acme.example"
    )


def test_changing_email_onto_another_contacts_email_is_refused(admin):
    first = add_supplier(admin, "Acme Castings")
    second = add_supplier(admin, "Bolt Brothers")
    add_contact(admin, first, "taken@acme.example")
    contact = add_contact(admin, second, "free@bolt.example")
    with pytest.raises(ValidationError) as caught:
        edit_contact(admin, second, contact, name="", email="TAKEN@acme.example", active=True)
    assert "email" in caught.value.message_dict
    contact.refresh_from_db()
    assert contact.email == "free@bolt.example"
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact.objects.for_company(admin.company).filter(pk=contact.pk).update(
            email="taken@acme.example"
        )


def test_public_id_is_a_unique_uuid4_generated_by_the_application(admin):
    supplier = add_supplier(admin)
    contacts = [add_contact(admin, supplier, f"person{i}@acme.example") for i in range(5)]
    direct = SupplierContact.objects.for_company(admin.company).create(
        company=admin.company, supplier=supplier, email="direct@acme.example"
    )
    public_ids = []
    for contact in [*contacts, direct]:
        contact.refresh_from_db()
        assert isinstance(contact.public_id, uuid.UUID)
        assert contact.public_id.version == 4
        public_ids.append(contact.public_id)
    assert len(set(public_ids)) == len(public_ids)


def test_database_rejects_a_duplicate_public_id_even_across_companies(admin, other_admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    theirs = add_supplier(other_admin)
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact.objects.for_company(admin.company).create(
            company=admin.company,
            supplier=supplier,
            email="copy@acme.example",
            public_id=contact.public_id,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact.objects.for_company(other_admin.company).create(
            company=other_admin.company,
            supplier=theirs,
            email="copy@acme.example",
            public_id=contact.public_id,
        )


def test_public_id_cannot_be_changed_after_creation(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    original = contact.public_id
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact.objects.for_company(admin.company).filter(pk=contact.pk).update(
            public_id=uuid.uuid4()
        )
    contact.refresh_from_db()
    assert contact.public_id == original


def test_public_id_survives_edits_and_status_changes(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    original = contact.public_id
    edit_contact(admin, supplier, contact, name="New name", email="new@acme.example", active=True)
    set_contact_active(admin, supplier, contact, False)
    set_contact_active(admin, supplier, contact, True)
    contact.refresh_from_db()
    assert contact.public_id == original


def test_contact_supplier_cannot_be_changed_after_creation(admin):
    first = add_supplier(admin, "Acme Castings")
    second = add_supplier(admin, "Bolt Brothers")
    contact = add_contact(admin, first)
    contact.supplier = second
    with pytest.raises(ValidationError):
        contact.save()
    with pytest.raises(ValidationError):
        contact.save(update_fields=["supplier"])
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact.objects.for_company(admin.company).filter(pk=contact.pk).update(
            supplier=second
        )
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            'UPDATE "suppliers_suppliercontact" SET supplier_id = %s WHERE id = %s',
            [second.pk, contact.pk],
        )
    stored = SupplierContact.objects.for_company(admin.company).get(pk=contact.pk)
    assert stored.supplier_id == first.pk


def test_contact_company_cannot_be_changed_after_creation(admin, other_admin):
    supplier = add_supplier(admin)
    theirs = add_supplier(other_admin)
    contact = add_contact(admin, supplier)
    contact.company = other_admin.company
    with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
        contact.save()
    contact.supplier = theirs
    with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
        contact.save()
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact.objects.for_company(admin.company).filter(pk=contact.pk).update(
            company=other_admin.company
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact.objects.for_company(admin.company).filter(pk=contact.pk).update(
            company=other_admin.company, supplier=theirs
        )
    stored = SupplierContact.objects.for_company(admin.company).get(pk=contact.pk)
    assert stored.company_id == admin.company.pk and stored.supplier_id == supplier.pk
    assert not SupplierContact.objects.for_company(other_admin.company).exists()


def test_database_rejects_a_contact_whose_supplier_is_in_another_company(admin, other_admin):
    theirs = add_supplier(other_admin)
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact.objects.for_company(admin.company).create(
            company=admin.company, supplier=theirs, email="forged@acme.example"
        )
    assert not SupplierContact.objects.filter(email="forged@acme.example").exists()


def test_contact_requires_a_supplier_and_a_company(admin):
    supplier = add_supplier(admin)
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact(company=admin.company, email="orphan@acme.example").save()
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierContact(supplier=supplier, email="orphan@acme.example").save()


def test_contacts_cannot_be_deleted(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    with pytest.raises(ValidationError):
        contact.delete()
    with pytest.raises(ValidationError):
        SupplierContact.objects.for_company(admin.company).filter(pk=contact.pk).delete()
    with pytest.raises(ValidationError):
        supplier.contacts.all().delete()
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute('DELETE FROM "suppliers_suppliercontact" WHERE id = %s', [contact.pk])
    assert SupplierContact.objects.for_company(admin.company).filter(pk=contact.pk).exists()


def test_deactivated_supplier_and_contact_are_retained(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    set_contact_active(admin, supplier, contact, False)
    set_supplier_active(admin, supplier, False)
    listed = services.suppliers_for(by=admin.user, actor_membership=admin.membership)
    assert list(listed) == [supplier]
    contacts = services.supplier_contacts_for(
        by=admin.user, actor_membership=admin.membership, supplier=supplier
    )
    assert list(contacts) == [contact]
    assert not listed.get().active and not contacts.get().active


# --------------------------------------------------------------------------------------
# Role checks in every service function (CLAUDE.md 6, 12)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
def test_company_admin_may_use_every_service_function(admin, operation):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    OPERATIONS[operation](admin, supplier, contact)


@pytest.mark.parametrize("role", OTHER_ROLES)
@pytest.mark.parametrize("operation", OPERATIONS)
def test_other_roles_are_refused_by_every_service_function(admin, operation, role):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    set_role(admin, role)
    before = snapshot()
    with pytest.raises(PermissionDenied):
        OPERATIONS[operation](admin, supplier, contact)
    assert snapshot() == before


@pytest.mark.parametrize("revoked", ["user", "membership", "company"])
@pytest.mark.parametrize("operation", OPERATIONS)
def test_revoked_actor_is_refused_by_every_service_function(admin, operation, revoked):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    revoke(admin, revoked)
    before = snapshot()
    with pytest.raises(PermissionDenied):
        OPERATIONS[operation](admin, supplier, contact)
    assert snapshot() == before


@pytest.mark.parametrize("operation", OPERATIONS)
def test_stale_in_memory_role_does_not_grant_access(operation):
    """The role is re-read from the database, not trusted from the object handed in."""
    admin = make_actor("was-admin")
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    set_role(admin, "originator")
    assert admin.membership.role == "company_admin"  # the stale copy still says admin
    before = snapshot()
    with pytest.raises(PermissionDenied):
        OPERATIONS[operation](admin, supplier, contact)
    assert snapshot() == before


@pytest.mark.parametrize("operation", OPERATIONS)
def test_platform_developer_has_no_implicit_supplier_access(admin, operation):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    developer = get_user_model().objects.create_superuser("developer@example.com", "test-password")
    before = snapshot()
    for membership in (None, admin.membership):
        with pytest.raises(PermissionDenied):
            OPERATIONS[operation](Actor(developer, admin.company, membership), supplier, contact)
    assert snapshot() == before


@pytest.mark.parametrize("operation", OPERATIONS)
def test_missing_or_anonymous_actor_is_refused(admin, operation):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    before = snapshot()
    with pytest.raises(PermissionDenied):
        OPERATIONS[operation](Actor(None, admin.company, admin.membership), supplier, contact)
    with pytest.raises(PermissionDenied):
        OPERATIONS[operation](Actor(admin.user, admin.company, None), supplier, contact)
    assert snapshot() == before


# --------------------------------------------------------------------------------------
# Cross-company isolation in the services (CLAUDE.md 5.1, 12)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
def test_another_companys_membership_cannot_be_borrowed(admin, other_admin, operation):
    """User A presenting company B's membership (or the reverse) gets nothing."""
    supplier = add_supplier(other_admin)
    contact = add_contact(other_admin, supplier)
    before = snapshot()
    borrowed = Actor(admin.user, other_admin.company, other_admin.membership)
    with pytest.raises(PermissionDenied):
        OPERATIONS[operation](borrowed, supplier, contact)
    assert snapshot() == before


@pytest.mark.parametrize("operation", OPERATIONS)
def test_forged_unsaved_membership_is_refused(admin, other_admin, operation):
    supplier = add_supplier(other_admin)
    contact = add_contact(other_admin, supplier)
    forged = Membership(
        company=other_admin.company, user=admin.user, role=Membership.Role.COMPANY_ADMIN
    )
    before = snapshot()
    with pytest.raises(PermissionDenied):
        OPERATIONS[operation](Actor(admin.user, other_admin.company, forged), supplier, contact)
    forged.pk = admin.membership.pk  # a real id, but the stated company is still B
    with pytest.raises(PermissionDenied):
        OPERATIONS[operation](Actor(admin.user, other_admin.company, forged), supplier, contact)
    assert snapshot() == before


@pytest.mark.parametrize(
    "operation",
    [
        "update_supplier",
        "set_supplier_active",
        "create_supplier_contact",
        "update_supplier_contact",
        "set_supplier_contact_active",
    ],
)
def test_write_services_refuse_another_companys_supplier_and_contact(admin, other_admin, operation):
    foreign_supplier = add_supplier(other_admin, "Private supplier B")
    foreign_contact = add_contact(other_admin, foreign_supplier, "private@b.example")
    add_supplier(admin, "Own supplier")
    before = snapshot()
    with pytest.raises(ObjectDoesNotExist):
        OPERATIONS[operation](admin, foreign_supplier, foreign_contact)
    assert snapshot() == before


@pytest.mark.parametrize("operation", ["update_supplier_contact", "set_supplier_contact_active"])
def test_contact_services_refuse_a_foreign_contact_under_an_own_supplier(
    admin, other_admin, operation
):
    own_supplier = add_supplier(admin, "Own supplier")
    own_contact = add_contact(admin, own_supplier, "own@a.example")
    foreign_supplier = add_supplier(other_admin, "Private supplier B")
    foreign_contact = add_contact(other_admin, foreign_supplier, "private@b.example")
    before = snapshot()
    with pytest.raises(ObjectDoesNotExist):
        OPERATIONS[operation](admin, own_supplier, foreign_contact)
    with pytest.raises(ObjectDoesNotExist):
        OPERATIONS[operation](admin, foreign_supplier, own_contact)
    assert snapshot() == before


@pytest.mark.parametrize("operation", ["update_supplier_contact", "set_supplier_contact_active"])
def test_contact_services_refuse_a_contact_under_the_wrong_supplier_of_the_same_company(
    admin, operation
):
    right = add_supplier(admin, "Acme Castings")
    wrong = add_supplier(admin, "Bolt Brothers")
    contact = add_contact(admin, right)
    before = snapshot()
    with pytest.raises(ObjectDoesNotExist):
        OPERATIONS[operation](admin, wrong, contact)
    assert snapshot() == before


def test_read_services_never_return_another_companys_rows(admin, other_admin):
    mine = add_supplier(admin, "Own supplier")
    add_contact(admin, mine, "own@a.example")
    theirs = add_supplier(other_admin, "Private supplier B")
    add_contact(other_admin, theirs, "private@b.example")
    listed = services.suppliers_for(by=admin.user, actor_membership=admin.membership)
    assert list(listed) == [mine]
    foreign_contacts = services.supplier_contacts_for(
        by=admin.user, actor_membership=admin.membership, supplier=theirs
    )
    assert list(foreign_contacts) == []


def test_submitted_company_supplier_and_ids_are_ignored_by_the_services(admin, other_admin):
    theirs = add_supplier(other_admin, "Private supplier B")
    first = add_supplier(admin, "Acme Castings")
    second = add_supplier(
        admin,
        "Bolt Brothers",
        company=other_admin.company.pk,
        company_id=other_admin.company.pk,
        id=999999,
        pk=999999,
        created_at="2000-01-01",
    )
    assert second.company_id == admin.company.pk and second.pk != 999999
    forged_public_id = uuid.uuid4()
    forged = {
        "company": other_admin.company.pk,
        "company_id": other_admin.company.pk,
        "supplier": theirs.pk,
        "supplier_id": theirs.pk,
        "public_id": str(forged_public_id),
        "id": 999999,
    }
    contact = services.create_supplier_contact(
        by=admin.user,
        actor_membership=admin.membership,
        supplier_id=first.pk,
        data={**forged, "email": "buyer@acme.example", "active": True},
    )
    contact.refresh_from_db()
    assert contact.company_id == admin.company.pk
    assert contact.supplier_id == first.pk
    assert contact.public_id != forged_public_id and contact.pk != 999999
    original = contact.public_id
    services.update_supplier_contact(
        by=admin.user,
        actor_membership=admin.membership,
        supplier_id=first.pk,
        contact_id=contact.pk,
        data={
            **forged,
            "supplier": second.pk,
            "supplier_id": second.pk,
            "name": "Edited",
            "email": "buyer@acme.example",
            "active": True,
        },
    )
    contact.refresh_from_db()
    assert contact.name == "Edited"
    assert contact.company_id == admin.company.pk
    assert contact.supplier_id == first.pk
    assert contact.public_id == original
    edit_supplier(
        admin,
        first,
        name="Acme Renamed",
        active=True,
        company=other_admin.company.pk,
        company_id=other_admin.company.pk,
        id=theirs.pk,
    )
    first.refresh_from_db()
    assert first.company_id == admin.company.pk and first.name == "Acme Renamed"
    assert list(Supplier.objects.for_company(other_admin.company)) == [theirs]
    assert not SupplierContact.objects.for_company(other_admin.company).exists()


def test_audit_entries_stay_in_the_acting_company(admin, other_admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    edit_supplier(admin, supplier, name="Renamed", active=False)
    set_contact_active(admin, supplier, contact, False)
    assert AuditEntry.objects.for_company(admin.company).count() == 5
    assert not AuditEntry.objects.for_company(other_admin.company).exists()


# --------------------------------------------------------------------------------------
# Audit trail (CLAUDE.md 5.5, data-model.md B.6)
# --------------------------------------------------------------------------------------


def test_creating_a_supplier_writes_exactly_one_created_audit_entry(admin):
    region = make_region(admin.company)
    supplier = add_supplier(admin, region=region.pk)
    assert AuditEntry.objects.count() == 1
    entry = audits(admin.company, "supplier", supplier).get()
    assert entry.action == "created"
    assert entry.company_id == admin.company.pk
    assert entry.actor_user_id == admin.user.pk
    assert entry.created_at is not None
    initial = json.loads(entry.new_value)
    assert initial["name"] == "Acme Castings"
    assert initial["active"] is True
    assert region.pk in initial.values()


def test_creating_a_contact_writes_exactly_one_created_audit_entry(admin):
    supplier = add_supplier(admin)
    before = AuditEntry.objects.count()
    contact = add_contact(admin, supplier, "Buyer@Acme.Example", name="Thandi Buyer")
    assert AuditEntry.objects.count() == before + 1
    entry = audits(admin.company, "supplier_contact", contact).get()
    assert entry.action == "created"
    assert entry.company_id == admin.company.pk
    assert entry.actor_user_id == admin.user.pk
    initial = json.loads(entry.new_value)
    assert initial["name"] == "Thandi Buyer"
    assert initial["email"] == "buyer@acme.example"
    assert initial["active"] is True
    # The supplier's own history is untouched by adding a contact.
    assert audits(admin.company, "supplier", supplier).count() == 1


def test_each_changed_supplier_field_writes_one_field_changed_entry(admin):
    region = make_region(admin.company)
    supplier = add_supplier(admin)
    edit_supplier(admin, supplier, name="Acme Foundry", region=region.pk, active=False)
    supplier.refresh_from_db()
    assert (supplier.name, supplier.region_id, supplier.active) == (
        "Acme Foundry",
        region.pk,
        False,
    )
    changes = audits(admin.company, "supplier", supplier, action="field_changed")
    assert changes.count() == 3
    found = {entry.field_name.removesuffix("_id"): entry for entry in changes}
    assert set(found) == {"name", "region", "active"}
    expected = {
        "name": ("Acme Castings", "Acme Foundry"),
        "region": (None, region.pk),
        "active": (True, False),
    }
    for field, (old, new) in expected.items():
        entry = found[field]
        assert json.loads(entry.old_value) == old
        assert json.loads(entry.new_value) == new
        assert entry.actor_user_id == admin.user.pk
        assert entry.company_id == admin.company.pk
    assert audits(admin.company, "supplier", supplier, action="created").count() == 1


def test_only_the_changed_supplier_field_is_audited(admin):
    supplier = add_supplier(admin)
    edit_supplier(admin, supplier, name="Acme Foundry", active=True)
    entry = audits(admin.company, "supplier", supplier, action="field_changed").get()
    assert entry.field_name == "name"
    assert json.loads(entry.old_value) == "Acme Castings"
    assert json.loads(entry.new_value) == "Acme Foundry"


def test_each_changed_contact_field_writes_one_field_changed_entry(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier, "old@acme.example", name="Old Name")
    edit_contact(admin, supplier, contact, name="New Name", email="New@Acme.Example", active=False)
    contact.refresh_from_db()
    assert (contact.name, contact.email, contact.active) == ("New Name", "new@acme.example", False)
    changes = audits(admin.company, "supplier_contact", contact, action="field_changed")
    assert changes.count() == 3
    found = {entry.field_name: entry for entry in changes}
    expected = {
        "name": ("Old Name", "New Name"),
        "email": ("old@acme.example", "new@acme.example"),
        "active": (True, False),
    }
    assert set(found) == set(expected)
    for field, (old, new) in expected.items():
        assert json.loads(found[field].old_value) == old
        assert json.loads(found[field].new_value) == new
        assert found[field].actor_user_id == admin.user.pk


def test_only_the_changed_contact_field_is_audited(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier, "same@acme.example", name="Old Name")
    edit_contact(admin, supplier, contact, name="New Name", email="same@acme.example", active=True)
    entry = audits(admin.company, "supplier_contact", contact, action="field_changed").get()
    assert entry.field_name == "name"


def test_unchanged_supplier_save_writes_no_audit_and_no_update(admin):
    region = make_region(admin.company)
    with freeze_time("2026-03-02 08:00:00"):
        supplier = add_supplier(admin, region=region.pk)
    supplier.refresh_from_db()
    stamped = supplier.updated_at
    count = AuditEntry.objects.count()
    with freeze_time("2026-03-03 09:30:00"):
        edit_supplier(admin, supplier, name="Acme Castings", region=region.pk, active=True)
        edit_supplier(admin, supplier, name="  Acme Castings  ", region=str(region.pk), active="on")
        set_supplier_active(admin, supplier, True)
    assert AuditEntry.objects.count() == count
    supplier.refresh_from_db()
    assert supplier.updated_at == stamped


def test_unchanged_contact_save_writes_no_audit_and_no_update(admin):
    supplier = add_supplier(admin)
    with freeze_time("2026-03-02 08:00:00"):
        contact = add_contact(admin, supplier, "buyer@acme.example", name="Thandi")
    contact.refresh_from_db()
    stamped = contact.updated_at
    count = AuditEntry.objects.count()
    with freeze_time("2026-03-03 09:30:00"):
        edit_contact(
            admin, supplier, contact, name="Thandi", email="buyer@acme.example", active=True
        )
        # The same address in another capitalisation is still the same address.
        edit_contact(
            admin, supplier, contact, name=" Thandi ", email="  BUYER@Acme.Example ", active=True
        )
        set_contact_active(admin, supplier, contact, True)
    assert AuditEntry.objects.count() == count
    contact.refresh_from_db()
    assert contact.updated_at == stamped


def test_a_real_change_moves_updated_at(admin):
    with freeze_time("2026-03-02 08:00:00"):
        supplier = add_supplier(admin)
        contact = add_contact(admin, supplier)
    with freeze_time("2026-03-03 09:30:00"):
        edit_supplier(admin, supplier, name="Renamed", active=True)
        set_contact_active(admin, supplier, contact, False)
        changed_at = timezone.now()
    supplier.refresh_from_db()
    contact.refresh_from_db()
    assert supplier.updated_at == changed_at and contact.updated_at == changed_at
    assert supplier.created_at == changed_at - timedelta(days=1, hours=1, minutes=30)


def test_supplier_activation_and_deactivation_are_audited(admin):
    supplier = add_supplier(admin)
    for active in (False, True):
        result = set_supplier_active(admin, supplier, active)
        supplier.refresh_from_db()
        assert supplier.active is active and result.active is active
    changes = list(
        audits(admin.company, "supplier", supplier, action="field_changed").order_by("pk")
    )
    assert [entry.field_name for entry in changes] == ["active", "active"]
    assert [(json.loads(e.old_value), json.loads(e.new_value)) for e in changes] == [
        (True, False),
        (False, True),
    ]
    assert all(entry.actor_user_id == admin.user.pk for entry in changes)
    assert Supplier.objects.for_company(admin.company).count() == 1


def test_contact_activation_and_deactivation_are_audited(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    for active in (False, True):
        result = set_contact_active(admin, supplier, contact, active)
        contact.refresh_from_db()
        assert contact.active is active and result.active is active
    changes = list(
        audits(admin.company, "supplier_contact", contact, action="field_changed").order_by("pk")
    )
    assert [entry.field_name for entry in changes] == ["active", "active"]
    assert [(json.loads(e.old_value), json.loads(e.new_value)) for e in changes] == [
        (True, False),
        (False, True),
    ]
    assert all(entry.actor_user_id == admin.user.pk for entry in changes)
    # Deactivating a contact is not a change to the supplier.
    assert not audits(admin.company, "supplier", supplier, action="field_changed").exists()


@pytest.mark.parametrize("value", ["", "maybe", "1", None])
def test_status_services_reject_an_unclear_status(admin, value):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    before = snapshot()
    data = {} if value is None else {"active": value}
    with pytest.raises(ValidationError):
        services.set_supplier_active(
            by=admin.user, actor_membership=admin.membership, supplier_id=supplier.pk, data=data
        )
    with pytest.raises(ValidationError):
        services.set_supplier_contact_active(
            by=admin.user,
            actor_membership=admin.membership,
            supplier_id=supplier.pk,
            contact_id=contact.pk,
            data=data,
        )
    assert snapshot() == before


@pytest.mark.parametrize("operation", WRITE_OPERATIONS)
def test_audit_failure_rolls_the_whole_change_back(admin, operation):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    before = snapshot()
    with patch.object(AuditEntry, "save", side_effect=RuntimeError("audit failed")):
        with pytest.raises(RuntimeError):
            OPERATIONS[operation](admin, supplier, contact)
    assert snapshot() == before
    # The same call succeeds, and is audited, once audit writing works again.
    OPERATIONS[operation](admin, supplier, contact)
    assert snapshot() != before
    assert AuditEntry.objects.count() > before[2]


def test_validation_failure_leaves_no_partial_change_or_audit(admin):
    supplier = add_supplier(admin)
    contact = add_contact(admin, supplier)
    add_contact(admin, supplier, "taken@acme.example")
    before = snapshot()
    with pytest.raises(ValidationError):
        edit_contact(admin, supplier, contact, name="New", email="taken@acme.example", active=False)
    with pytest.raises(ValidationError):
        edit_supplier(admin, supplier, name="", active=False)
    assert snapshot() == before


def test_database_duplicate_raised_after_the_check_becomes_a_validation_error(admin):
    """A concurrent duplicate slips past the pre-check; the unique key must not surface as a 500."""
    supplier = add_supplier(admin, "Acme Castings")
    add_contact(admin, supplier, "taken@acme.example")
    before = snapshot()
    with patch("apps.suppliers.services._check_supplier_name"):
        with pytest.raises(ValidationError):
            add_supplier(admin, "ACME CASTINGS")
    with patch("apps.suppliers.services._check_contact_email"):
        with pytest.raises(ValidationError):
            add_contact(admin, supplier, "taken@acme.example")
    assert snapshot() == before
    # The transaction is still usable afterwards.
    assert add_supplier(admin, "Bolt Brothers").pk is not None

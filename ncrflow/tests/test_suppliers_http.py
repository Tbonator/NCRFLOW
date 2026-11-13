"""Phase 3 item 1: the Company-admin supplier and supplier-contact screens over HTTP.

Written from CLAUDE.md sections 5, 6 and 12 and docs/data-model.md sections 2.10, 2.11
and 4. Model, database and service rules are covered in tests/test_suppliers.py.

Cross-supplier isolation (supplier X cannot open supplier Y's pages) is not applicable
yet: there are no supplier sessions or supplier-facing pages until a later Phase 3 item.
"""

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils.html import escape

from apps.suppliers.models import Supplier, SupplierContact
from apps.tenants.context import CURRENT_MEMBERSHIP_SESSION_KEY
from apps.tenants.models import AuditEntry, Membership, Region
from tests.test_suppliers import (
    OTHER_ROLES,
    add_contact,
    add_supplier,
    audits,
    make_actor,
    make_region,
    revoke,
    set_role,
    snapshot,
)
from tests.test_suppliers import admin as admin
from tests.test_suppliers import other_admin as other_admin

pytestmark = pytest.mark.django_db

# Which methods each screen is meant to answer; anything else must never succeed.
ALLOWED_METHODS = {
    "list": {"GET"},
    "create": {"GET", "POST"},
    "detail": {"GET"},
    "edit": {"GET", "POST"},
    "status": {"POST"},
    "contact-create": {"GET", "POST"},
    "contact-edit": {"GET", "POST"},
    "contact-status": {"POST"},
}
POST_DATA = {
    "list": {},
    "create": {"name": "Posted supplier", "active": "on"},
    "detail": {},
    "edit": {"name": "Posted rename", "active": "on"},
    "status": {"active": "False"},
    "contact-create": {"email": "posted@acme.example", "name": "Posted", "active": "on"},
    "contact-edit": {"email": "posted.edit@acme.example", "name": "Posted", "active": "on"},
    "contact-status": {"active": "False"},
}
SUPPLIER_SCOPED = ["detail", "edit", "status", "contact-create"]
CONTACT_SCOPED = ["contact-edit", "contact-status"]


def urls_for(supplier_id, contact_id):
    supplier = {"supplier_id": supplier_id}
    contact = {"supplier_id": supplier_id, "contact_id": contact_id}
    return {
        "list": reverse("suppliers:list"),
        "create": reverse("suppliers:create"),
        "detail": reverse("suppliers:detail", kwargs=supplier),
        "edit": reverse("suppliers:edit", kwargs=supplier),
        "status": reverse("suppliers:status", kwargs=supplier),
        "contact-create": reverse("suppliers:contact-create", kwargs=supplier),
        "contact-edit": reverse("suppliers:contact-edit", kwargs=contact),
        "contact-status": reverse("suppliers:contact-status", kwargs=contact),
    }


def call(client, method, url, name):
    if method == "GET":
        return client.get(url)
    return client.post(url, POST_DATA[name])


@pytest.fixture
def world(admin, other_admin):
    """Company A and company B, each with one supplier and one contact."""
    supplier = add_supplier(admin, "Acme Castings")
    contact = add_contact(admin, supplier, "buyer@acme.example", name="Thandi Buyer")
    foreign_supplier = add_supplier(other_admin, "Private Supplier B")
    foreign_contact = add_contact(
        other_admin, foreign_supplier, "secret@private-b.example", name="Secret Person B"
    )
    return {
        "admin": admin,
        "other_admin": other_admin,
        "supplier": supplier,
        "contact": contact,
        "foreign_supplier": foreign_supplier,
        "foreign_contact": foreign_contact,
        "urls": urls_for(supplier.pk, contact.pk),
    }


def choose_company(client, actor):
    client.force_login(actor.user)
    session = client.session
    session[CURRENT_MEMBERSHIP_SESSION_KEY] = actor.membership.pk
    session.save()


def assert_errors_are_shown(response, field):
    assert response.status_code == 200
    errors = response.context["form"].errors
    assert errors.get(field), errors
    page = response.content.decode()
    for message in errors[field]:
        assert escape(message) in page


# --------------------------------------------------------------------------------------
# The Company admin can do the whole job
# --------------------------------------------------------------------------------------


def test_company_admin_can_create_list_open_and_edit_a_supplier(client, admin):
    region = make_region(admin.company, "Gauteng")
    client.force_login(admin.user)
    create_url = reverse("suppliers:create")
    form_page = client.get(create_url)
    assert form_page.status_code == 200
    assert b"Gauteng" in form_page.content
    response = client.post(
        create_url, {"name": "Acme Castings", "region": region.pk, "active": "on"}
    )
    supplier = Supplier.objects.for_company(admin.company).get()
    assert response.status_code == 302
    assert response.url == reverse("suppliers:detail", kwargs={"supplier_id": supplier.pk})
    assert (supplier.name, supplier.region_id, supplier.active) == (
        "Acme Castings",
        region.pk,
        True,
    )
    listing = client.get(reverse("suppliers:list"))
    assert listing.status_code == 200
    assert b"Acme Castings" in listing.content and b"Gauteng" in listing.content
    assert "no-store" in listing["Cache-Control"]
    detail = client.get(response.url)
    assert detail.status_code == 200 and b"Acme Castings" in detail.content
    edit_url = reverse("suppliers:edit", kwargs={"supplier_id": supplier.pk})
    edit_page = client.get(edit_url)
    assert edit_page.status_code == 200 and b"Acme Castings" in edit_page.content
    assert client.post(edit_url, {"name": "Acme Foundry", "active": "on"}).status_code == 302
    supplier.refresh_from_db()
    assert supplier.name == "Acme Foundry" and supplier.region_id is None
    assert audits(admin.company, "supplier", supplier, action="created").count() == 1
    changed = audits(admin.company, "supplier", supplier, action="field_changed")
    assert changed.count() == 2
    assert all(entry.actor_user_id == admin.user.pk for entry in changed)


def test_company_admin_can_add_and_edit_a_contact(client, admin):
    supplier = add_supplier(admin)
    client.force_login(admin.user)
    kwargs = {"supplier_id": supplier.pk}
    create_url = reverse("suppliers:contact-create", kwargs=kwargs)
    assert client.get(create_url).status_code == 200
    response = client.post(
        create_url,
        {"name": "Thandi Buyer", "email": "  Thandi.Buyer@ACME.Example ", "active": "on"},
    )
    assert response.status_code == 302
    assert response.url == reverse("suppliers:detail", kwargs=kwargs)
    contact = SupplierContact.objects.for_company(admin.company).get()
    assert contact.supplier_id == supplier.pk and contact.company_id == admin.company.pk
    assert contact.email == "thandi.buyer@acme.example"
    assert contact.public_id.version == 4
    detail = client.get(response.url)
    assert b"thandi.buyer@acme.example" in detail.content and b"Thandi Buyer" in detail.content
    edit_url = reverse(
        "suppliers:contact-edit", kwargs={"supplier_id": supplier.pk, "contact_id": contact.pk}
    )
    edit_page = client.get(edit_url)
    assert edit_page.status_code == 200 and b"thandi.buyer@acme.example" in edit_page.content
    response = client.post(edit_url, {"name": "", "email": "New@Acme.Example", "active": "on"})
    assert response.status_code == 302
    contact.refresh_from_db()
    assert contact.name == "" and contact.email == "new@acme.example"
    entry = audits(admin.company, "supplier_contact", contact, field_name="email").get()
    assert json.loads(entry.old_value) == "thandi.buyer@acme.example"
    assert json.loads(entry.new_value) == "new@acme.example"
    assert entry.actor_user_id == admin.user.pk


def test_contact_can_be_added_with_an_email_address_alone(client, admin):
    supplier = add_supplier(admin)
    client.force_login(admin.user)
    url = reverse("suppliers:contact-create", kwargs={"supplier_id": supplier.pk})
    assert client.post(url, {"email": "alone@acme.example", "active": "on"}).status_code == 302
    assert SupplierContact.objects.for_company(admin.company).get().name == ""


def test_status_urls_deactivate_and_reactivate_and_keep_the_rows_listed(client, world):
    admin, supplier, contact = world["admin"], world["supplier"], world["contact"]
    client.force_login(admin.user)
    assert client.post(world["urls"]["contact-status"], {"active": "False"}).status_code == 302
    assert client.post(world["urls"]["status"], {"active": "False"}).status_code == 302
    supplier.refresh_from_db()
    contact.refresh_from_db()
    assert not supplier.active and not contact.active
    listing = client.get(world["urls"]["list"])
    assert b"Acme Castings" in listing.content and b"Inactive" in listing.content
    detail = client.get(world["urls"]["detail"])
    assert b"buyer@acme.example" in detail.content and b"Inactive" in detail.content
    assert client.post(world["urls"]["contact-status"], {"active": "True"}).status_code == 302
    assert client.post(world["urls"]["status"], {"active": "True"}).status_code == 302
    supplier.refresh_from_db()
    contact.refresh_from_db()
    assert supplier.active and contact.active
    for object_type, record in (("supplier", supplier), ("supplier_contact", contact)):
        changes = audits(admin.company, object_type, record, field_name="active").order_by("pk")
        assert [json.loads(entry.new_value) for entry in changes] == [False, True]
        assert all(entry.actor_user_id == admin.user.pk for entry in changes)


def test_supplier_list_is_paginated_and_only_counts_the_current_company(client, world):
    admin, other_admin = world["admin"], world["other_admin"]
    for number in range(30):
        Supplier.objects.for_company(admin.company).create(
            company=admin.company, name=f"Bulk supplier {number:02d}"
        )
        Supplier.objects.for_company(other_admin.company).create(
            company=other_admin.company, name=f"Hidden bulk {number:02d}"
        )
    client.force_login(admin.user)
    seen = []
    for page_number in (1, 2):
        response = client.get(reverse("suppliers:list"), {"page": page_number})
        assert response.status_code == 200
        seen.extend(response.context["page"].object_list)
        assert b"Hidden bulk" not in response.content
    assert len(seen) == 31
    assert {supplier.company_id for supplier in seen} == {admin.company.pk}


def test_supplier_and_contact_text_is_escaped(client, admin):
    supplier = add_supplier(admin, "<script>alert(1)</script>")
    add_contact(admin, supplier, "x@acme.example", name="<img src=x onerror=alert(2)>")
    client.force_login(admin.user)
    urls = urls_for(supplier.pk, SupplierContact.objects.for_company(admin.company).get().pk)
    for name in ("list", "detail", "edit", "contact-create", "contact-edit"):
        response = client.get(urls[name])
        assert response.status_code == 200
        assert b"<script>alert(1)</script>" not in response.content
        assert b"<img src=x" not in response.content
    assert b"&lt;script&gt;" in client.get(urls["list"]).content


# --------------------------------------------------------------------------------------
# Role checks (CLAUDE.md 6: only the Company admin manages suppliers)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALLOWED_METHODS)
def test_company_admin_is_allowed_on_every_url(client, world, name):
    client.force_login(world["admin"].user)
    for method in ALLOWED_METHODS[name]:
        response = call(client, method, world["urls"][name], name)
        assert response.status_code == (200 if method == "GET" else 302), (name, method)


@pytest.mark.parametrize("role", OTHER_ROLES)
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("name", ALLOWED_METHODS)
def test_other_roles_are_refused_on_every_url_and_method(client, world, name, method, role):
    set_role(world["admin"], role)
    client.force_login(world["admin"].user)
    before = snapshot()
    response = call(client, method, world["urls"][name], name)
    if method in ALLOWED_METHODS[name]:
        assert response.status_code == 403
    else:
        assert response.status_code in (403, 405)
    assert b"Acme Castings" not in response.content
    assert b"buyer@acme.example" not in response.content
    assert snapshot() == before


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_other_roles_get_403_not_404_for_unknown_and_foreign_ids(client, world, role):
    """The role is checked before any lookup, so a refused role learns nothing about ids."""
    set_role(world["admin"], role)
    client.force_login(world["admin"].user)
    foreign = urls_for(world["foreign_supplier"].pk, world["foreign_contact"].pk)
    missing = urls_for(999999, 999999)
    for urls in (foreign, missing):
        for name in SUPPLIER_SCOPED + CONTACT_SCOPED:
            for method in ALLOWED_METHODS[name]:
                assert call(client, method, urls[name], name).status_code == 403, (name, method)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("name", ALLOWED_METHODS)
def test_anonymous_is_redirected_to_login(client, world, name, method):
    before = snapshot()
    response = call(client, method, world["urls"][name], name)
    assert response.status_code == 302
    assert response.url.startswith(reverse("accounts:login"))
    assert snapshot() == before


@pytest.mark.parametrize("revoked", ["user", "membership", "company"])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("name", ALLOWED_METHODS)
def test_revoked_admin_session_is_refused(client, world, name, method, revoked):
    client.force_login(world["admin"].user)
    assert client.get(world["urls"]["list"]).status_code == 200
    revoke(world["admin"], revoked)
    before = snapshot()
    response = call(client, method, world["urls"][name], name)
    assert response.status_code in (302, 403)
    if response.status_code == 302:
        assert response.url.startswith(reverse("accounts:login")) or response.url in (
            reverse("tenants:no-access"),
            reverse("tenants:select"),
        )
    assert b"Acme Castings" not in response.content
    assert b"buyer@acme.example" not in response.content
    assert snapshot() == before


def test_platform_developer_without_membership_cannot_manage_suppliers(client, world):
    developer = get_user_model().objects.create_superuser("developer@example.com", "test-password")
    client.force_login(developer)
    before = snapshot()
    for name, methods in ALLOWED_METHODS.items():
        for method in methods:
            response = call(client, method, world["urls"][name], name)
            assert response.status_code == 302
            assert response.url == reverse("tenants:no-access")
    assert snapshot() == before


@pytest.mark.parametrize("role", ["company_admin", *OTHER_ROLES])
def test_workspace_link_shows_only_for_company_admin(client, admin, role):
    set_role(admin, role)
    client.force_login(admin.user)
    response = client.get(reverse("tenants:workspace"))
    assert response.status_code == 200
    page = response.content.decode()
    link = f'href="{reverse("suppliers:list")}"'
    if role == "company_admin":
        assert link in page and "Manage suppliers" in page
    else:
        assert link not in page and "Manage suppliers" not in page


# --------------------------------------------------------------------------------------
# Cross-company isolation (CLAUDE.md 5.1)
# --------------------------------------------------------------------------------------


def test_list_never_shows_another_companys_suppliers(client, world):
    client.force_login(world["admin"].user)
    forged = {
        "company": world["other_admin"].company.pk,
        "company_id": world["other_admin"].company.pk,
        "membership": world["other_admin"].membership.pk,
    }
    response = client.get(world["urls"]["list"], forged)
    assert response.status_code == 200
    assert b"Acme Castings" in response.content
    assert b"Private Supplier B" not in response.content
    assert [supplier.pk for supplier in response.context["page"].object_list] == [
        world["supplier"].pk
    ]


def test_detail_never_shows_another_companys_contacts(client, world):
    client.force_login(world["admin"].user)
    response = client.get(world["urls"]["detail"])
    assert response.status_code == 200
    assert b"buyer@acme.example" in response.content
    assert b"secret@private-b.example" not in response.content
    assert b"Secret Person B" not in response.content
    assert [contact.pk for contact in response.context["contacts"]] == [world["contact"].pk]


@pytest.mark.parametrize("name", SUPPLIER_SCOPED + CONTACT_SCOPED)
def test_another_companys_supplier_and_contact_urls_are_not_found(client, world, name):
    client.force_login(world["admin"].user)
    foreign = urls_for(world["foreign_supplier"].pk, world["foreign_contact"].pk)
    before = snapshot()
    for method in ALLOWED_METHODS[name]:
        response = call(client, method, foreign[name], name)
        assert response.status_code == 404, (name, method)
        assert b"Private Supplier B" not in response.content
        assert b"secret@private-b.example" not in response.content
    assert snapshot() == before


@pytest.mark.parametrize("name", CONTACT_SCOPED)
def test_mixing_own_and_foreign_ids_in_contact_urls_is_not_found(client, world, name):
    client.force_login(world["admin"].user)
    own_supplier_foreign_contact = urls_for(world["supplier"].pk, world["foreign_contact"].pk)
    foreign_supplier_own_contact = urls_for(world["foreign_supplier"].pk, world["contact"].pk)
    before = snapshot()
    for urls in (own_supplier_foreign_contact, foreign_supplier_own_contact):
        for method in ALLOWED_METHODS[name]:
            response = call(client, method, urls[name], name)
            assert response.status_code == 404, (name, method)
            assert b"secret@private-b.example" not in response.content
    assert snapshot() == before


@pytest.mark.parametrize("name", CONTACT_SCOPED)
def test_contact_under_the_wrong_supplier_of_the_same_company_is_not_found(client, world, name):
    other_supplier = add_supplier(world["admin"], "Bolt Brothers")
    client.force_login(world["admin"].user)
    wrong = urls_for(other_supplier.pk, world["contact"].pk)
    before = snapshot()
    for method in ALLOWED_METHODS[name]:
        response = call(client, method, wrong[name], name)
        assert response.status_code == 404, (name, method)
        assert b"buyer@acme.example" not in response.content
    assert snapshot() == before
    # The same contact under its real supplier still works.
    assert client.get(world["urls"]["contact-edit"]).status_code == 200


@pytest.mark.parametrize("name", SUPPLIER_SCOPED + CONTACT_SCOPED)
def test_guessed_ids_that_do_not_exist_are_not_found(client, world, name):
    client.force_login(world["admin"].user)
    before = snapshot()
    for urls in (urls_for(999999, world["contact"].pk), urls_for(world["supplier"].pk, 999999)):
        if "999999" not in urls[name]:
            continue  # a supplier-only URL built from the real supplier id
        for method in ALLOWED_METHODS[name]:
            assert call(client, method, urls[name], name).status_code == 404, (name, method)
    assert snapshot() == before


@pytest.mark.parametrize(
    "path",
    [
        "/suppliers/0/",
        "/suppliers/-1/",
        "/suppliers/abc/",
        "/suppliers/1e3/edit/",
        "/suppliers/11111111-1111-4111-8111-111111111111/",
        "/suppliers/99999999999999999999999999/",
        "/suppliers/1/contacts/",
        "/suppliers/1/contacts/abc/edit/",
        "/suppliers/1/delete/",
        "/suppliers/1/contacts/1/delete/",
    ],
)
def test_malformed_and_unknown_urls_are_not_found(client, world, path):
    client.force_login(world["admin"].user)
    before = snapshot()
    assert client.get(path).status_code == 404
    # A well-formed id on the GET-only detail page answers 405 to POST; neither succeeds.
    assert client.post(path, {"active": "False"}).status_code in (404, 405)
    assert snapshot() == before


def test_forged_company_and_ids_in_a_supplier_post_are_ignored(client, world):
    admin, other_admin = world["admin"], world["other_admin"]
    client.force_login(admin.user)
    forged = {
        "company": other_admin.company.pk,
        "company_id": other_admin.company.pk,
        "id": world["foreign_supplier"].pk,
        "pk": world["foreign_supplier"].pk,
        "supplier_id": world["foreign_supplier"].pk,
        "created_at": "2000-01-01 00:00:00",
        "updated_at": "2000-01-01 00:00:00",
    }
    response = client.post(
        reverse("suppliers:create"), {"name": "Forged create", "active": "on", **forged}
    )
    assert response.status_code == 302
    created = Supplier.objects.get(name="Forged create")
    assert created.company_id == admin.company.pk
    assert created.pk != world["foreign_supplier"].pk
    assert created.created_at.year != 2000
    response = client.post(world["urls"]["edit"], {"name": "Forged edit", "active": "on", **forged})
    assert response.status_code == 302
    world["supplier"].refresh_from_db()
    assert world["supplier"].name == "Forged edit"
    assert world["supplier"].company_id == admin.company.pk
    assert client.post(world["urls"]["status"], {"active": "False", **forged}).status_code == 302
    world["supplier"].refresh_from_db()
    assert not world["supplier"].active
    # Company B is exactly as it was.
    foreign = Supplier.objects.for_company(other_admin.company).get()
    assert (foreign.pk, foreign.name, foreign.active) == (
        world["foreign_supplier"].pk,
        "Private Supplier B",
        True,
    )
    assert AuditEntry.objects.for_company(other_admin.company).count() == 2


def test_forged_company_supplier_and_public_id_in_a_contact_post_are_ignored(client, world):
    admin, other_admin = world["admin"], world["other_admin"]
    second = add_supplier(admin, "Bolt Brothers")
    client.force_login(admin.user)
    forged_public_id = uuid.uuid4()
    forged = {
        "company": other_admin.company.pk,
        "company_id": other_admin.company.pk,
        "supplier": world["foreign_supplier"].pk,
        "supplier_id": world["foreign_supplier"].pk,
        "public_id": str(forged_public_id),
        "id": world["foreign_contact"].pk,
        "pk": world["foreign_contact"].pk,
    }
    response = client.post(
        world["urls"]["contact-create"], {"email": "forged@acme.example", "active": "on", **forged}
    )
    assert response.status_code == 302
    created = SupplierContact.objects.get(email="forged@acme.example")
    assert created.company_id == admin.company.pk
    assert created.supplier_id == world["supplier"].pk
    assert created.public_id != forged_public_id
    # Editing cannot move the contact to another supplier, own or foreign, or reissue its id.
    contact = world["contact"]
    original_public_id = contact.public_id
    for target in (second.pk, world["foreign_supplier"].pk):
        response = client.post(
            world["urls"]["contact-edit"],
            {
                "email": "buyer@acme.example",
                "name": f"Edited {target}",
                "active": "on",
                **forged,
                "supplier": target,
                "supplier_id": target,
            },
        )
        assert response.status_code == 302
        contact.refresh_from_db()
        assert contact.name == f"Edited {target}"
        assert contact.supplier_id == world["supplier"].pk
        assert contact.company_id == admin.company.pk
        assert contact.public_id == original_public_id
    assert client.post(world["urls"]["contact-status"], {"active": "False", **forged})
    contact.refresh_from_db()
    assert not contact.active and contact.supplier_id == world["supplier"].pk
    foreign = SupplierContact.objects.for_company(other_admin.company).get()
    assert (foreign.pk, foreign.email, foreign.active, foreign.supplier_id) == (
        world["foreign_contact"].pk,
        "secret@private-b.example",
        True,
        world["foreign_supplier"].pk,
    )
    assert not SupplierContact.objects.for_company(admin.company).filter(supplier=second).exists()


def test_region_from_another_company_is_not_offered_and_cannot_be_posted(client, world):
    admin, other_admin = world["admin"], world["other_admin"]
    own = make_region(admin.company, "Own Region A")
    foreign = make_region(other_admin.company, "Secret Region B")
    client.force_login(admin.user)
    for name in ("create", "edit"):
        page = client.get(world["urls"][name])
        assert b"Own Region A" in page.content
        assert b"Secret Region B" not in page.content
    before = snapshot()
    response = client.post(
        world["urls"]["create"], {"name": "Forged region", "region": foreign.pk, "active": "on"}
    )
    assert_errors_are_shown(response, "region")
    assert b"Secret Region B" not in response.content
    response = client.post(
        world["urls"]["edit"], {"name": "Acme Castings", "region": foreign.pk, "active": "on"}
    )
    assert_errors_are_shown(response, "region")
    assert snapshot() == before
    assert client.post(
        world["urls"]["edit"], {"name": "Acme Castings", "region": own.pk, "active": "on"}
    )
    world["supplier"].refresh_from_db()
    assert world["supplier"].region_id == own.pk


def test_admin_of_two_companies_only_reaches_the_selected_company(client, world):
    """Company comes from the selected membership, never from the id in the URL."""
    admin, other_admin = world["admin"], world["other_admin"]
    second_membership = Membership.objects.for_company(other_admin.company).create(
        company=other_admin.company, user=admin.user, role=Membership.Role.COMPANY_ADMIN
    )
    foreign = urls_for(world["foreign_supplier"].pk, world["foreign_contact"].pk)
    choose_company(client, admin)
    listing = client.get(world["urls"]["list"])
    assert b"Acme Castings" in listing.content and b"Private Supplier B" not in listing.content
    before = snapshot()
    for name in SUPPLIER_SCOPED + CONTACT_SCOPED:
        for method in ALLOWED_METHODS[name]:
            assert call(client, method, foreign[name], name).status_code == 404, (name, method)
    assert snapshot() == before
    # After switching company the picture reverses.
    session = client.session
    session[CURRENT_MEMBERSHIP_SESSION_KEY] = second_membership.pk
    session.save()
    listing = client.get(world["urls"]["list"])
    assert b"Private Supplier B" in listing.content and b"Acme Castings" not in listing.content
    assert client.get(foreign["detail"]).status_code == 200
    assert client.get(world["urls"]["detail"]).status_code == 404
    assert client.post(world["urls"]["status"], {"active": "False"}).status_code == 404
    world["supplier"].refresh_from_db()
    assert world["supplier"].active


def test_admin_in_one_company_has_no_admin_rights_through_a_lesser_membership(client, world):
    admin, other_admin = world["admin"], world["other_admin"]
    lesser = make_actor("two-hats", role=Membership.Role.COMPANY_ADMIN)
    originator_membership = Membership.objects.for_company(other_admin.company).create(
        company=other_admin.company, user=lesser.user, role=Membership.Role.ORIGINATOR
    )
    client.force_login(lesser.user)
    session = client.session
    session[CURRENT_MEMBERSHIP_SESSION_KEY] = originator_membership.pk
    session.save()
    foreign = urls_for(world["foreign_supplier"].pk, world["foreign_contact"].pk)
    before = snapshot()
    for name, methods in ALLOWED_METHODS.items():
        for method in methods:
            assert call(client, method, foreign[name], name).status_code == 403, (name, method)
    assert snapshot() == before
    assert admin.company.pk != other_admin.company.pk


# --------------------------------------------------------------------------------------
# Forms and HTTP behaviour
# --------------------------------------------------------------------------------------


def test_forms_expose_only_the_documented_fields(client, world):
    client.force_login(world["admin"].user)
    for name in ("create", "edit"):
        form = client.get(world["urls"][name]).context["form"]
        assert set(form.fields) == {"name", "region", "active"}
    for name in ("contact-create", "contact-edit"):
        form = client.get(world["urls"][name]).context["form"]
        assert set(form.fields) == {"name", "email", "active"}
        assert not form.fields["name"].required
        assert form.fields["email"].required


def test_get_never_writes(client, world):
    client.force_login(world["admin"].user)
    before = snapshot()
    query = {
        "name": "Written by GET",
        "email": "get@acme.example",
        "active": "False",
        "region": "",
    }
    for name, url in world["urls"].items():
        response = client.get(url, query)
        expected = 200 if "GET" in ALLOWED_METHODS[name] else 405
        assert response.status_code == expected, name
    assert client.head(world["urls"]["status"]).status_code == 405
    assert snapshot() == before


@pytest.mark.parametrize("name", ["status", "contact-status"])
def test_status_change_urls_reject_get(client, world, name):
    client.force_login(world["admin"].user)
    before = snapshot()
    assert client.get(world["urls"][name], {"active": "False"}).status_code == 405
    assert snapshot() == before


@pytest.mark.parametrize("name", ["list", "detail"])
def test_read_only_urls_reject_post(client, world, name):
    client.force_login(world["admin"].user)
    before = snapshot()
    response = client.post(world["urls"][name], {"name": "Posted", "active": "False"})
    assert response.status_code == 405
    assert snapshot() == before


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
@pytest.mark.parametrize("name", ALLOWED_METHODS)
def test_other_http_methods_are_rejected(client, world, name, method):
    client.force_login(world["admin"].user)
    before = snapshot()
    assert getattr(client, method)(world["urls"][name]).status_code == 405
    assert snapshot() == before


@pytest.mark.parametrize("name", [n for n, methods in ALLOWED_METHODS.items() if "POST" in methods])
def test_posts_require_csrf(world, name):
    client = Client(enforce_csrf_checks=True)
    client.force_login(world["admin"].user)
    url = world["urls"][name]
    before = snapshot()
    assert client.post(url, POST_DATA[name]).status_code == 403
    assert snapshot() == before
    client.get(world["urls"]["create"])
    token = client.cookies["csrftoken"].value
    response = client.post(url, {**POST_DATA[name], "csrfmiddlewaretoken": token})
    assert response.status_code == 302
    assert snapshot() != before


@pytest.mark.parametrize("name", ["create", "edit"])
@pytest.mark.parametrize("bad_name", ["", "   ", "x" * 201])
def test_invalid_supplier_name_re_renders_with_a_message(client, world, name, bad_name):
    client.force_login(world["admin"].user)
    before = snapshot()
    response = client.post(world["urls"][name], {"name": bad_name, "active": "on"})
    assert_errors_are_shown(response, "name")
    assert snapshot() == before


@pytest.mark.parametrize("duplicate", ["Bolt Brothers", "BOLT BROTHERS", "  bolt brothers  "])
def test_duplicate_supplier_name_shows_a_form_error_not_a_500(client, world, duplicate):
    add_supplier(world["admin"], "Bolt Brothers")
    client.force_login(world["admin"].user)
    before = snapshot()
    response = client.post(world["urls"]["create"], {"name": duplicate, "active": "on"})
    assert response.status_code == 200
    assert response.context["form"].errors
    assert b"already exists" in response.content
    response = client.post(world["urls"]["edit"], {"name": duplicate, "active": "on"})
    assert response.status_code == 200
    assert response.context["form"].errors
    assert snapshot() == before


def test_supplier_name_used_by_another_company_is_accepted(client, world):
    client.force_login(world["admin"].user)
    response = client.post(world["urls"]["create"], {"name": "PRIVATE supplier b", "active": "on"})
    assert response.status_code == 302
    assert Supplier.objects.for_company(world["admin"].company).count() == 2


def test_inactive_region_cannot_be_newly_chosen_but_a_current_one_is_kept(client, world):
    admin = world["admin"]
    current = make_region(admin.company, "Current Region")
    retired = make_region(admin.company, "Retired Region", active=False)
    client.force_login(admin.user)
    assert b"Retired Region" not in client.get(world["urls"]["create"]).content
    before = snapshot()
    response = client.post(
        world["urls"]["create"], {"name": "New one", "region": retired.pk, "active": "on"}
    )
    assert_errors_are_shown(response, "region")
    response = client.post(
        world["urls"]["edit"], {"name": "Acme Castings", "region": retired.pk, "active": "on"}
    )
    assert_errors_are_shown(response, "region")
    assert snapshot() == before
    data = {"name": "Acme Castings", "region": current.pk, "active": "on"}
    assert client.post(world["urls"]["edit"], data).status_code == 302
    Region.objects.for_company(admin.company).filter(pk=current.pk).update(active=False)
    # The now-inactive current region is still offered and still accepted on this supplier.
    assert b"Current Region" in client.get(world["urls"]["edit"]).content
    response = client.post(world["urls"]["edit"], {**data, "name": "Acme Renamed"})
    assert response.status_code == 302
    world["supplier"].refresh_from_db()
    assert world["supplier"].name == "Acme Renamed"
    assert world["supplier"].region_id == current.pk
    # It is not offered to a different supplier.
    assert b"Current Region" not in client.get(world["urls"]["create"]).content


@pytest.mark.parametrize("name", ["contact-create", "contact-edit"])
@pytest.mark.parametrize(
    "bad_email", ["", "   ", "not-an-email", "a@b", "x" * 250 + "@acme.example"]
)
def test_invalid_contact_email_re_renders_with_a_message(client, world, name, bad_email):
    client.force_login(world["admin"].user)
    before = snapshot()
    response = client.post(world["urls"][name], {"email": bad_email, "name": "X", "active": "on"})
    assert_errors_are_shown(response, "email")
    assert snapshot() == before


def test_contact_name_longer_than_200_characters_re_renders_with_a_message(client, world):
    client.force_login(world["admin"].user)
    before = snapshot()
    response = client.post(
        world["urls"]["contact-create"],
        {"email": "long@acme.example", "name": "n" * 201, "active": "on"},
    )
    assert_errors_are_shown(response, "name")
    assert snapshot() == before


@pytest.mark.parametrize(
    "duplicate", ["taken@bolt.example", "TAKEN@Bolt.Example", "  taken@bolt.example  "]
)
@pytest.mark.parametrize("taken_is_active", [True, False])
def test_duplicate_contact_email_shows_a_form_error_not_a_500(
    client, world, duplicate, taken_is_active
):
    """The address is taken by a contact of a different supplier in the same company."""
    admin = world["admin"]
    second = add_supplier(admin, "Bolt Brothers")
    add_contact(admin, second, "taken@bolt.example", active=taken_is_active)
    client.force_login(admin.user)
    before = snapshot()
    for name in ("contact-create", "contact-edit"):
        response = client.post(world["urls"][name], {"email": duplicate, "active": "on"})
        assert response.status_code == 200, name
        assert response.context["form"].errors, name
        assert b"already exists" in response.content
    assert snapshot() == before


def test_contact_email_used_by_another_company_is_accepted(client, world):
    client.force_login(world["admin"].user)
    response = client.post(
        world["urls"]["contact-create"], {"email": "Secret@Private-B.example", "active": "on"}
    )
    assert response.status_code == 302
    assert SupplierContact.objects.filter(email="secret@private-b.example").count() == 2
    assert (
        SupplierContact.objects.for_company(world["admin"].company)
        .filter(email="secret@private-b.example", supplier=world["supplier"])
        .count()
        == 1
    )


@pytest.mark.parametrize("name", ["status", "contact-status"])
@pytest.mark.parametrize("data", [{}, {"active": ""}, {"active": "maybe"}, {"active": "2"}])
def test_unclear_status_post_is_a_bad_request_and_changes_nothing(client, world, name, data):
    client.force_login(world["admin"].user)
    before = snapshot()
    assert client.post(world["urls"][name], data).status_code == 400
    assert snapshot() == before


def test_resubmitting_the_same_form_or_status_writes_nothing(client, world):
    client.force_login(world["admin"].user)
    before = snapshot()
    assert (
        client.post(world["urls"]["edit"], {"name": "Acme Castings", "active": "on"}).status_code
        == 302
    )
    contact_data = {"name": "Thandi Buyer", "email": "BUYER@acme.example", "active": "on"}
    assert client.post(world["urls"]["contact-edit"], contact_data).status_code == 302
    assert client.post(world["urls"]["status"], {"active": "True"}).status_code == 302
    assert client.post(world["urls"]["contact-status"], {"active": "True"}).status_code == 302
    assert snapshot() == before

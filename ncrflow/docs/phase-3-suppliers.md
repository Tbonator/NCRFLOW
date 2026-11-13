# Phase 3, first item: suppliers and supplier contacts

Supplier and SupplierContact follow approved data-model.md sections 2.10 and 2.11.
A supplier belongs to one company and its name is unique in that company ignoring
case. A contact belongs to one supplier for ever, has a lowercase email that is
unique in the company, and carries a random `public_id` for later magic links.
This item adds the two tables and the Company-admin screens only. External NCRs,
dispatch, magic links, supplier sessions and supplier-facing pages are not built.

## Table names

| Logical name (data-model.md) | Physical table | Model |
|---|---|---|
| `supplier` | `suppliers_supplier` | `apps.suppliers.models.Supplier` |
| `supplier_contact` | `suppliers_suppliercontact` | `apps.suppliers.models.SupplierContact` |

## What is implemented

- Models in `apps/suppliers/models.py`, using the existing required-company and
  timestamp base model and its `for_company(company)` manager.
- Migration `suppliers/0001_initial`: both tables, the name and email unique keys,
  `public_id` unique, the technical keys `(company_id, id)` on both tables and
  `(company_id, supplier_id, id)` on contacts, and CHECK `email = lower(email)`.
- Migration `suppliers/0002_integrity_guards`: PostgreSQL composite foreign keys
  `(company_id, region_id)` to Region and `(company_id, supplier_id)` to Supplier,
  plus triggers that reject deletion, company reassignment, and any change to a
  contact's supplier or `public_id`. SQLite receives equivalent triggers so the
  isolated test suite exercises the same rules.
- Explicit forms in `forms.py` and transactional services in `services.py`:
  `create_supplier`, `update_supplier`, `set_supplier_active`,
  `create_supplier_contact`, `update_supplier_contact` and
  `set_supplier_contact_active`.
- Screens under `/suppliers/` with templates in `templates/suppliers/`: paginated
  supplier list, add/edit supplier, a supplier page listing its contacts, add/edit
  contact, and POST-only activate/deactivate actions for both.
- A **Manage suppliers** link in the workspace, shown to Company admins only.

No extra indexes are added: section 5 lists none for these tables, and the unique
keys already serve the lookups it names.

## Rules applied

- Supplier: name, region and active can be edited. Region is optional and must be
  an active region of the same company when chosen or changed; a supplier's
  existing region may be kept after that region is deactivated.
- Contact: name, email and active can be edited. Email is trimmed, lowercased and
  must be unique in the company, including against inactive contacts. A contact's
  supplier cannot be changed by any form, service, model save or direct SQL.
- Every create writes one `created` audit entry with a snapshot of the initial
  fields; every edit writes one `field_changed` entry per field that actually
  changed, with old and new values. An unchanged save writes nothing. Audit rows
  commit in the same transaction as the change.
- Nothing is deleted. Suppliers and contacts are retired with the active flag and
  remain visible to the Company admin.

## Permissions and isolation

Only the current company's active Company admin membership can open these screens
or use the write services (CLAUDE.md section 6: Company admin manages suppliers).
Quality, Manager and Originator receive 403 and do not see the workspace link.
The platform developer has no implicit tenant access. Company always comes from
the verified membership; write services reload the user, company and membership,
then lock company, membership, supplier and contact in that order. A supplier or
contact id from another company returns not found, as does a contact id requested
under a different supplier of the same company. Forms list their fields
explicitly; company, supplier and `public_id` are never form fields.

## Running and checking by hand

The two supplier migrations have not been applied to the development database.
With PostgreSQL running and `.env` configured:

```powershell
.\.local-python\python.exe manage.py migrate
.\.local-python\python.exe manage.py runserver
```

1. Sign in as a Company admin, choose the company and open **Manage suppliers**.
2. Add a supplier with and without a region. A second supplier whose name differs
   only in capitalisation must show a duplicate error.
3. Open the supplier, add a contact with a mixed-case email and confirm it is
   stored lowercase. Adding the same email again, under any supplier of the
   company, must show a duplicate error.
4. Edit the supplier and the contact; deactivate and reactivate each. They must
   stay in their lists with an Inactive label.
5. Sign in as Quality, Manager or Originator: `/suppliers/` must return 403 and
   the workspace link must be absent. A Company admin of another company must get
   404 for this company's supplier and contact URLs.

## Automated verification

```powershell
.\.local-python\python.exe -m pytest --ds=tests.settings -p no:cacheprovider -q
.\.local-python\python.exe manage.py check --settings=tests.settings
.\.local-python\python.exe manage.py makemigrations --check --dry-run --settings=tests.settings
.\.local-python\python.exe -m ruff check .
.\.local-python\python.exe -m ruff format --check .
```

At build time the existing 548 tests pass on isolated SQLite and the Django,
migration-drift, lint and format checks are clean. No tests for this item exist
yet; the test-writer adds them next, and independent model review follows.

Both migrations were also applied to a disposable PostgreSQL 17 test database
(created and dropped by the check, separate from the development database). There
the constraints, composite keys and triggers were created, each guard was shown to
reject a direct SQL violation, and `0002` was reversed and reapplied. Row-lock
concurrency and browser layout were not checked.

## Open questions

1. Read access for other roles is undefined. Only Company admin can see suppliers
   here. Raising an external NCR will later need a supplier picker for other roles;
   that belongs to the external-NCR item and needs an owner decision on whether
   Quality may also browse or manage suppliers.
2. Contact email changes are allowed because Appendix B.6 lists contact email
   among audited edits, but no numbered owner decision states it outright.
3. The delete, company and `public_id` guards are not listed in section 4.3. They
   follow the "never deleted" convention and the precedent of earlier phases.
4. Contacts can be added to, and remain active under, an inactive supplier. The
   model says such contacts cannot log in; that check belongs to the later
   supplier-session item.

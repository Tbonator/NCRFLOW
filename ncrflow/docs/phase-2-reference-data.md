# Phase 2, first item: reference data

Region, Discipline and Classification follow approved data-model.md sections 2.5
to 2.7. Each row belongs to one company; names are unique ignoring case within
that company, including inactive rows. Classification requires non-negative
calendar SLA days. No NCR table or lifecycle is included in this item.

## What is implemented

- Region and Discipline models in `apps/tenants/models.py`, Classification in
  `apps/ncr/models.py`, using the existing required-company/timestamp base model.
- Explicit forms, company-scoped Quality-role checks and transactional services
  in `apps/tenants/reference_forms.py`, `permissions.py` and `reference_data.py`.
- Overview, paginated lists, create/edit forms and POST-only active-status actions
  in `reference_views.py`, with templates under `templates/reference/`.
- A **Manage reference data** link for Quality users in the workspace.
- Atomic creation snapshots and per-field edit/activation audits. Unchanged saves
  do not produce misleading change events. Audit failure rolls back the write.
- Migrations `tenants/0004_discipline_region`, `ncr/0001_initial` and
  `tenants/0005_reference_data_guards`. The final migration depends on both model
  migrations so guards are installed only after all three tables exist.

Models use the approved case-insensitive name constraints, technical
UNIQUE(company_id, id) keys and Classification SLA check. PostgreSQL and SQLite
guards reject physical deletion or company reassignment. Retire a row through its
active flag; it remains visible to Quality and can be reactivated. Future NCR
creation must select only active same-company rows. Changing Classification SLA
values will not rewrite due dates of existing NCRs when that feature is implemented.

## Permissions and isolation

Only the current company's active Quality membership can use these management
screens or write services. Company admin, Manager and Originator do not gain
Quality permission automatically. The platform developer has no implicit tenant
access. Views use the current verified company; write services reload user,
company and membership status/role and lock company, membership, then reference.
An ID belonging to another company returns not found. Posted company/user/role/id
fields cannot select the target tenant or elevate permissions.

Forms list allowed fields explicitly; status changes and edits require POST and
CSRF protection. Duplicate names show a form error rather than a server exception.
No delete endpoint exists. Audit records remain append-only under Phase 1 guards.
Business writes must go through the services rather than raw ORM/SQL, which does
not provide lifecycle auditing.

## Running and checking by hand

With normal PostgreSQL setup and `.env` configured:

```powershell
python manage.py migrate
python manage.py runserver
```

Use the local runtime described in `phase-1-accounts.md` if the workstation's
normal Python installation is unavailable. Do not run the app with test settings.
No development database migration or real customer data creation was performed
by this implementation session.

Provision a Quality membership using the authorised Company admin and the existing
`provision_member` service from `phase-1-tenants.md`, choosing role `quality` and a
valid initial password for a new account. Then:

1. Sign in at `/accounts/login/`, choose the company and open **Manage reference data**.
2. Create a Region, Discipline and Classification. Zero SLA days is allowed;
   negative SLA days and blank names are rejected.
3. Try a second name with different capitalisation; it must show a duplicate error.
4. Edit a name or SLA; deactivate and reactivate the same record. It must remain
   in the list with its status rather than disappear.
5. A non-Quality account must receive 403 for `/reference/`; another company's
   Quality account must not see these rows or edit them using a guessed ID.

## Automated verification and review

```powershell
python -m pytest --ds=tests.settings -p no:cacheprovider
python manage.py check
python manage.py makemigrations --check --dry-run --settings=tests.settings
python -m ruff check .
python -m ruff format --check .
```

The full suite passes 202 tests against isolated SQLite. Reference-data coverage
includes company isolation, all refused roles, active-state revocation, URL/form
forgery, duplicate names at service and database boundaries, creation/edit/status
audits and rollback, immutable company keys, retained records, CSRF, name escaping
and SLA validation. Existing Phase 1 tests also pass. Django, migration-drift,
lint and formatting checks passed.

Independent review of this item returned **PASS — 0 Blockers, 0 Should fix,
0 Minor**. The reviewer independently ran all 202 tests, Django checks, migration
consistency, Ruff linting and formatting successfully. PostgreSQL trigger execution
and live concurrency remain unverified; SQLite does not establish those behaviours.
Supplier isolation tests and NCR lifecycles belong to subsequent phase items.

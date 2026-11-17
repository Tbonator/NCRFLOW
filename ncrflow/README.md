# ncrflow

Non-conformance reporting for small and mid-size manufacturers, with a free supplier portal.

`ncrflow` is a working name.

## Where the project stands

The data model is approved. Phase 1 has started with the custom email-login User
model, Company, Membership, append-only AuditEntry and transactional provisioning
services, with migrations and tests. Internal login, verified company selection,
tenant middleware and the company workspace are now implemented as well.
Independent Phase 1 implementation review passed with no remaining findings.
Live PostgreSQL validation remains outstanding; see `docs/phase-1-review.md`.
See `CLAUDE.md` and `docs/data-model.md`.

Phase 2's first item adds Region, Discipline and Classification management.
Quality users can create, edit, deactivate and reactivate entries within their
current company. Every change is audited. The next item now adds internal NCR
creation, per-company numbering and calendar SLA due dates, with a private
creation confirmation. Review/closure and general NCR lists/details are still
subsequent work.
See `docs/phase-2-reference-data.md`.
This item passed independent review with no findings; all 202 tests pass using
isolated SQLite. Live PostgreSQL validation is still outstanding.

## What is in the folder

```
CLAUDE.md            instructions Claude Code reads every session
.claude/agents/      the four agents (data-modeller, model-reviewer, app-builder, test-writer)
config/              settings, urls, celery
apps/                accounts and tenant foundations; other apps filled phase by phase
docs/                approved data model and implementation notes
templates/           HTML templates
tests/               pytest tests
docker-compose.yml   local Postgres and Redis
.env.example         copy to .env
```

## First-time setup

You need Python 3.12 or newer and Docker Desktop.

1. Open this folder in VS Code.
2. Open a terminal in VS Code (Terminal > New Terminal).
3. Create a virtual environment:
   - Windows: `python -m venv .venv` then `.venv\Scripts\activate`
   - Mac or Linux: `python3 -m venv .venv` then `source .venv/bin/activate`
4. Install the packages: `pip install -r requirements.txt`
5. Copy `.env.example` to a new file named `.env` and change `SECRET_KEY`.
6. Run the isolated checks: `python -m pytest --ds=tests.settings`. They use an
   in-memory SQLite database, leaving your development database untouched.
   Run `python manage.py check` and `python -m ruff check .` as well.

## Phase 1 progress

The first item sets `AUTH_USER_MODEL = "accounts.User"` before the first migration.
Email is stored lowercase and globally unique; passwords use Django hashing.
Platform staff and superuser flags must match. Customer permissions will come
from company memberships, not platform flags. See `docs/phase-1-accounts.md`
for validation commands and current limitations.

The next implemented item adds Company/Membership/AuditEntry with database
integrity guards and audited provisioning. Each company membership has one role;
a global login can have memberships in several companies. See
`docs/phase-1-tenants.md` for service examples and the PostgreSQL validation limit.

Internal users sign in at `/accounts/login/`. A single active company is selected
automatically; accounts with multiple active memberships choose a company at
`/companies/select/`. `/workspace/` requires a verified active membership, even
for the platform superuser. Developer administration remains at `/admin/`.
See `docs/phase-1-login.md` for setup and validation details.

## Phase 2 progress

Quality users can open **Manage reference data** from their workspace, or visit
`/reference/`. Region and Discipline have company-specific names and active flags;
Classification also has non-negative SLA days. Names are unique within each
company, ignoring case, including inactive entries. Entries are retained for
history. Company admin and Manager roles do not implicitly grant Quality access.

All active internal members can now open **Raise an internal NCR** in their
workspace (`/ncrs/new/`). The form offers only active company reference data and
active same-company Managers. Number, originator, company, Open status and due
date are assigned by the server. See `docs/phase-2-ncr-creation.md` for setup,
scope and validation status.
The creation item passed independent review with no remaining findings and all
268 tests passing. NCR lists/details and internal review/close/cancel actions now
exist; see `docs/phase-2-internal-lifecycle.md` for the current item and validation.
Approved NCR editing is also implemented; `docs/phase-2-ncr-editing.md` describes
role-specific fields, manager reassignment and stale-edit protection. Supplier
and contact management are available; dispatch and magic-link access are next.
External NCR creation and internal pre-dispatch review are now available through
`/ncrs/new/external/`. All active company roles can choose an active supplier in
their company. Creating the report sends no supplier email. See
`docs/phase-3-external-ncr.md` for scope and validation.
The SupplierResponse schema foundation is now implemented as the prerequisite for
dispatch; see `docs/phase-3-supplier-response-schema.md`. Dispatch delivery and
authenticated supplier access are still upcoming; no send action is enabled yet.
Current validation (2026-11-14): 1,098 tests pass, including external NCR creation,
pre-dispatch workflow and the response-schema foundation. The latest item was
independently reviewed PASS with no findings.
Local PostgreSQL rollback verification and live HTTP
checks pass; interactive browser layout and concurrent-request checks remain
outstanding. See `docs/phase-3-external-ncr.md` for current review status and
`docs/presentation-refresh.md` for the earlier presentation/migration review.

## Running the app (from Phase 1 onward)

```
docker compose up -d
python manage.py migrate
python manage.py runserver
```

The custom User model now exists in `accounts/0001_initial`. Apply migrations
only to a database created for this project; an existing database that already
used Django's default user needs a separately planned migration. Do not reset it.

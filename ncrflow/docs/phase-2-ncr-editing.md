# Phase 2: edit an internal NCR

Implements approved Q38 / Appendix B.7 with no schema changes or migrations.

## Permissions

| Actor | Open | In Review | Closed or Cancelled |
|---|---|---|---|
| Creator, regardless of company role | Descriptive fields | Only if also assigned Manager or Quality | No edits |
| Assigned active Manager | Descriptive fields | Descriptive fields | No edits |
| Quality | Descriptive fields, region, discipline and responsible manager | Same | No edits |
| Other members | None | None | None |

Descriptive fields are subject, description, part number and job number. Company
admin read access alone grants no editing rights. Every permission requires an
active account, active membership and active company. Replacement routing entries
must be active in the same company; a replacement manager must have an active
Manager membership and account. An unchanged historical inactive choice can be
retained, so an unrelated text edit does not force a reassignment.

Classification, company, originator, direction, number, UUID, sequence, creation
time, due date, status and terminal stamps cannot be changed through this form.
Forged extra POST fields are ignored; only the explicit permitted form fields are
applied. No email is sent.

## Write boundary

`update_ncr` (originally `update_internal_ncr`, generalized in Phase 3) reloads
and locks Company, actor Membership and NCR, then
checks current role, assignment and status. Changed routing rows or manager
memberships are checked under locks too. The form's hidden revision uses the
existing updated_at timestamp; a mismatch with the locked row returns a conflict
instead of overwriting newer changes. Invalid/stale saves write no fields or
audits. Each actual changed field receives one linked field_changed audit, with
JSON old/new values, in the same transaction. Audit failure rolls everything back.
A no-op creates no audit and does not change updated_at.

Q42 remains in force: the detail page shows only audit time, actor and readable
action label. Full stored field names, old/new values and comments remain private
from that display.

## Screens and files

The NCR detail page shows **Edit NCR** when allowed. `/ncrs/<uuid>/edit/` accepts
GET and CSRF-protected POST. Each role sees only its permitted fields. Foreign or
invisible records return 404; visible records without edit permission return 403.
Conflicting edits return 409 with a reload message; successful saves redirect to
detail. GET never writes anything.

Application changes: `apps/ncr/forms.py`, `services.py`, `views.py`, `urls.py`,
`templates/ncr/edit.html`, `templates/ncr/detail.html`. Tests:
`tests/test_ncr_editing.py`. Requirements mapping was recorded first in
`docs/data-model.md`.

## Try it

Use the running local app, or run `python manage.py runserver` from the inner
project directory with the virtual environment active and PostgreSQL running.

1. Open an Open NCR as its originator; choose Edit NCR and update its description.
2. As Quality, change its region or assign a different active Manager. Confirm the
   old manager loses assigned-manager authority and the new one gains it.
3. Open two edit tabs. Save one, then save the other: the second shows a conflict.
4. Close or cancel the NCR as Quality: no edit action remains and forged edit POSTs
   are refused. Company admin alone can read but cannot edit.
5. Check that history remains plain and a save with no changes adds no history row.

## Verification

**548 tests passed in 105.08s** on isolated SQLite, including 61 new editing tests.
Django system checks, migration drift, Ruff lint and formatting checks pass.
Independent review: **PASS, 0 Blockers, 0 Should fix, 0 Minor**; the reviewer
independently passed all 61 editing tests in 11.69s and Django/drift checks.

Live HTTP checks verified role-specific edit fields and Company admin refusal
against the running app. A PostgreSQL transaction verified text/routing changes,
reference locking SQL and linked field audits, then rolled back all verification
changes and confirmed the demo NCR was unchanged. Interactive browser layout and
live concurrent edits remain unverified. No migrations were added or run for this
item; no application data was permanently changed by its verification.

Tests cover permissions, cross-company scoping,
immutable/forged inputs, active replacement lists, retained historical choices,
stale revision/state/role/assignment, no-op handling, full audit rollback and
HTTP form/CSRF boundaries. Supplier isolation is not applicable until Phase 3.

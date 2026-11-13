# Phase 2: internal NCR creation

This item implements N1 from the approved data model: an active internal member
raises an internal NCR in Open status, chooses a responsible Manager and receives
an NCR number and a due date. General role-filtered lists/details, editing and
review/close/cancel transitions remain subsequent items. No supplier feature or
email dispatch is introduced by this item.

## Behaviour

- Any active membership role can raise an NCR in its current company. The
  originator is the authenticated user, regardless of that membership's role.
- The form explicitly permits subject, description, Classification, Region,
  Discipline, responsible Manager, part number and job number. Part/job numbers
  are optional; the other fields are required.
- Reference choices must be active and belong to the current company. The
  responsible user must be active with an active Manager membership in that
  company. A Quality role alone does not make someone an assignable Manager.
- Company, originator, internal direction, Open status, number and due date are
  server-owned. Forged posted values cannot override them.
- Numbering starts at `NCR-00001` separately for each company. Counter allocation,
  report creation and both creation audit entries share one transaction. Failed
  validation or auditing consumes no number. Padding grows beyond five digits.
- Due date is the company-local creation date plus Classification SLA calendar
  days. Zero days means the same local date. One captured UTC instant is used for
  both stored created_at and the date calculation, even if insertion crosses
  midnight. Later Classification SLA changes do not rewrite stored due dates.

The private confirmation at `/ncrs/<uuid>/created/` is a receipt for the report's
originator within the current company. It is not the general NCR detail screen
or final Quality/Manager browsing policy. Changing company or guessing another
creator's UUID does not grant access. The page displays the number, due date,
status, reference names, manager and submitted report content.

## Implementation and integrity

- `apps/ncr/models.py`: NCR fields, statuses, approved unique/check constraints
  and current-phase indexes; the supplier field is deliberately deferred.
- `apps/tenants/models.py`: NumberSequence and nullable AuditEntry.ncr/history index.
- `apps/ncr/forms.py`, `services.py`, `views.py`, `urls.py`: explicit choices,
  transactional creation and scoped originator confirmation.
- Templates in `templates/ncr/`, plus the workspace creation link.
- `ncr/0002_ncr`, `tenants/0006_numbersequence_auditentry_ncr_and_more` and
  `ncr/0003_creation_integrity`: schema followed by cross-company integrity guards.

Company and membership authority are reloaded. Creation locks Company, actor
membership, reference rows, chosen Manager membership and number counter. Manager
membership locking targets that table only, avoiding a joined User lock that
would conflict with account-status audit operations. The existing Company lock
also serializes reference changes and first-use counter creation in that company.
Other companies have separate locks/counters. A named series row is obtained with
get_or_create then locked/incremented; database uniqueness backs up the service.

PostgreSQL composite FKs enforce same-company reference and user-membership links,
including the nullable closing/cancelling user references for future transitions.
AuditEntry.ncr must also match the audit company. SQLite uses integrity triggers.
Database guards reject report/counter deletion, report identity/classification/
originator/date changes, counter reassignment and decreasing issued counters.
Only internal direction is currently permitted; the Phase 3 supplier migration
must replace that staging check before enabling external reports. Business writes
must use the service; raw ORM/SQL does not provide lifecycle audit creation.

Independent review reproduced an additional SQLite rollback issue: removing the
new history FK rebuilds AuditEntry and can drop the older audit immutability and
actor-validation triggers. The new `tenants/0006` now has a reverse-only operation
that restores exactly those three audit guards after field removal. Existing
older migrations are unchanged. A regression runs isolated forward, rollback and
reapply migrations, verifying raw audit UPDATE/DELETE and foreign-actor INSERT
are rejected at each stage. This adds no production schema column or rule.

## Running and manual checks

With PostgreSQL and `.env` configured, apply all project migrations and start the
server from the inner project directory:

```powershell
python manage.py migrate
python manage.py runserver
```

The workstation's ignored local runtime can replace `python` with
`.\.local-python\python.exe`, as documented in `phase-1-accounts.md`. Do not use
test settings for a running app. No persistent development database was migrated
and no real company/user/report data was created during this implementation.

1. Have Quality create active Region, Discipline and Classification entries using
   the previous item. Classification needs an SLA in calendar days.
2. Have the Company admin provision an active Manager membership using the
   existing provisioning service. Assign an initial password when a new account
   needs to log in; Manager assignment itself does not require an active session.
3. Sign in with an active company member, open the workspace and select
   **Raise an internal NCR**. Complete the required fields and submit.
4. Confirm the receipt's number, Open status and due date. A second successful
   report gets the next number. A missing/invalid required choice must show an
   error without consuming a number.
5. Another company's reference or Manager must never appear or be accepted. The
   confirmation must not be accessible to another originator or selected company.

## Verification

```powershell
python -m pytest --ds=tests.settings -p no:cacheprovider
python manage.py check
python manage.py makemigrations --check --dry-run --settings=tests.settings
python -m ruff check .
python -m ruff format --check .
```

The full suite passes 268 tests using isolated SQLite, including numbering,
rollback, timezone/midnight/date overflow, zero-day SLA, immutable dates, forged
fields, active choices, database company integrity, audit links, confirmation
ownership, CSRF and escaped report content. Existing audit immutability tests also
pass after adding the nullable NCR history field. Django checks, migration
consistency, Ruff linting and formatting passed. The independent reviewer also
ran all 268 tests successfully, including the isolated migration-roundtrip case.
That integration test has phase diagnostics and a 180-second timeout to tolerate
the workstation's runtime startup/load variation; no integrity assertion is
skipped or weakened. The independent full run completed in 154.71 seconds.

Independent review returned **PASS — 0 Blockers, 0 Should fix, 0 Minor**. The
reviewer closed the SQLite audit-trigger rollback finding, independently verified
the 4→3→4 trigger preservation across rollback/reapply, ran all 268 tests and
passed Django system checks, migration consistency, Ruff linting and formatting.

PostgreSQL SQL execution and live concurrent number allocation remain unverified;
SQLite tests are not production database/concurrency evidence. Later internal
lifecycle and general list/detail screens are not implied by this creation item.

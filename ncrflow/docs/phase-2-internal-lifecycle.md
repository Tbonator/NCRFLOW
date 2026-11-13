# Phase 2: internal NCR lifecycle

Implements approved N2, N7 and internal N8 with no schema changes or migrations.
Supplier workflows remain Phase 3 and all status email delivery remains Phase 4.
This item sends no closure email.

## Behaviour

- `NCR.start_review(by=..., actor_membership=...)`: Open to In Review, by the
  currently assigned active Manager or active Quality member. Sets review_started_at.
- `NCR.close(...)`: In Review to Closed, by Quality only. Sets closed_at and closed_by.
- `NCR.cancel(..., reason="")`: Open or In Review to Cancelled, by Quality only.
  Sets cancelled_at, cancelled_by and optional trimmed reason; preserves review time.
- Closed and Cancelled are terminal. Repeated actions are refused without duplicate
  status audits. Company-admin read access grants no transition rights.
- Quality can act when the assigned manager has been deactivated.

Methods delegate to a transaction that reloads active company, account and membership
authority, locks Company then actor Membership then the same-company NCR, and checks
persisted role, assignment and status. Status, stamps and linked status_changed audit
save together. An audit failure rolls back everything. Stale model attributes and
posted company/status/actor fields do not control the write. The calling instance
is updated only after the service succeeds.

## Screens and history

The detail page offers appropriate actions using POST with CSRF to
`/ncrs/<uuid>/actions/<action>/`. The server repeats authority checks. Invisible or
foreign NCRs return 404; visible but unauthorised actions return 403; stale state
returns 409 with a reload message. Successful actions redirect to the NCR. Close
and cancel forms explain that those decisions cannot be reversed.

The detail screen now follows Q42: history shows only when, who (or System), and
the readable action label, for every role. Full stored audit values, field names
and comments remain intact but are not displayed. Cancellation reasons are stored
on the NCR and audit entry; this item adds no reason display.

## Files and manual checks

Changes: apps/ncr models, services, forms, views and URLs; templates/ncr/detail.html;
tests/test_ncr_transitions.py and list/detail regression tests. The approved model's
implementation mapping was recorded first in docs/data-model.md.

Start the existing configured app from the inner project directory:

```powershell
python manage.py runserver
```

1. Raise an NCR; sign in as its assigned Manager and select Start review on its
   detail page. Check In Review and its plain status history.
2. Sign in as Quality and close it. Check Closed and absence of action forms.
3. Cancel a different NCR as Quality with a reason. Check Cancelled; the reason and
   stored audit values must be absent from history.
4. The originator and Company admin retain read access without action buttons;
   handcrafted action POSTs are also refused. An unassigned Manager cannot review.
5. Another company's NCR stays inaccessible. Repeated POSTs add no duplicate audit.

## Verification

```powershell
.\.local-python\python.exe -m pytest --ds=tests.settings -p no:cacheprovider
.\.local-python\python.exe manage.py check --settings=tests.settings
.\.local-python\python.exe manage.py makemigrations --check --dry-run --settings=tests.settings
.\.local-python\python.exe -m ruff check .
.\.local-python\python.exe -m ruff format --check .
```

Full suite: **487 passed in 91.17s**, including 55 lifecycle tests and the Q42
regressions. Django system checks report no issues; migration drift reports no
changes; Ruff lint and formatting checks pass.

Independent review: **PASS, 0 Blockers, 0 Should fix, 0 Minor**. The reviewer
independently passed all 219 lifecycle and list/detail tests in 71.92s, plus Django
system and migration-drift checks. The full 487-test result above is the main
agent's run. The Q42 display finding is closed; no unresolved findings remain.

Tests use isolated SQLite; PostgreSQL locking/concurrency and a live browser have
not been verified here. No development database migrations or live records were
created by this item.

# Phase 3: supplier-response schema foundation

This is the storage prerequisite for N3 dispatch, implementing approved section
2.12 without exposing an incomplete supplier workflow. The response belongs to
one NCR forever and stores the four supplier text fields, submission timestamps
and count, latest review decision/comment/reviewer, acceptance time and normal
tenant timestamps.

## Guarantees

- One response per NCR and a technical company/id key for later child references.
- Status/decision lists, nonnegative submission count and complete submission and
  review stamp bundles. Partial bundles are refused, including NULL corner cases.
- Accepted status, decision and time agree. Revision requires a non-whitespace
  comment and matching decision. Under Review requires a review-start timestamp.
- PostgreSQL compound keys enforce the company/NCR/supplier triple, dispatched
  and submitting contacts from that supplier, and same-company reviewer membership.
  SQLite insert/update guards enforce the same boundaries. Any active contact of
  the same supplier can later submit under Q16; the sent-to contact stays fixed.
- A database trigger rejects changes to supplier text when the stored OLD status
  is Submitted, Under Review or Accepted. Combining status unlock and text edit
  in one update cannot bypass it. Review/status metadata can still change without
  changing locked text.
- No physical deletion, identity/sent-to-contact movement or first-submission
  rewrite after it has been set. Normal ORM retention rules also apply.

The database checks membership of the company, not a reviewer's current Quality
role or active status. Those actor checks remain obligations of future review
services. Supplier writable-parent checks likewise belong to authenticated save
and submit services. Response INSERT does not require the NCR already Awaiting
Supplier: N3 creates both changes in its transaction, response first.

## Staged application boundary

No public route, admin registration, creation service, supplier form or write
endpoint is introduced. The conditional text-save method and explicit four-field
form arrive with authenticated supplier access; audited R1 creation belongs to
the upcoming N3 transaction. This item sends no mail, creates no notification and
issues no magic link or supplier session. It creates no response business row.
Existing internal and external NCR screens keep their current behaviour.

## Files and running

- `apps/ncr/models.py`: SupplierResponse.
- `apps/ncr/migrations/0006_supplier_response.py`: table/checks.
- `apps/ncr/migrations/0007_response_integrity.py`: compound keys and triggers.
- `tests/test_supplier_response_schema.py`: direct database invariants.
- `tests/test_supplier_response_migrations.py`: guard rollback/reapply and full
  response-table recreation in an isolated subprocess database.
- `docs/data-model.md`: physical/staging mapping recorded before the model.

From the inner project folder with the virtual environment active and PostgreSQL
running, apply with `python manage.py migrate`. No new page or button is expected
from this schema-only item. The next dispatch implementation can create the
response inside the same audited transaction as the NCR state change.

## Verification

83 focused tests passed in 15.54s: 81 schema cases plus the response and existing
audit migration regressions. Independent review PASS: 0 Blockers, 0 Should fix,
0 Minor; reviewer independently passed all 83 in 19.66s plus Django/drift checks.

The main agent's full suite passed **1,098 tests in 86.10s**, with Django system,
migration-drift, Ruff lint and formatting checks clean. Both new migrations applied
successfully to local PostgreSQL. A rolled-back PostgreSQL verification accepted
all five valid states and rejected 25 invalid operations across content locking,
combined unlock attempts, company/supplier/contact/reviewer keys, partial/NULL
stamps, whitespace comments, identity/first-submission changes and physical delete.
No verification business records or supplier responses were retained. These live
results are separate from the independent reviewer's SQLite run.

SQLite tests do not prove PostgreSQL lock concurrency. Supplier authentication,
write APIs, dispatch delivery, browser interaction and response-loop behaviour
are not claimed by this schema foundation.

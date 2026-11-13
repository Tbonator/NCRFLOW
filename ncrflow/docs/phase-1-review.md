# Phase 1 implementation consistency review

Date: 2026-11-13.

The owner explicitly authorised an independent model-reviewer on 2026-11-13.
The first independent review returned **PASS: 0 Blockers, 1 Should fix, 0 Minor**.
The reviewer independently ran all 106 pre-fix tests plus Django/migration checks.
The finding was reproduced, corrected and submitted for independent re-review.
The owner's earlier PASS concerned the data model; it is distinct from this review.

Final independent re-review: **PASS — 0 Blockers, 0 Should fix, 0 Minor**.
The reviewer closed the idle-session finding and independently reran all 118 tests,
Ruff, Django system checks and migration consistency successfully. Live PostgreSQL
execution/concurrency remains unverified; this PASS does not claim those checks ran.

## Correction

**Should fix, resolved:** AuditEntry lacked the approved history index
`(company_id, object_type, object_id)` from data-model.md section 5. Added it to
the model and generated `tenants/0003_auditentry_tenants_audit_object_history.py`.
Existing migrations were not edited. No business schema requirement changed.

**Should fix, resolved and independently verified:** deactivating and reactivating an idle
user without an intervening browser request restored the old session. Added a
platform-only transactional account-status service that audits changes and revokes
all target DB sessions. Login and company switching now coordinate session
persistence under the same User lock; rejected staged logins and stale switches
clear auth state. This covers internal and Django admin login through a registered
user_logged_in receiver. No schema change was needed. The supported status-change
boundary is the new service, not direct ORM/SQL flag edits.

Re-review also identified and corrected a lock-order hazard in the first version
of the status service. It now uses `select_for_update(no_key=True)` for actor/target
users, allowing the audit foreign-key key-share locks held by company operations
while retaining serialization with login and company-switch User locks. The
reviewer confirmed this against PostgreSQL's documented lock table and the installed
Django backend. This is static compatibility analysis, not live concurrency evidence.

## Checks performed

- Compared User, Company, Membership and AuditEntry fields and constraints with
  the approved Phase 1 definitions, including the documented global-user exception
  and later-phase audit foreign keys.
- Checked email uniqueness/lowercase storage, one role per company membership,
  unique company/user membership, escalation thresholds, physical table mapping
  and company/object audit-history lookup.
- Checked the tenant bootstrap exception, per-request active membership ownership,
  session selection, default tenant protection, developer admin separation and
  role refresh. Company URLs and form values do not establish tenant authority.
- Checked transactional provisioning, active/role rechecks, audit rollback,
  profile preservation for existing global users, append-only audit controls
  and membership identity/retention guards.
- Inspected PostgreSQL and SQLite guard migration branches. Only SQLite guards
  were executed by the tests; inspection is not PostgreSQL execution evidence.

## Verification after the correction

All 106 tests passed after the index correction. The session correction adds
12 tests, and all 118 now pass with isolated SQLite. These cover idle revocation,
other-user session preservation, global status permissions/audits/rollback,
staged login rejection, pre-response persistence, stale company switches and late
saves after session deletion. Unsupported session backends fail explicitly.
The index migration was applied only to the isolated test database. Session fixes
add no migration; final Django/migration/lint/format checks passed. The independent
reviewer separately verified the final suite and checks.

The configured PostgreSQL endpoint remains unreachable from the current command
environment; the last socket probe timed out. This does not establish whether a
server is absent or access is restricted. PostgreSQL execution and concurrent
row-lock behaviour still require validation with database access.

## Next review and build scope

Independent re-review is complete with a clean PASS and no remaining findings.
Phase 2 has not been started; the proposed item below is ready for the owner's
approval under CLAUDE.md's per-item workflow. Production database validation
remains an explicit limit, rather than being implied by the review outcome.

The proposed first Phase 2 item is limited to the approved Region, Discipline and
Classification reference data:

1. Add their documented company keys, case-insensitive unique names, technical
   parent keys, active flags and Classification SLA-day constraint.
2. Add Quality-only, company-scoped reference-data management with explicit forms
   and transactional create/change audit entries.
3. Verify company isolation, role refusal, duplicate names, valid SLA values,
   retention and atomic auditing; compare migrations with the approved model.

NCR creation, numbering, lifecycle and list/detail screens are later Phase 2 items.
No Phase 2 application code was added during this review.

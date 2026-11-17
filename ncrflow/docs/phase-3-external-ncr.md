# Phase 3: external NCR creation and pre-dispatch review

This item enables N1 external creation, N2 internal review, N8 cancellation and
the applicable Q38 edits using the supplier schema already applied locally.
It adds no migrations. Dispatch, response creation, magic links, supplier sessions
and all supplier-facing access remain subsequent items. No email is sent here.

## Creation and read scope

The owner explicitly approved supplier selection for every active internal role
on 2026-11-14. `/ncrs/new/external/` requires an active supplier in the verified
current company. The picker exposes supplier names and IDs; it exposes no contacts
or email addresses and grants no supplier-directory access. Directory management
remains Company-admin-only.

The internal route continues to fix direction=internal and no supplier. The new
route fixes direction=external and requires the supplier. Posted direction,
company, originator, status, number or due-date values cannot override those
server decisions. External and internal NCRs share one company number series and
the same company-local calendar SLA basis. Company/member, reference, manager,
supplier and number-counter checks/locks occur in one creation transaction. The
supplier is rechecked before number allocation. Report, number and creation audit
roll back together on failure; the snapshot includes supplier_id.

Internal-user visibility remains the existing company-scoped rule: creator sees
own reports, Manager sees assigned or own, Quality and Company admin see all.
The receipt remains creator-only. The supplier appears in list/detail/receipt;
undispatched reports show Not dispatched. Q42 keeps audit display plain.

## Workflow and editing

- Assigned active Manager or Quality starts Open → In Review through the named
  model method, with timestamp and status audit in the same transaction.
- External NCRs cannot use the internal close path. External closure awaits N6's
  accepted-response implementation. No close button is offered for an external NCR.
- Quality can cancel from any of the four named nonterminal statuses. Reasons
  remain optional and stored; no notification is created. Terminal states cannot
  reopen.
- Q38 descriptive/routing edits work in Open/In Review with existing creator,
  assigned Manager and Quality permissions. Supplier and SLA/identity fields stay
  fixed. In supplier-waiting statuses only Quality manager reassignment is allowed.
  Revision checking, inactive historical choices and atomic field audits remain.

Shared services now use `ncr_actions_for`, `_transition_ncr` and `update_ncr`.
The model's named transition methods and HTTP action URLs stay the same.
`create_internal_ncr` and `create_external_ncr` delegate to a private shared N1
implementation. Existing internal behaviour remains covered by its original tests.

## Files and trying it

Changes: `apps/ncr/forms.py`, `services.py`, `models.py` (methods only), `views.py`,
`urls.py`; create/receipt/list/detail and workspace templates; a type-switch style
in `static/css/app.css`; and `tests/test_external_ncr_workflow.py`. Existing tests
were updated only for shared service/context names. Requirements were recorded
first in `docs/data-model.md`; the supplier-picker question is resolved in
`docs/phase-3-suppliers.md`.

1. As Company admin, add an active supplier under Manage suppliers.
2. As any active company member, choose Raise an external NCR on the workspace,
   NCR list or the type switch on the creation form. Select that supplier and save.
3. Confirm Open, the company NCR number, due date, supplier and Not dispatched.
4. As its assigned Manager or Quality, start review. Quality may edit or cancel;
   an external NCR cannot be closed at this stage.
5. Confirm other companies cannot open it. Other roles can select the supplier
   while their supplier-directory URLs remain refused.

## Verification

The full suite passed **1,016 tests in 97.15s**, including 55 new external-NCR
tests. Django system and migration-drift checks, Ruff lint and formatting pass.
The earlier targeted creation/workflow/edit/browsing run passed 393 tests before
seven HTTP-scope checks were added. Independent review: **PASS, 0 Blockers,
0 Should fix, 0 Minor**. The reviewer independently passed 229 targeted tests
in 18.79s, plus Django system and migration-drift checks. Full-suite and live
PostgreSQL/HTTP results below are the main agent's separate checks.

Tests cover scoped picker privacy, active
supplier validation/recheck, fixed direction/server fields, SLA and numbering,
audit rollback, review/cancellation actors, external-close refusal, waiting-state
edit whitelists, receipt/list/detail access, escaping, authentication and CSRF.

Live PostgreSQL verification created a temporary supplier and external NCR,
reviewed it as the assigned Manager, proved that Quality could not close it,
edited and cancelled it as Quality, then verified the linked audits. The entire
transaction was rolled back, including its NCR number, and existing record/count
values were checked afterward. Live HTTP checks verified both creation forms for
all four demo roles. No permanent verification records were created.

Concurrent-request tests and interactive browser layout checks remain unverified.

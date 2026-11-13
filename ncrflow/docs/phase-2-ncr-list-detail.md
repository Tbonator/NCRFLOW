# Phase 2: internal NCR list and detail screens

This item adds two read-only screens: a list of the NCRs a signed-in member may
see in their current company, and a detail page for one NCR with its audit
history. It introduces no status transitions, no editing, no supplier feature
and no email. There are no model changes and no migrations.

## Behaviour

### Who sees what

Visibility is always limited to the user's current company, taken from the
server-side tenant context (never from the URL or a form). The user account and
the membership must both be active.

- Originator: NCRs they raised.
- Manager: NCRs where they are the responsible manager, plus any they raised.
- Quality: every NCR in the company.
- Company admin: every NCR in the company.

The rule lives in one function, `visible_ncrs_for(*, by, actor_membership)` in
`apps/ncr/services.py`. It reloads company, account and membership authority
with the existing `require_company_role` helper, reads the role fresh from the
database and returns a company-filtered queryset. Both screens are built on it;
no view or template repeats the rule.

### List screen: `/ncrs/`

- Table columns: number, subject, status, classification, responsible manager,
  due date. Each number links to the detail page.
- Default order is due date ascending, then NCR sequence number (the numeric
  part of the number, so `NCR-100000` still sorts after `NCR-99999`).
- Status filter: a `status` GET parameter validated by `NCRStatusFilterForm`.
  An unknown value shows the form's validation message and applies no filter;
  it never causes an error page.
- "Overdue" marker: shown when the due date is before today's date in the
  company's timezone and the status is neither Closed nor Cancelled.
- 25 rows per page using Django's `Paginator`. Page links keep the chosen
  status. An out-of-range or non-numeric `page` falls back to a valid page.
- Empty states: one message when the user has nothing to see, another when the
  chosen status has no matches.
- Classification and responsible manager are loaded with `select_related`, so
  the number of queries does not grow with the number of rows.

### Detail screen: `/ncrs/<uuid>/`

- The NCR is looked up by `public_id` through the shared visibility queryset.
  An NCR that is not visible to the user, belongs to another company or does
  not exist returns the same 404.
- Shows number, status, subject, description, direction, classification,
  region, discipline, originator, responsible manager, part number, job
  number, due date (with the overdue marker) and the created time in the
  company's timezone.
- Audit history: read by
  `audit_history_for(*, by, actor_membership, report)`. It does not trust the
  report it is handed: it reloads the verified company with
  `require_company_role`, filters `AuditEntry` by that company and this NCR,
  and returns nothing when the NCR is in another company or is not in the
  actor's `visible_ncrs_for` queryset. Entries are
  oldest first, with when, who, what (action, plus field name and
  comment when present), old value and new value. Values are shown exactly as
  stored; the creation entry's new value is the JSON snapshot written by the
  creation service.
- The page contains no form or button that changes data. Both views accept
  GET only.

### Navigation

- The header shows an "NCRs" link for signed-in members with a current company.
- The workspace has a "View NCRs" button.
- The creation receipt links to the new detail page. The receipt route
  `/ncrs/<uuid>/created/` and its originator-only behaviour are unchanged.

## Files changed

- `apps/ncr/services.py`: `company_today`, `visible_ncrs_for`, `ncr_list_for`,
  `ncr_details_for`, `audit_history_for` and the private `_with_overdue_flag`.
- `apps/ncr/forms.py`: `NCRStatusFilterForm`.
- `apps/ncr/views.py`: `ncr_list` and `ncr_detail` (thin, GET only).
- `apps/ncr/urls.py`: `ncr:list` at `/ncrs/` and `ncr:detail` at
  `/ncrs/<uuid:public_id>/`. `ncr:create` and `ncr:created` are unchanged.
- `templates/ncr/list.html`, `templates/ncr/detail.html`: new.
- `templates/base.html`, `templates/tenants/workspace.html`,
  `templates/ncr/created.html`: navigation links.
- `static/css/app.css`: `.filter-form`, `.overdue-marker`, `.stacked-card`,
  `.audit-value`. Status badges reuse the existing `.status-label`.

Status is always written as text; the badge colour is decoration only. The
overdue marker is the word "Overdue". All user-supplied text relies on Django's
automatic escaping; `|safe` is not used.

## Running and manual checks

With PostgreSQL and `.env` configured, start the server from the inner project
directory. This item adds no migrations.

```powershell
python manage.py migrate
python manage.py runserver
```

1. Sign in as an Originator who has raised an NCR. Select **NCRs** in the
   header. Only that user's NCRs are listed. Open one and confirm the details
   and the "Created" audit entry.
2. Sign in as a Manager. The list shows NCRs assigned to that Manager and any
   the Manager raised, and nothing else.
3. Sign in as Quality, then as Company admin. Each sees every NCR in the
   company.
4. Choose a status in the filter and select **Filter**. Edit the address to
   `?status=nonsense`: the page still loads, shows a validation message and
   lists everything.
5. Copy a detail address, sign in as an Originator who did not raise that NCR
   and paste it: the result is "Not found". The same applies after switching to
   another company.
6. An NCR whose due date has passed and is not Closed or Cancelled shows
   "Overdue" in the list and on its detail page.
7. Raise a new NCR and confirm the receipt page still appears and its
   **View this NCR** link opens the detail page.

## Verification

Commands run by the builder from the project directory after the change:

```powershell
.\.venv\Scripts\python.exe -m pytest --ds=tests.settings -p no:cacheprovider -q
.\.venv\Scripts\python.exe manage.py check --settings=tests.settings
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run --settings=tests.settings
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Results: the existing suite reported `268 passed in 28.01s` on isolated SQLite;
`check` reported "System check identified no issues (0 silenced)";
`makemigrations --check --dry-run` reported "No changes detected"; `ruff check`
reported "All checks passed!"; `ruff format --check` reported "71 files already
formatted".

After the reviewer follow-up that changed `audit_history_for` to
`audit_history_for(*, by, actor_membership, report)`, the builder re-ran the
same commands with the test-writer's new file present: `2 failed, 397 passed in
82.77s`. Both failures are `TypeError` from tests still calling the old
one-argument signature (`test_detail_audit_history_is_only_for_that_ncr` and
`test_same_numbered_ncr_in_another_company_does_not_share_history` in
`tests/test_ncr_list_detail.py`); the test-writer owns that update. `check`,
`makemigrations --check --dry-run` and both Ruff commands passed (72 files
already formatted).

The builder also ran a temporary, uncommitted smoke script against isolated
SQLite covering the four role rules, cross-company and not-visible 404s, the
invalid status filter, the overdue marker either side of local midnight,
pagination, escaping and the unchanged receipt. It is not part of the test
suite. Permanent tests for this item are the test-writer's job and had not been
written when this document was produced.

Not verified: behaviour on PostgreSQL, and the screens in a real browser
(layout, keyboard and screen-reader behaviour were not checked by hand).

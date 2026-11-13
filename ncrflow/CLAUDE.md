# CLAUDE.md

Project instructions for Claude Code. Read this fully before making changes.

## 1. What we are building

A multi-tenant SaaS for non-conformance reporting (NCR), aimed at small and mid-size manufacturers in South Africa.

Working name: `ncrflow` (placeholder, may change).

Core idea: a company raises an NCR, internally or against a supplier. The supplier responds through a free portal using an emailed link, with no password. The company reviews the response and closes the NCR. Suppliers can also raise NCRs against the company.

What makes it different from existing tools:
1. Supplier portal included at a low price, with free and unlimited supplier logins.
2. Passwordless supplier access.
3. Suppliers can raise NCRs back against the customer.
4. Quality-first, not a safety suite with NCR bolted on.

## 2. Working style

- The owner is a solo developer who is strong in SQL, BI and Power Platform, and newer to Django. Explain what you are doing step by step, in plain language.
- Before a large change, give a short numbered plan and wait for approval.
- Work in small steps. One phase item at a time. Do not jump ahead to later phases.
- After each change, say which files changed, how to run it, and how to test it.
- Prefer boring, standard Django. No clever abstractions.
- If a requirement is unclear, ask. Do not guess business rules.
- Write all code, names and text from scratch. Never copy code, names, branding or data from any previous employer's system.

## 3. Tech stack

- Python 3.12 or newer
- Django (current LTS release)
- PostgreSQL
- Django templates with HTMX for interactivity. No React or separate frontend.
- Celery with Redis for background jobs and the daily schedule (Celery beat)
- WeasyPrint for PDF export
- pytest and pytest-django for tests
- ruff for linting and formatting
- Docker Compose for local Postgres and Redis
- Environment variables through a `.env` file (never commit it)

## 4. Project layout

```
config/          settings, urls, celery
apps/
  tenants/       Company, membership, tenant middleware
  accounts/      custom User, roles, magic-link login
  suppliers/     Supplier, SupplierContact
  ncr/           NCR, SupplierResponse, Evidence, status transitions
  sncr/          supplier-raised NCRs
  notifications/ email templates and send functions
  escalation/    daily overdue reminder and escalation tasks
  reports/       PDF export, dashboards, supplier scorecard
templates/
static/
tests/
```

## 5. Non-negotiable architecture rules

### 5.1 Multi-tenancy
- Shared database, shared schema.
- Every business model has a required `company` foreign key.
- Every query must be filtered by the current company. Use a base model and a custom manager (`for_company(company)`) so this is hard to forget.
- Never trust a company id from the URL, form or request body. Take it from the logged-in user's membership.
- Every feature needs a test proving company A cannot see or change company B's data.

### 5.2 Supplier scoping
- A supplier contact can only see NCRs where that contact's supplier is the NCR's supplier, inside one company.
- Scoping is enforced in the queryset on the server, in one shared place. Never rely on hiding things in templates or JavaScript.
- Every supplier view needs a test proving supplier X cannot open supplier Y's NCR, including by guessing the URL.
- Use UUIDs in supplier-facing URLs, not sequential ids.

### 5.3 Supplier writes
- Suppliers may only edit a fixed whitelist of fields on their own response. Use explicit form fields, never `fields = "__all__"`.
- Once a response is submitted it is locked on the server. A locked response rejects edits even if the request is hand-crafted.

### 5.4 Status changes
- Status changes happen only through named transition methods on the model (for example `ncr.send_to_supplier(by=user)`).
- Each method checks the current status, checks the user's role, saves, writes an audit entry, and triggers the right email.
- Views never set `status` directly.

### 5.5 Audit trail
- Every create, status change and review decision writes an `AuditEntry` (who, what, when, old value, new value). Audit entries are never edited or deleted.

## 6. Roles

Internal (belong to a company):
- **Originator**: raises NCRs, sees their own.
- **Manager**: responsible manager for assigned NCRs, reviews and acts.
- **Quality**: sees everything in the company, reviews supplier responses, closes NCRs, manages reference data.
- **Company admin**: manages users, suppliers and settings.

External:
- **Supplier contact**: responds to NCRs raised against their supplier and raises supplier NCRs. No password. Not billed.

## 7. Domain model (first version)

- `Company`: name, timezone (default `Africa/Johannesburg`), settings.
- `User`: custom user model from the first migration. Email is the login.
- `Membership`: user, company, role.
- `Supplier`: company, name, region, active.
- `SupplierContact`: supplier, name, email. Unique on (company, email) to prevent duplicates.
- `Classification`: company, name, SLA days. Drives the NCR due date.
- `NCR`: company, number (per-company sequence such as `NCR-00001`), subject, description, direction (internal or external), classification, discipline, region, originator, responsible manager, supplier (nullable for internal), part number, job number, due date, status, timestamps.
- `SupplierResponse`: NCR, status, root cause, corrective action, preventive action, comments, submitted at, review decision, review comment.
- `Evidence`: file, title, document type, uploaded by, linked to a response or an SNCR.
- `SupplierNCR` (SNCR): company, number (`SNCR-0001`), supplier, raised by contact, title, type, priority, description, business impact, status, assigned to, investigation findings, root cause, corrective action, resolution summary, supplier accepted (boolean), supplier closure comment, dates.
- `QualityRoute`: company, user, region, discipline (or "All"), active. Used for escalation routing.
- `AuditEntry`: company, actor, object, action, old value, new value, timestamp.

## 8. Lifecycles

### NCR status
`Open` > `In Review` > `Awaiting Supplier` > `Awaiting Internal Review` > `Closed`
`Cancelled` is allowed from any status before `Closed`.
Internal NCRs skip the supplier steps.

### Supplier response status
`Draft` > `Submitted` > `Under Review` > `Accepted`
`Under Review` > `Revision Required` > back to `Draft` (unlocked for the supplier) > `Submitted`

### SNCR status
`Draft` > `New` > `Assigned` > `Under Investigation` > `Action Identified` > `Resolved` > `Closed`
`Under Investigation` <> `Pending Supplier Information`
From `Resolved` the supplier either accepts (goes to `Closed`) or reopens with a required comment (back to `Under Investigation`).

## 9. Key workflows

### 9.1 Dispatching an external NCR
When an external NCR is sent to a supplier, one service function does all of this in a single database transaction:
1. Find the supplier contact by (company, email), or create it.
2. Create the `SupplierResponse` in `Draft`.
3. Send one email to the contact containing a signed, expiring link straight to that NCR.
4. Notify the originator and responsible manager.

Rules:
- It must be idempotent. Running it twice for the same NCR creates no duplicates and sends no second email.
- Emails are sent after the transaction commits (`transaction.on_commit`).
- Always resolve the supplier's email from the `SupplierContact`, never from a free-text field.

### 9.2 Magic-link access
- Use Django's signing tools to create a time-limited token tied to the contact.
- Opening the link logs the contact in to a supplier session and lands them on the NCR in the link.
- An expired or invalid link shows a friendly page with a "send me a new link" button. Never show a raw error.
- If a session expires mid-form, tell the user clearly and offer a new link.

### 9.3 Review loop
- Supplier submits: response locks, NCR moves to `Awaiting Internal Review`, internal team is emailed, supplier gets an acknowledgment email.
- Quality accepts: supplier is emailed.
- Quality requests revision (comment required): response unlocks, supplier is emailed with the comment.
- NCR closed: originator and supplier are both emailed.

### 9.4 Overdue reminders and escalation
One daily Celery beat task, 07:00 in the company's timezone, skipping Saturdays and Sundays:
- Overdue (past due date, not closed): remind the responsible manager, and the supplier if it is waiting on them.
- Tier 1, 3 or more days overdue: escalate to the responsible manager.
- Tier 2, 7 or more days overdue: escalate to the quality users matched in `QualityRoute` by region and discipline, including "All".
- Repeats each working day until closed.
- The day thresholds are company settings, not hardcoded.

### 9.5 Supplier-raised NCRs
- A logged-in supplier contact can raise an SNCR with evidence, save a draft, and submit.
- Uploading evidence before the first save should save the draft automatically.
- Quality users are notified on submit. The supplier is notified on each status change they care about, and each notification is sent only once per change.
- The supplier sees a "My NCRs" and "My SNCRs" list with clear status and an "awaiting your response" flag.

## 10. Build phases

Work through these in order. Do not start a phase until the previous one has passing tests.

0. **Data model first.** Produce `docs/data-model.md` covering every table needed for phases 1 to 6. The project skeleton already exists (settings, eight empty apps, Docker Compose, a smoke test), so do not recreate it. Write no models, no migrations and no feature code in this phase, and do not run `migrate`. Phase 0 is finished only when the model-reviewer returns PASS and the owner adds `Status: APPROVED` to the top of the file.
1. Custom User (set `AUTH_USER_MODEL` before the first migration), Company, Membership, roles, tenant middleware, login for internal users. The skeleton, settings and Docker Compose are already in place.
2. Classification, NCR model, numbering, internal lifecycle, SLA due date, list and detail screens per role.
3. Supplier, SupplierContact, dispatch function, magic link, supplier response page, evidence upload.
4. Review loop and all status emails.
5. Daily overdue reminders and two-tier escalation.
6. SNCR feature end to end.
7. PDF export, dashboard, supplier scorecard (NCR count, response time, repeat issues per supplier).
8. Self-signup and billing. Not before a customer asks for it.

Phases 1 to 4 are the MVP.

## 11. Coding conventions

- Business logic lives in `services.py` functions and model methods. Views stay thin.
- Class-based or function-based views are both fine. Pick the simpler one for the case.
- Use Django forms for all input validation.
- All datetimes stored in UTC. Display in the company's timezone.
- File uploads: validate type and size, store outside the web root, serve only through a view that checks permissions.
- Emails: HTML plus plain text. Use a text wordmark in the header, not an image logo, because mail gateways block images.
- No secrets in code. Read them from the environment.
- Migrations are committed. Never edit a migration that has been applied.
- Use clear, full names. No abbreviations a new reader would not understand.

## 12. Testing

- Every service function and status transition has tests.
- Required tests for every feature that touches data:
  1. Cross-company isolation.
  2. Cross-supplier isolation.
  3. Role checks (who is allowed, who is refused).
- Dispatch has an idempotency test.
- Escalation tests cover weekends, both tiers, and closed NCRs being skipped.
- Use `freezegun` or Django's time helpers for date logic.
- Run the full test suite before saying a task is done.

## 13. Commands

```
docker compose up -d            # start Postgres and Redis
python manage.py migrate
python manage.py runserver
celery -A config worker -l info
celery -A config beat -l info
pytest
ruff check .
ruff format .
```

Update this section if the commands change.

## 14. Do not

- Do not write any application code until `docs/data-model.md` says `Status: APPROVED`.
- Do not change a Django model without changing `docs/data-model.md` first.
- Do not add a feature that is not in the current phase.
- Do not add audits, document control or training modules. They are out of scope.
- Do not add a JavaScript framework.
- Do not write a query on a business model without a company filter.
- Do not set a status field directly from a view.
- Do not send email inside a database transaction.
- Do not commit `.env`, uploaded files or real customer data.
- Do not mark work as done with failing tests.

## 15. Agents

Four subagents live in `.claude/agents/`. The main session coordinates them and reports to the owner. It does not do their work itself.

| Agent | Job | Can write to |
|---|---|---|
| `data-modeller` | Designs the data model | `docs/` only |
| `model-reviewer` | Independent review, read-only | nothing |
| `app-builder` | Writes Django code from the approved model | application code |
| `test-writer` | Writes and runs tests from the requirements | `tests/` only |

### Phase 0 loop
1. `data-modeller` drafts `docs/data-model.md`.
2. `model-reviewer` reviews it and returns PASS or FAIL with findings.
3. On FAIL, `data-modeller` fixes the findings. Repeat from step 2.
4. On PASS, show the owner a short summary, the open questions and the reviewer's findings. Wait.
5. The owner answers the questions and adds `Status: APPROVED`. Only then does Phase 1 start.

### Phases 1 to 7 loop, per phase item
1. `app-builder` builds one item.
2. `test-writer` adds and runs tests.
3. `model-reviewer` checks the models and migrations for drift from `docs/data-model.md`.
4. Fix, then repeat until tests pass and the reviewer returns PASS.
5. Report to the owner and wait for approval before the next item.

### Changing the model later
Any change to a table, field, constraint or lifecycle goes back through `data-modeller` and `model-reviewer` first, with a line in the change log. Code follows the document, never the other way round.

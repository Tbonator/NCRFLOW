# Phase 1: internal login and company workspace

Internal users can sign in with their provisioned email/password, select an
authorised company and enter its workspace. These screens use Django templates
and ordinary forms; no separate frontend or signup feature is introduced.

## Routes

| URL | Behaviour |
|---|---|
| `/` | Public introduction with a sign-in/workspace link |
| `/accounts/login/` | Email/password login, with Django session and CSRF protection |
| `/accounts/logout/` | POST-only logout with CSRF validation; clears identity and company |
| `/companies/select/` | GET displays own active memberships; POST selects/switches after verification |
| `/companies/no-access/` | Friendly 403 when an authenticated account has no active company |
| `/workspace/` | Selected company's name, current role, timezone and the user's own account details |
| `/admin/` | Separate Django platform administration for the developer |

One eligible membership is selected automatically. Multiple memberships require
selection before company views are allowed. Invalid or revoked selections are
cleared and do not silently choose another company in the same request. Selection
rotates the session key and reloads membership ownership/active checks after form
validation. Login always routes through membership resolution and ignores submitted
`next` destinations. Company URLs and role fields never override tenant context.

`TenantMiddleware` runs after Django authentication/messages middleware. It sets
`request.company` and `request.membership` only from the authenticated user's
active membership in an active company. Membership roles are reloaded on every
request, not cached in the session. Company-specific timezone activation is
restored after the request. Authenticated responses are marked private/no-store.
Inactive logins lose their existing session even if the account is later reactivated
when deactivated through the supported platform account-status service described below.

Future company views require tenant context by default. The current exceptions
are the named public/auth/bootstrap routes and Django admin; future supplier
routes must add their own supplier authentication/scoping rather than bypassing
checks broadly. Company views must continue to filter business querysets through
`for_company(request.company)` and use services for role-bound writes. Membership
middleware is not a substitute for those per-action permission checks.

All company roles may open their own workspace. Company admin is not platform
staff. A developer superuser with no company membership may enter `/admin/` but
cannot enter a company's workspace or choose another user's membership.

## Running

After normal Python/PostgreSQL setup from the README, apply the project migrations
and provision a company account using `phase-1-tenants.md`. Supply a valid initial
password for a new account; users with unusable passwords cannot log in.

```powershell
python manage.py migrate
python manage.py runserver
```

Open `http://127.0.0.1:8000/accounts/login/`. This session did not migrate a
development database or create real user/company accounts. Do not use
`tests.settings` to run the app: its database is in memory and its password hasher
is test-only. On this workstation, the local runtime described in
`phase-1-accounts.md` can replace `python` with `.\.local-python\python.exe`.

## Validation

```powershell
python -m pytest --ds=tests.settings -p no:cacheprovider
python manage.py check
python manage.py makemigrations --check --dry-run --settings=tests.settings
python -m ruff check .
python -m ruff format --check .
```

The full SQLite suite passed all 106 tests, including forged session/form
selections, another user's membership in the same company, revoked company/user/
membership access, role refresh, selection recheck after form validation, CSRF,
logout/session invalidation, open redirects, cache control, company-name escaping
and request-local timezone restoration. Existing account/provisioning/audit tests
also pass. Django checks and migration consistency passed; no new schema migration
was needed for this item.

PostgreSQL execution/concurrency remains outstanding. Independent Phase 1 re-review
returned a clean PASS after the session correction below. No NCR,
supplier, signup or billing screens are added by this Phase 1 item.

The subsequent Phase 1 consistency review added the missing approved audit-history
index in `tenants/0003`; all 106 tests passed again. See `phase-1-review.md` for
the correction and the remaining review/validation status.

## Account deactivation and reactivation

Use the platform-only service for global account status changes:

```python
from apps.accounts.services import set_account_active

set_account_active(by=developer, user_id=user.pk, active=False)
set_account_active(by=developer, user_id=user.pk, active=True)
```

Deactivation revokes every live database session for that login immediately and
audits the change in its membership companies. Reactivation preserves the password
but requires a fresh login. Company administrators may deactivate their own
company memberships through the existing membership service; they cannot change
a shared account's global status.

Do not change User.is_active directly through ORM/SQL for application operations:
those writes bypass lifecycle auditing and session revocation. This service
supports the standard database session backend only. Both login implementations
and company switching persist sessions under the same User lock as deactivation;
an old in-flight request cannot recreate a deleted authenticated session.
The follow-up suite has 118 passing tests, including the idle reactivation case
reported by the independent reviewer. Independent re-review closed that finding
with 0 Blockers, 0 Should fix and 0 Minor; see `phase-1-review.md`.

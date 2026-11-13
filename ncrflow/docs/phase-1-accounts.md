# Phase 1: custom User, first item

The custom model is `apps.accounts.models.User`, selected by
`AUTH_USER_MODEL = "accounts.User"`. Its initial migration creates `accounts_user`
with the approved columns and lowercase-email/platform-flag constraints.
There is no username column. Email is stripped and lowercased when creating or
saving users and when looking up an email for authentication. Database constraints
also reject uppercase email or mismatched staff/superuser flags when a caller
bypasses model saving.

Passwords are hashed by Django. A user created without a password gets an
unusable password. `create_superuser` requires a password and active/staff/superuser
flags. Inactive users cannot authenticate through Django's standard backend.
Framework permission join tables come from PermissionsMixin; they do not replace
the approved company membership roles.

## What this item does not yet provide

Company, Membership, AuditEntry and tenant-scoped provisioning are now implemented
in the next item; see `phase-1-tenants.md`. Internal login, tenant middleware and
company selection are also implemented; see `phase-1-login.md`. Customer
account-creation and company-user management screens are not implemented yet.
The low-level User manager does not replace the provisioning services'
company and audit checks. Company isolation tests are in `test_tenants.py`;
supplier isolation tests will accompany supplier features. User has no company key.

## Validation

From the inner `ncrflow` directory with a working Python environment:

```powershell
python -m pytest --ds=tests.settings -p no:cacheprovider
python manage.py check
python manage.py makemigrations --check --dry-run --settings=tests.settings
python -m ruff check .
python -m ruff format --check .
```

`tests.settings` uses an in-memory SQLite database and a fast test-only password
hasher. Do not use it for deployment or your development server. Normal settings
retain PostgreSQL and Django's production password hashers. Run `python -m pytest`
with PostgreSQL available to validate against the production database engine.
No development PostgreSQL database was migrated by this item.

On this workstation, both existing virtual environments referenced a missing
Python 3.12 installation. A local official Python 3.12.10 embeddable runtime was
downloaded to the ignored `.local-python/` directory and configured to use the
already installed `.venv/Lib/site-packages`. For this session's commands, replace
`python` above with `.\.local-python\python.exe`. This fallback is local only and
is not included in source control; a normal Python installation and virtual
environment remain the standard setup.

At the first successful run, all 19 tests passed against SQLite. Django's normal
system check and the isolated migration-drift check passed. PostgreSQL execution
has not been verified: neither Docker nor a PostgreSQL server is available in
the current command environment. A subsequent independent Phase 1 implementation
review returned a clean PASS after the account/session revocation correction;
the final suite has 118 passing tests. See `phase-1-review.md` for scope and limits.

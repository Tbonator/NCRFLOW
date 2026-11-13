# Phase 1: company and membership foundations

Company, Membership and AuditEntry follow the approved schema. Membership has
one role per company/user, and the same global login can have memberships in
several companies. Company admin is a company role, separate from the developer's
platform flags. Tenant middleware, company selection and login screens are now
implemented in the next item; see `phase-1-login.md`.

## Changes

- `apps/tenants/models.py`: Company settings, Membership roles, a reusable tenant
  base model and `for_company(company)` queryset, and append-only AuditEntry.
- `apps/tenants/forms.py`: explicit provisioning and membership-change fields,
  timezone/SLA checks, email normalisation and password validation.
- `apps/tenants/services.py`: `provision_company`, `provision_member` and
  `update_membership`, all transactional with audit entries.
- `tenants/0001_initial`: tables, choice checks, threshold checks, unique keys
  and membership lookup index. `tenants/0002_integrity_guards`: PostgreSQL and
  SQLite triggers for audit immutability/actor validity, membership identity
  preservation and company/membership retention.
- `tenants/0003_auditentry_tenants_audit_object_history`: the approved
  company/object-type/object-id history index, added during implementation review.
- `tests/test_tenants.py`: company isolation, role checks, revoked access,
  atomic rollback and direct database integrity checks.

Company provisioning is platform-only and creates the initial Company admin.
Other provisioning requires the acting user's active Company admin membership.
Services reload that user's identity, company and membership before proceeding;
they never use submitted company/platform flags. Existing global users retain
their name, password and flags when another company grants them membership.
The exact-email global lookup is the documented provisioning bootstrap exception.
These services do not expose global user listings or other companies' memberships.

Use provisioning services for business creates and membership changes, rather
than direct ORM writes. Creation snapshots exclude passwords and password hashes.
If auditing fails, the associated create or update rolls back. Membership changes
are audited per changed field; repeated identical provisioning creates no duplicate
membership or audit entry. Inactive memberships require an explicit audited change
instead of automatic reactivation.

## Running locally

From the inner project directory with PostgreSQL available and `.env` configured:

```powershell
docker compose up -d
python manage.py migrate
python manage.py createsuperuser
python manage.py shell
```

Only apply these migrations to the project's intended database. Existing database
migrations are never reset by these instructions. In the Django shell, use the
developer email created in the previous command:

```python
from getpass import getpass
from django.contrib.auth import get_user_model
from apps.tenants.services import provision_company, provision_member, update_membership

developer = get_user_model().objects.get(email="developer@example.com")
company, admin = provision_company(
    by=developer,
    company_data={
        "name": "Example Factory",
        "timezone": "Africa/Johannesburg",
        "escalation_tier_one_days": 3,
        "escalation_tier_two_days": 7,
    },
    admin_data={
        "email": "admin@example.com",
        "full_name": "Company Administrator",
        "password": getpass("Initial company admin password: "),
    },
)
member = provision_member(
    by=admin.user,
    actor_membership=admin,
    data={"email": "member@example.com", "full_name": "Example Member", "role": "originator"},
)
update_membership(
    by=admin.user,
    actor_membership=admin,
    membership_id=member.pk,
    data={"role": "manager", "active": True},
)
```

Without a supplied initial password, a new account has an unusable password.
Supplying a password for an existing global user does not reset that account.
There is no customer signup UI in this phase. Provisioned users can now sign in
at `/accounts/login/`; company selection and workspace behaviour are documented
in `phase-1-login.md`.

## Verification and limits

```powershell
python -m pytest --ds=tests.settings -p no:cacheprovider
python manage.py check
python manage.py makemigrations --check --dry-run --settings=tests.settings
python -m ruff check .
python -m ruff format --check .
```

All 60 tests passed using isolated SQLite, including database-trigger checks.
Django's normal system check, migration-drift check and Ruff passed. This
workstation uses the ignored local runtime described in `phase-1-accounts.md`;
replace `python` with `.\.local-python\python.exe` here if needed.

The configured PostgreSQL port is not reachable in this environment. No
development PostgreSQL migrations were applied. Production PostgreSQL trigger SQL
and concurrent row-lock behaviour remain unverified; with PostgreSQL available,
run `python -m pytest` against its isolated test database before deployment.
Subsequent independent Phase 1 implementation re-review returned a clean PASS
after the account/session correction, with all 118 tests passing; see
`phase-1-review.md`. That review does not establish live PostgreSQL behaviour.

Later migrations adding audit foreign keys must preserve/reinstall these guards
if SQLite rebuilds the audit table. Direct low-level ORM writes do not replace
audited services, even where individual database constraints permit them.

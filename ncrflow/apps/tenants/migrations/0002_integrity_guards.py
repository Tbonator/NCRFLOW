from django.db import migrations
from django.db.utils import NotSupportedError


POSTGRESQL_SQL = """
CREATE FUNCTION tenants_reject_audit_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Audit entries are append-only' USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER tenants_audit_immutable
BEFORE UPDATE OR DELETE ON tenants_auditentry
FOR EACH ROW EXECUTE FUNCTION tenants_reject_audit_mutation();

CREATE FUNCTION tenants_validate_audit_actor() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.actor_user_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM accounts_user u WHERE u.id = NEW.actor_user_id AND (
            (u.is_active AND u.is_staff AND u.is_superuser)
            OR EXISTS (
                SELECT 1 FROM tenants_membership m
                WHERE m.user_id = u.id AND m.company_id = NEW.company_id
            )
        )
    ) THEN
        RAISE EXCEPTION 'Audit actor must belong to the company or be platform superuser'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER tenants_audit_actor_valid
BEFORE INSERT ON tenants_auditentry
FOR EACH ROW EXECUTE FUNCTION tenants_validate_audit_actor();

CREATE FUNCTION tenants_guard_membership() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Memberships must not be deleted' USING ERRCODE = '23514';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id
        OR NEW.user_id IS DISTINCT FROM OLD.user_id THEN
        RAISE EXCEPTION 'Membership company and user cannot change' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER tenants_membership_identity_guard
BEFORE UPDATE OR DELETE ON tenants_membership
FOR EACH ROW EXECUTE FUNCTION tenants_guard_membership();

CREATE FUNCTION tenants_reject_company_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Companies must not be deleted' USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER tenants_company_retained
BEFORE DELETE ON tenants_company
FOR EACH ROW EXECUTE FUNCTION tenants_reject_company_delete();
"""

SQLITE_STATEMENTS = [
    """CREATE TRIGGER tenants_audit_no_update BEFORE UPDATE ON tenants_auditentry
       BEGIN SELECT RAISE(ABORT, 'Audit entries are append-only'); END""",
    """CREATE TRIGGER tenants_audit_no_delete BEFORE DELETE ON tenants_auditentry
       BEGIN SELECT RAISE(ABORT, 'Audit entries are append-only'); END""",
    """CREATE TRIGGER tenants_audit_actor_valid BEFORE INSERT ON tenants_auditentry
       WHEN NEW.actor_user_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM accounts_user u WHERE u.id = NEW.actor_user_id AND (
               (u.is_active AND u.is_staff AND u.is_superuser) OR EXISTS (
                   SELECT 1 FROM tenants_membership m
                   WHERE m.user_id = u.id AND m.company_id = NEW.company_id
               )
           )
       )
       BEGIN SELECT RAISE(ABORT, 'Audit actor must belong to the company or be platform superuser');
       END""",
    """CREATE TRIGGER tenants_membership_identity_guard BEFORE UPDATE ON tenants_membership
       WHEN NEW.company_id != OLD.company_id OR NEW.user_id != OLD.user_id
       BEGIN SELECT RAISE(ABORT, 'Membership company and user cannot change'); END""",
    """CREATE TRIGGER tenants_membership_retained BEFORE DELETE ON tenants_membership
       BEGIN SELECT RAISE(ABORT, 'Memberships must not be deleted'); END""",
    """CREATE TRIGGER tenants_company_retained BEFORE DELETE ON tenants_company
       BEGIN SELECT RAISE(ABORT, 'Companies must not be deleted'); END""",
]


def install_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(POSTGRESQL_SQL)
    elif vendor == "sqlite":
        for statement in SQLITE_STATEMENTS:
            schema_editor.execute(statement)
    else:
        raise NotSupportedError("Tenant integrity guards require PostgreSQL or SQLite.")


def remove_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        for table, trigger, function in [
            ("tenants_auditentry", "tenants_audit_immutable", "tenants_reject_audit_mutation"),
            ("tenants_auditentry", "tenants_audit_actor_valid", "tenants_validate_audit_actor"),
            ("tenants_membership", "tenants_membership_identity_guard", "tenants_guard_membership"),
            ("tenants_company", "tenants_company_retained", "tenants_reject_company_delete"),
        ]:
            schema_editor.execute(f'DROP TRIGGER "{trigger}" ON "{table}"')
            schema_editor.execute(f'DROP FUNCTION "{function}"()')
    elif vendor == "sqlite":
        for trigger in [
            "tenants_audit_no_update", "tenants_audit_no_delete", "tenants_audit_actor_valid",
            "tenants_membership_identity_guard", "tenants_membership_retained",
            "tenants_company_retained",
        ]:
            schema_editor.execute(f'DROP TRIGGER "{trigger}"')
    else:
        raise NotSupportedError("Tenant integrity guards require PostgreSQL or SQLite.")


class Migration(migrations.Migration):
    dependencies = [("tenants", "0001_initial")]

    operations = [migrations.RunPython(install_guards, remove_guards)]

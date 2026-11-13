from django.db import migrations
from django.db.utils import NotSupportedError


LINKS = [
    ("classification_id", "ncr_classification", "id"),
    ("region_id", "tenants_region", "id"),
    ("discipline_id", "tenants_discipline", "id"),
    ("originator_id", "tenants_membership", "user_id"),
    ("responsible_manager_id", "tenants_membership", "user_id"),
    ("closed_by_id", "tenants_membership", "user_id"),
    ("cancelled_by_id", "tenants_membership", "user_id"),
]
IMMUTABLE = (
    "company_id", "public_id", "sequence_number", "number", "direction",
    "classification_id", "originator_id", "due_date", "created_at",
)


def install_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        for column, table, key in LINKS:
            schema_editor.execute(f'''
                ALTER TABLE ncr_ncr ADD CONSTRAINT "ncr_company_{column}"
                FOREIGN KEY (company_id, "{column}") REFERENCES "{table}" (company_id, "{key}")
                ON DELETE RESTRICT
            ''')
        schema_editor.execute('''
            ALTER TABLE tenants_auditentry ADD CONSTRAINT tenants_audit_ncr_company
            FOREIGN KEY (company_id, ncr_id) REFERENCES ncr_ncr (company_id, id)
            ON DELETE RESTRICT
        ''')
        changed = " OR ".join(f'NEW."{field}" IS DISTINCT FROM OLD."{field}"' for field in IMMUTABLE)
        schema_editor.execute(f'''
            CREATE FUNCTION ncr_guard_report() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'NCRs must not be deleted' USING ERRCODE = '23514';
                END IF;
                IF {changed} THEN
                    RAISE EXCEPTION 'NCR identity and SLA basis cannot change' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END; $$;
            CREATE TRIGGER ncr_report_retained BEFORE UPDATE OR DELETE ON ncr_ncr
            FOR EACH ROW EXECUTE FUNCTION ncr_guard_report();

            CREATE FUNCTION ncr_guard_counter() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Number counters must not be deleted' USING ERRCODE = '23514';
                END IF;
                IF NEW.company_id IS DISTINCT FROM OLD.company_id
                    OR NEW.sequence_name IS DISTINCT FROM OLD.sequence_name
                    OR NEW.created_at IS DISTINCT FROM OLD.created_at
                    OR NEW.last_number < OLD.last_number THEN
                    RAISE EXCEPTION 'Number counter identity or issued numbers cannot change'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END; $$;
            CREATE TRIGGER ncr_counter_retained BEFORE UPDATE OR DELETE ON tenants_numbersequence
            FOR EACH ROW EXECUTE FUNCTION ncr_guard_counter();
        ''')
    elif vendor == "sqlite":
        scope_checks = " OR ".join(
            f'(NEW."{column}" IS NOT NULL AND NOT EXISTS ('
            f'SELECT 1 FROM "{table}" p WHERE p.company_id = NEW.company_id '
            f'AND p."{key}" = NEW."{column}"))'
            for column, table, key in LINKS
        )
        changed = " OR ".join(f'NEW."{field}" IS NOT OLD."{field}"' for field in IMMUTABLE)
        statements = [
            f'''CREATE TRIGGER ncr_report_scope_insert BEFORE INSERT ON ncr_ncr
                WHEN {scope_checks} BEGIN SELECT RAISE(ABORT, 'NCR company references must match'); END''',
            f'''CREATE TRIGGER ncr_report_scope_update BEFORE UPDATE ON ncr_ncr
                WHEN {scope_checks} BEGIN SELECT RAISE(ABORT, 'NCR company references must match'); END''',
            f'''CREATE TRIGGER ncr_report_identity BEFORE UPDATE ON ncr_ncr
                WHEN {changed} BEGIN SELECT RAISE(ABORT, 'NCR identity and SLA basis cannot change'); END''',
            '''CREATE TRIGGER ncr_report_retained BEFORE DELETE ON ncr_ncr
               BEGIN SELECT RAISE(ABORT, 'NCRs must not be deleted'); END''',
            '''CREATE TRIGGER ncr_counter_retained BEFORE DELETE ON tenants_numbersequence
               BEGIN SELECT RAISE(ABORT, 'Number counters must not be deleted'); END''',
            '''CREATE TRIGGER ncr_counter_identity BEFORE UPDATE ON tenants_numbersequence
               WHEN NEW.company_id != OLD.company_id OR NEW.sequence_name != OLD.sequence_name
                    OR NEW.created_at IS NOT OLD.created_at OR NEW.last_number < OLD.last_number
               BEGIN SELECT RAISE(ABORT, 'Number counter identity or issued numbers cannot change'); END''',
            '''CREATE TRIGGER tenants_audit_ncr_company BEFORE INSERT ON tenants_auditentry
               WHEN NEW.ncr_id IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM ncr_ncr n WHERE n.id = NEW.ncr_id AND n.company_id = NEW.company_id
               ) BEGIN SELECT RAISE(ABORT, 'NCR audit company must match'); END''',
        ]
        for statement in statements:
            schema_editor.execute(statement)
    else:
        raise NotSupportedError("NCR integrity guards require PostgreSQL or SQLite.")


def remove_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        for column, _, _ in LINKS:
            schema_editor.execute(f'ALTER TABLE ncr_ncr DROP CONSTRAINT "ncr_company_{column}"')
        schema_editor.execute('ALTER TABLE tenants_auditentry DROP CONSTRAINT tenants_audit_ncr_company')
        schema_editor.execute('DROP TRIGGER ncr_report_retained ON ncr_ncr')
        schema_editor.execute('DROP FUNCTION ncr_guard_report()')
        schema_editor.execute('DROP TRIGGER ncr_counter_retained ON tenants_numbersequence')
        schema_editor.execute('DROP FUNCTION ncr_guard_counter()')
    elif vendor == "sqlite":
        for trigger in (
            "ncr_report_scope_insert", "ncr_report_scope_update", "ncr_report_identity",
            "ncr_report_retained", "ncr_counter_retained", "ncr_counter_identity",
            "tenants_audit_ncr_company",
        ):
            schema_editor.execute(f'DROP TRIGGER "{trigger}"')
    else:
        raise NotSupportedError("NCR integrity guards require PostgreSQL or SQLite.")


class Migration(migrations.Migration):
    dependencies = [
        ("ncr", "0002_ncr"),
        ("tenants", "0006_numbersequence_auditentry_ncr_and_more"),
    ]

    operations = [migrations.RunPython(install_guards, remove_guards)]

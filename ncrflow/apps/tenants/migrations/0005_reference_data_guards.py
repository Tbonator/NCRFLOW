from django.db import migrations
from django.db.utils import NotSupportedError


TABLES = ("tenants_region", "tenants_discipline", "ncr_classification")


def install_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute("""
            CREATE FUNCTION tenants_guard_reference_identity() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Reference records must be deactivated, not deleted'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.company_id IS DISTINCT FROM OLD.company_id THEN
                    RAISE EXCEPTION 'Reference company cannot change' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$;
        """)
        for table in TABLES:
            schema_editor.execute(f'''
                CREATE TRIGGER "{table}_identity_guard"
                BEFORE UPDATE OR DELETE ON "{table}"
                FOR EACH ROW EXECUTE FUNCTION tenants_guard_reference_identity()
            ''')
    elif vendor == "sqlite":
        for table in TABLES:
            schema_editor.execute(f'''
                CREATE TRIGGER "{table}_retained" BEFORE DELETE ON "{table}"
                BEGIN SELECT RAISE(ABORT, 'Reference records must be deactivated'); END
            ''')
            schema_editor.execute(f'''
                CREATE TRIGGER "{table}_company_guard" BEFORE UPDATE ON "{table}"
                WHEN NEW.company_id != OLD.company_id
                BEGIN SELECT RAISE(ABORT, 'Reference company cannot change'); END
            ''')
    else:
        raise NotSupportedError("Reference-data guards require PostgreSQL or SQLite.")


def remove_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        for table in TABLES:
            schema_editor.execute(f'DROP TRIGGER "{table}_identity_guard" ON "{table}"')
        schema_editor.execute('DROP FUNCTION tenants_guard_reference_identity()')
    elif vendor == "sqlite":
        for table in TABLES:
            schema_editor.execute(f'DROP TRIGGER "{table}_retained"')
            schema_editor.execute(f'DROP TRIGGER "{table}_company_guard"')
    else:
        raise NotSupportedError("Reference-data guards require PostgreSQL or SQLite.")


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0004_discipline_region"),
        ("ncr", "0001_initial"),
    ]

    operations = [migrations.RunPython(install_guards, remove_guards)]

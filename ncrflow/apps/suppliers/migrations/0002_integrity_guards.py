from django.db import migrations
from django.db.utils import NotSupportedError


# (child table, constraint name, child column, parent table): data-model.md section 4.2.
LINKS = [
    ("suppliers_supplier", "suppliers_supplier_company_region", "region_id", "tenants_region"),
    (
        "suppliers_suppliercontact",
        "suppliers_contact_company_supplier",
        "supplier_id",
        "suppliers_supplier",
    ),
]

POSTGRESQL_GUARDS = """
CREATE FUNCTION suppliers_guard_supplier() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Suppliers must be deactivated, not deleted' USING ERRCODE = '23514';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id THEN
        RAISE EXCEPTION 'Supplier company cannot change' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER suppliers_supplier_identity_guard
BEFORE UPDATE OR DELETE ON suppliers_supplier
FOR EACH ROW EXECUTE FUNCTION suppliers_guard_supplier();

CREATE FUNCTION suppliers_guard_contact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Supplier contacts must be deactivated, not deleted'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id
        OR NEW.supplier_id IS DISTINCT FROM OLD.supplier_id
        OR NEW.public_id IS DISTINCT FROM OLD.public_id THEN
        RAISE EXCEPTION 'Supplier contact company, supplier and public id cannot change'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER suppliers_contact_identity_guard
BEFORE UPDATE OR DELETE ON suppliers_suppliercontact
FOR EACH ROW EXECUTE FUNCTION suppliers_guard_contact();
"""

SQLITE_GUARDS = [
    """CREATE TRIGGER suppliers_supplier_retained BEFORE DELETE ON suppliers_supplier
       BEGIN SELECT RAISE(ABORT, 'Suppliers must be deactivated, not deleted'); END""",
    """CREATE TRIGGER suppliers_supplier_identity_guard BEFORE UPDATE ON suppliers_supplier
       WHEN NEW.company_id IS NOT OLD.company_id
       BEGIN SELECT RAISE(ABORT, 'Supplier company cannot change'); END""",
    """CREATE TRIGGER suppliers_contact_retained BEFORE DELETE ON suppliers_suppliercontact
       BEGIN SELECT RAISE(ABORT, 'Supplier contacts must be deactivated, not deleted'); END""",
    """CREATE TRIGGER suppliers_contact_identity_guard BEFORE UPDATE ON suppliers_suppliercontact
       WHEN NEW.company_id IS NOT OLD.company_id OR NEW.supplier_id IS NOT OLD.supplier_id
            OR NEW.public_id IS NOT OLD.public_id
       BEGIN SELECT RAISE(ABORT,
           'Supplier contact company, supplier and public id cannot change'); END""",
]
SQLITE_GUARD_NAMES = [
    "suppliers_supplier_retained",
    "suppliers_supplier_identity_guard",
    "suppliers_contact_retained",
    "suppliers_contact_identity_guard",
]


def install_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        for child, name, column, parent in LINKS:
            schema_editor.execute(f'''
                ALTER TABLE "{child}" ADD CONSTRAINT "{name}"
                FOREIGN KEY (company_id, "{column}") REFERENCES "{parent}" (company_id, id)
                ON DELETE RESTRICT
            ''')
        schema_editor.execute(POSTGRESQL_GUARDS)
    elif vendor == "sqlite":
        # SQLite cannot add a foreign key to an existing table, so equivalent triggers
        # check that the parent row is in the same company on every insert and update.
        for child, name, column, parent in LINKS:
            mismatch = (
                f'NEW."{column}" IS NOT NULL AND NOT EXISTS ('
                f'SELECT 1 FROM "{parent}" p WHERE p.company_id = NEW.company_id '
                f'AND p.id = NEW."{column}")'
            )
            for event in ("INSERT", "UPDATE"):
                schema_editor.execute(f'''
                    CREATE TRIGGER "{name}_{event.lower()}" BEFORE {event} ON "{child}"
                    WHEN {mismatch}
                    BEGIN SELECT RAISE(ABORT, 'Supplier company references must match'); END
                ''')
        for statement in SQLITE_GUARDS:
            schema_editor.execute(statement)
    else:
        raise NotSupportedError("Supplier integrity guards require PostgreSQL or SQLite.")


def remove_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER suppliers_contact_identity_guard ON suppliers_suppliercontact"
        )
        schema_editor.execute("DROP FUNCTION suppliers_guard_contact()")
        schema_editor.execute("DROP TRIGGER suppliers_supplier_identity_guard ON suppliers_supplier")
        schema_editor.execute("DROP FUNCTION suppliers_guard_supplier()")
        for child, name, _, _ in reversed(LINKS):
            schema_editor.execute(f'ALTER TABLE "{child}" DROP CONSTRAINT "{name}"')
    elif vendor == "sqlite":
        for trigger in SQLITE_GUARD_NAMES:
            schema_editor.execute(f'DROP TRIGGER "{trigger}"')
        for _, name, _, _ in LINKS:
            for event in ("insert", "update"):
                schema_editor.execute(f'DROP TRIGGER "{name}_{event}"')
    else:
        raise NotSupportedError("Supplier integrity guards require PostgreSQL or SQLite.")


class Migration(migrations.Migration):
    dependencies = [
        ("suppliers", "0001_initial"),
        ("tenants", "0005_reference_data_guards"),
    ]

    operations = [migrations.RunPython(install_guards, remove_guards)]

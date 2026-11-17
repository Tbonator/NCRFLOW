"""Phase 3: same-company key for ncr.supplier_id and supplier immutability.

data-model.md section 4.2 (ncr.supplier_id is company-checked against supplier)
and Appendix B.7 / Q38 (an NCR's supplier is immutable after creation).
"""

from importlib import import_module

from django.db import migrations
from django.db.utils import NotSupportedError

SUPPLIER_LINK = ("supplier_id", "suppliers_supplier", "id")


def _earlier():
    creation = import_module("apps.ncr.migrations.0003_creation_integrity")
    supplier = import_module("apps.ncr.migrations.0004_ncr_supplier")
    return creation, supplier


def _replace_postgresql_guard(schema_editor, immutable):
    # Same function 0003 created; only the list of immutable columns differs.
    changed = " OR ".join(f'NEW."{field}" IS DISTINCT FROM OLD."{field}"' for field in immutable)
    schema_editor.execute(f"""
        CREATE OR REPLACE FUNCTION ncr_guard_report() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'NCRs must not be deleted' USING ERRCODE = '23514';
            END IF;
            IF {changed} THEN
                RAISE EXCEPTION 'NCR identity and SLA basis cannot change'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END; $$;
    """)


def _replace_sqlite_guards(schema_editor, links, immutable):
    _, supplier = _earlier()
    for name in supplier.SQLITE_REPORT_GUARD_NAMES:
        schema_editor.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    for statement in supplier.sqlite_report_guards(links, immutable):
        schema_editor.execute(statement)


def install_guards(apps, schema_editor):
    creation, _ = _earlier()
    links = [*creation.LINKS, SUPPLIER_LINK]
    immutable = (*creation.IMMUTABLE, "supplier_id")
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute("""
            ALTER TABLE ncr_ncr ADD CONSTRAINT "ncr_company_supplier_id"
            FOREIGN KEY (company_id, supplier_id) REFERENCES suppliers_supplier (company_id, id)
            ON DELETE RESTRICT
        """)
        _replace_postgresql_guard(schema_editor, immutable)
    elif vendor == "sqlite":
        _replace_sqlite_guards(schema_editor, links, immutable)
    else:
        raise NotSupportedError("NCR integrity guards require PostgreSQL or SQLite.")


def remove_guards(apps, schema_editor):
    creation, _ = _earlier()
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        _replace_postgresql_guard(schema_editor, creation.IMMUTABLE)
        schema_editor.execute('ALTER TABLE ncr_ncr DROP CONSTRAINT "ncr_company_supplier_id"')
    elif vendor == "sqlite":
        _replace_sqlite_guards(schema_editor, creation.LINKS, creation.IMMUTABLE)
    else:
        raise NotSupportedError("NCR integrity guards require PostgreSQL or SQLite.")


class Migration(migrations.Migration):
    dependencies = [
        ("ncr", "0004_ncr_supplier"),
        ("suppliers", "0002_integrity_guards"),
    ]

    operations = [migrations.RunPython(install_guards, remove_guards)]

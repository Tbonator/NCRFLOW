"""Phase 3: ncr.supplier_id, the direction/supplier check and the supplier keys.

data-model.md section 2.9 (column, check and extra unique key), 4.1 and 5.
The same-company composite foreign key and the immutability guard follow in 0005.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

SQLITE_REPORT_GUARD_NAMES = (
    "ncr_report_scope_insert",
    "ncr_report_scope_update",
    "ncr_report_identity",
    "ncr_report_retained",
)


def sqlite_report_guards(links, immutable):
    """The SQLite triggers on ncr_ncr, in the same shape 0003 created them.

    links: (column, parent table, parent key) same-company references to check.
    immutable: columns that may never change after the row is inserted.
    """
    scope_checks = " OR ".join(
        f'(NEW."{column}" IS NOT NULL AND NOT EXISTS ('
        f'SELECT 1 FROM "{table}" p WHERE p.company_id = NEW.company_id '
        f'AND p."{key}" = NEW."{column}"))'
        for column, table, key in links
    )
    changed = " OR ".join(f'NEW."{field}" IS NOT OLD."{field}"' for field in immutable)
    return [
        f"""CREATE TRIGGER ncr_report_scope_insert BEFORE INSERT ON ncr_ncr
            WHEN {scope_checks}
            BEGIN SELECT RAISE(ABORT, 'NCR company references must match'); END""",
        f"""CREATE TRIGGER ncr_report_scope_update BEFORE UPDATE ON ncr_ncr
            WHEN {scope_checks}
            BEGIN SELECT RAISE(ABORT, 'NCR company references must match'); END""",
        f"""CREATE TRIGGER ncr_report_identity BEFORE UPDATE ON ncr_ncr
            WHEN {changed}
            BEGIN SELECT RAISE(ABORT, 'NCR identity and SLA basis cannot change'); END""",
        """CREATE TRIGGER ncr_report_retained BEFORE DELETE ON ncr_ncr
           BEGIN SELECT RAISE(ABORT, 'NCRs must not be deleted'); END""",
    ]


def restore_sqlite_report_guards(apps, schema_editor):
    # Changing constraints rebuilds ncr_ncr on SQLite, which silently drops the
    # triggers 0003 put on it. Put the same triggers back after the rebuild, in
    # both directions. PostgreSQL alters the table in place and keeps its triggers.
    if schema_editor.connection.vendor != "sqlite":
        return
    creation = import_module("apps.ncr.migrations.0003_creation_integrity")
    for name in SQLITE_REPORT_GUARD_NAMES:
        schema_editor.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    for statement in sqlite_report_guards(creation.LINKS, creation.IMMUTABLE):
        schema_editor.execute(statement)
    drop_sqlite_audit_scope_guard(apps, schema_editor)
    schema_editor.execute("""
        CREATE TRIGGER tenants_audit_ncr_company BEFORE INSERT ON tenants_auditentry
        WHEN NEW.ncr_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM ncr_ncr n WHERE n.id = NEW.ncr_id AND n.company_id = NEW.company_id
        ) BEGIN SELECT RAISE(ABORT, 'NCR audit company must match'); END
    """)


def drop_sqlite_audit_scope_guard(apps, schema_editor):
    # SQLite validates triggers on other tables during RENAME TABLE. This
    # cross-table reference must be absent while ncr_ncr is being rebuilt.
    if schema_editor.connection.vendor == "sqlite":
        schema_editor.execute('DROP TRIGGER IF EXISTS "tenants_audit_ncr_company"')


class Migration(migrations.Migration):
    dependencies = [
        ("ncr", "0003_creation_integrity"),
        ("suppliers", "0002_integrity_guards"),
        ("tenants", "0006_numbersequence_auditentry_ncr_and_more"),
    ]

    operations = [
        # Drop before forward rebuilds; restore last when reversing this migration.
        migrations.RunPython(drop_sqlite_audit_scope_guard, restore_sqlite_report_guards),
        migrations.RemoveConstraint(
            model_name="ncr",
            name="ncr_internal_only_phase_two",
        ),
        migrations.AddField(
            model_name="ncr",
            name="supplier",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="suppliers.supplier",
            ),
        ),
        migrations.AddIndex(
            model_name="ncr",
            index=models.Index(
                fields=["company", "supplier", "status"], name="ncr_supplier_status"
            ),
        ),
        migrations.AddConstraint(
            model_name="ncr",
            constraint=models.UniqueConstraint(
                fields=("company", "id", "supplier"), name="ncr_report_supplier_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="ncr",
            constraint=models.CheckConstraint(
                condition=models.Q(("direction__in", ["internal", "external"])),
                name="ncr_direction_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="ncr",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("direction", "internal"), ("supplier__isnull", True)),
                    models.Q(("direction", "external"), ("supplier__isnull", False)),
                    _connector="OR",
                ),
                name="ncr_direction_supplier_match",
            ),
        ),
        # Restore after forward rebuilds; drop first before reverse rebuilds.
        migrations.RunPython(restore_sqlite_report_guards, drop_sqlite_audit_scope_guard),
    ]

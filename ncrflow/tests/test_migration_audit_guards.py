"""Exercise real schema rollback without flushing the main retained-record fixtures."""

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def test_sqlite_audit_guards_survive_ncr_migration_rollback_and_reapply():
    script = dedent("""
        import django
        django.setup()
        print("Django ready", flush=True)
        from django.contrib.auth import get_user_model
        from django.db import connection, IntegrityError, transaction
        from django.db.migrations.executor import MigrationExecutor
        from apps.tenants.models import Company, Membership, AuditEntry, Region, Discipline
        from apps.ncr.models import NCR, Classification

        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        executor.migrate(latest)
        print("Forward migration complete", flush=True)
        company = Company.objects.create(name="Audit Test A")
        other = Company.objects.create(name="Audit Test B")
        user = get_user_model().objects.create_user("audit-test@example.com")
        Membership.objects.for_company(other).create(company=other, user=user, role="quality")
        entry = AuditEntry.objects.for_company(company).create(
            company=company, object_type="company", object_id=company.pk, action="created"
        )
        region = Region.objects.for_company(other).create(company=other, name="Other region")
        discipline = Discipline.objects.for_company(other).create(
            company=other, name="Other discipline"
        )
        classification = Classification.objects.for_company(other).create(
            company=other, name="Other classification", sla_days=3
        )
        report = NCR.objects.for_company(other).create(
            company=other, number="NCR-00001", sequence_number=1, subject="Scope test",
            description="Migration integrity fixture", direction="internal",
            classification=classification, region=region, discipline=discipline,
            originator=user, responsible_manager=user, due_date="2026-11-16"
        )

        def must_reject(sql, params):
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(sql, params)
            except IntegrityError:
                return
            raise AssertionError("Audit protection did not reject: " + sql)

        def check_guards():
            must_reject(
                "UPDATE tenants_auditentry SET comment = 'rewrite' WHERE id = %s", [entry.pk]
            )
            must_reject("DELETE FROM tenants_auditentry WHERE id = %s", [entry.pk])
            must_reject(
                "INSERT INTO tenants_auditentry "
                "(company_id, actor_user_id, created_at, object_type, object_id, object_label, "
                "action, field_name, old_value, new_value, comment) "
                "VALUES (%s, %s, '2026-11-13 00:00:00', 'company', %s, '', "
                "'created', '', NULL, NULL, '')",
                [company.pk, user.pk, company.pk],
            )

        def check_ncr_guards():
            must_reject(
                "INSERT INTO tenants_auditentry "
                "(company_id, ncr_id, created_at, object_type, object_id, object_label, "
                "action, field_name, old_value, new_value, comment) "
                "VALUES (%s, %s, '2026-11-13 00:00:00', 'ncr', %s, '', "
                "'created', '', NULL, NULL, '')",
                [company.pk, report.pk, report.pk],
            )
            must_reject("UPDATE ncr_ncr SET number = 'NCR-99999' WHERE id = %s", [report.pk])
            must_reject("DELETE FROM ncr_ncr WHERE id = %s", [report.pk])

        check_guards()
        check_ncr_guards()
        MigrationExecutor(connection).migrate([("ncr", "0003_creation_integrity")])
        print("Supplier-column rollback complete", flush=True)
        check_guards()
        check_ncr_guards()
        MigrationExecutor(connection).migrate(latest)
        print("Supplier-column reapply complete", flush=True)
        check_guards()
        check_ncr_guards()
        MigrationExecutor(connection).migrate([
            ("ncr", "0001_initial"), ("tenants", "0005_reference_data_guards")
        ])
        print("Rollback complete", flush=True)
        check_guards()
        MigrationExecutor(connection).migrate(latest)
        print("Reapply complete", flush=True)
        check_guards()
        print("SQLite audit guards survive forward, rollback and reapply.")
    """)
    try:
        result = subprocess.run(
            [sys.executable, "-u", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "DJANGO_SETTINGS_MODULE": "tests.settings"},
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            f"Migration subprocess timed out. Progress: {error.stdout!r}; errors: {error.stderr!r}"
        ) from error
    assert result.returncode == 0, result.stdout + result.stderr

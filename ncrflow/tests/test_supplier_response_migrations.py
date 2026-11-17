"""Exercise guard removal/reinstallation on an isolated database, not retained fixtures."""

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def test_response_guards_survive_rollback_reapply_and_table_recreation():
    script = dedent("""
        import django
        django.setup()
        from django.db import connection, IntegrityError, transaction
        from django.db.migrations.executor import MigrationExecutor
        from apps.ncr.models import SupplierResponse
        from tests.test_supplier_response_schema import build_graph, insert, values

        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        MigrationExecutor(connection).migrate(latest)
        graph = build_graph()
        response = insert(graph, "submitted")

        def reject(operation):
            try:
                with transaction.atomic():
                    operation()
            except IntegrityError:
                return
            raise AssertionError("A response database guard did not reject a violation")

        def check():
            rows = SupplierResponse.objects.for_company(graph["company"]).filter(pk=response.pk)
            reject(lambda: rows.update(root_cause="Locked text bypass"))
            reject(lambda: rows.update(status="draft", comments="Combined unlock bypass"))
            reject(lambda: rows.update(sent_to_supplier_contact_id=graph["colleague"].pk))
            reject(lambda: SupplierResponse.objects.for_company(graph["company"]).create(
                **values(graph, ncr=graph["next_report"],
                         sent_to_supplier_contact=graph["wrong_contact"])
            ))
            def delete():
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM ncr_supplierresponse WHERE id = %s", [response.pk])
            reject(delete)

        check()
        MigrationExecutor(connection).migrate([("ncr", "0006_supplier_response")])
        print("Guard rollback complete", flush=True)
        MigrationExecutor(connection).migrate(latest)
        check()
        print("Guard reapply checked", flush=True)
        MigrationExecutor(connection).migrate([("ncr", "0005_supplier_integrity")])
        MigrationExecutor(connection).migrate(latest)
        response = insert(graph, "submitted")
        check()
        print("Table recreation checked", flush=True)
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
            f"Response migration subprocess timed out: {error.stdout!r}; {error.stderr!r}"
        ) from error
    assert result.returncode == 0, result.stdout + result.stderr

from django.db import migrations
from django.db.utils import NotSupportedError


IDENTITY = ("company_id", "ncr_id", "supplier_id", "sent_to_supplier_contact_id", "created_at")
CONTENT = ("root_cause", "corrective_action", "preventive_action", "comments")
SQLITE_GUARDS = (
    "ncr_response_scope_insert", "ncr_response_scope_update", "ncr_response_identity",
    "ncr_response_content_lock", "ncr_response_retained",
)
POSTGRESQL_KEYS = {
    "ncr_response_report_supplier": (
        "company_id, ncr_id, supplier_id", "ncr_ncr", "company_id, id, supplier_id"
    ),
    "ncr_response_sent_contact": (
        "company_id, supplier_id, sent_to_supplier_contact_id",
        "suppliers_suppliercontact", "company_id, supplier_id, id",
    ),
    "ncr_response_submitted_contact": (
        "company_id, supplier_id, submitted_by_supplier_contact_id",
        "suppliers_suppliercontact", "company_id, supplier_id, id",
    ),
    "ncr_response_reviewer_company": (
        "company_id, reviewed_by_id", "tenants_membership", "company_id, user_id"
    ),
}


def install_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        for name, (columns, parent, target) in POSTGRESQL_KEYS.items():
            schema_editor.execute(f"""
                ALTER TABLE ncr_supplierresponse ADD CONSTRAINT {name}
                FOREIGN KEY ({columns}) REFERENCES {parent} ({target}) ON DELETE RESTRICT
            """)
        identity_changed = " OR ".join(f'NEW."{field}" IS DISTINCT FROM OLD."{field}"' for field in IDENTITY)
        content_changed = " OR ".join(f'NEW."{field}" IS DISTINCT FROM OLD."{field}"' for field in CONTENT)
        schema_editor.execute(f"""
            CREATE FUNCTION ncr_guard_supplier_response() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Supplier responses must not be deleted' USING ERRCODE = '23514';
                END IF;
                IF {identity_changed} THEN
                    RAISE EXCEPTION 'Supplier response identity cannot change' USING ERRCODE = '23514';
                END IF;
                IF OLD.first_submitted_at IS NOT NULL
                    AND NEW.first_submitted_at IS DISTINCT FROM OLD.first_submitted_at THEN
                    RAISE EXCEPTION 'First submission cannot change' USING ERRCODE = '23514';
                END IF;
                IF OLD.status IN ('submitted', 'under_review', 'accepted')
                    AND ({content_changed}) THEN
                    RAISE EXCEPTION 'Submitted supplier text is locked' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END; $$;
            CREATE TRIGGER ncr_response_guard BEFORE UPDATE OR DELETE ON ncr_supplierresponse
            FOR EACH ROW EXECUTE FUNCTION ncr_guard_supplier_response();
        """)
    elif vendor == "sqlite":
        scope = """
            NOT EXISTS (SELECT 1 FROM ncr_ncr n WHERE n.id = NEW.ncr_id
                AND n.company_id = NEW.company_id AND n.supplier_id = NEW.supplier_id)
            OR NOT EXISTS (SELECT 1 FROM suppliers_suppliercontact c
                WHERE c.id = NEW.sent_to_supplier_contact_id AND c.company_id = NEW.company_id
                AND c.supplier_id = NEW.supplier_id)
            OR (NEW.submitted_by_supplier_contact_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM suppliers_suppliercontact c
                WHERE c.id = NEW.submitted_by_supplier_contact_id
                AND c.company_id = NEW.company_id AND c.supplier_id = NEW.supplier_id))
            OR (NEW.reviewed_by_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM tenants_membership m WHERE m.user_id = NEW.reviewed_by_id
                AND m.company_id = NEW.company_id))
        """
        for operation in ("INSERT", "UPDATE"):
            schema_editor.execute(f"""
                CREATE TRIGGER ncr_response_scope_{operation.lower()}
                BEFORE {operation} ON ncr_supplierresponse WHEN {scope}
                BEGIN SELECT RAISE(ABORT, 'Supplier response scope must match'); END
            """)
        changed = " OR ".join(f'NEW."{field}" IS NOT OLD."{field}"' for field in IDENTITY)
        content_changed = " OR ".join(f'NEW."{field}" IS NOT OLD."{field}"' for field in CONTENT)
        schema_editor.execute(f"""
            CREATE TRIGGER ncr_response_identity BEFORE UPDATE ON ncr_supplierresponse
            WHEN {changed} OR (OLD.first_submitted_at IS NOT NULL
                AND NEW.first_submitted_at IS NOT OLD.first_submitted_at)
            BEGIN SELECT RAISE(ABORT, 'Response identity and first submission cannot change'); END
        """)
        schema_editor.execute(f"""
            CREATE TRIGGER ncr_response_content_lock BEFORE UPDATE ON ncr_supplierresponse
            WHEN OLD.status IN ('submitted', 'under_review', 'accepted') AND ({content_changed})
            BEGIN SELECT RAISE(ABORT, 'Submitted supplier text is locked'); END
        """)
        schema_editor.execute("""
            CREATE TRIGGER ncr_response_retained BEFORE DELETE ON ncr_supplierresponse
            BEGIN SELECT RAISE(ABORT, 'Supplier responses must not be deleted'); END
        """)
    else:
        raise NotSupportedError("Supplier response guards require PostgreSQL or SQLite.")


def remove_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute('DROP TRIGGER ncr_response_guard ON ncr_supplierresponse')
        schema_editor.execute('DROP FUNCTION ncr_guard_supplier_response()')
        for name in POSTGRESQL_KEYS:
            schema_editor.execute(f'ALTER TABLE ncr_supplierresponse DROP CONSTRAINT {name}')
    elif vendor == "sqlite":
        for name in SQLITE_GUARDS:
            schema_editor.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    else:
        raise NotSupportedError("Supplier response guards require PostgreSQL or SQLite.")


class Migration(migrations.Migration):
    dependencies = [("ncr", "0006_supplier_response")]
    operations = [migrations.RunPython(install_guards, remove_guards)]

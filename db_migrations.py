"""
db_migrations.py
Database migration and schema update functions for CFC Order Backend.
These are helper functions called by the migration endpoints in main.py.
"""

from db_helpers import get_db


def create_pending_checkouts_table() -> dict:
    """Create pending_checkouts table for B2BWave checkout flow"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_checkouts (
                    order_id VARCHAR(50) PRIMARY KEY,
                    customer_email VARCHAR(255),
                    checkout_token VARCHAR(100),
                    payment_link TEXT,
                    payment_amount DECIMAL(10, 2),
                    payment_initiated_at TIMESTAMP WITH TIME ZONE,
                    payment_completed_at TIMESTAMP WITH TIME ZONE,
                    transaction_id VARCHAR(100),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
    return {"status": "ok", "message": "pending_checkouts table created"}


def create_shipments_table() -> dict:
    """Create order_shipments table without resetting other tables"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS order_shipments (
                    id SERIAL PRIMARY KEY,
                    order_id VARCHAR(50) REFERENCES orders(order_id) ON DELETE CASCADE,
                    shipment_id VARCHAR(50) NOT NULL UNIQUE,
                    warehouse VARCHAR(100) NOT NULL,
                    status VARCHAR(50) DEFAULT 'needs_order',
                    tracking VARCHAR(100),
                    pro_number VARCHAR(50),
                    bol_sent BOOLEAN DEFAULT FALSE,
                    bol_sent_at TIMESTAMP WITH TIME ZONE,
                    weight DECIMAL(10,2),
                    ship_method VARCHAR(50),
                    sent_to_warehouse_at TIMESTAMP WITH TIME ZONE,
                    warehouse_confirmed_at TIMESTAMP WITH TIME ZONE,
                    shipped_at TIMESTAMP WITH TIME ZONE,
                    delivered_at TIMESTAMP WITH TIME ZONE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shipments_order ON order_shipments(order_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shipments_id ON order_shipments(shipment_id)")
    return {"status": "ok", "message": "order_shipments table created"}


def add_rl_shipping_fields() -> dict:
    """Add RL Carriers shipping fields to order_shipments table"""
    with get_db() as conn:
        with conn.cursor() as cur:
            fields_to_add = [
                ("origin_zip", "VARCHAR(10)"),
                ("rl_quote_number", "VARCHAR(50)"),
                ("rl_quote_price", "DECIMAL(10,2)"),
                ("rl_customer_price", "DECIMAL(10,2)"),
                ("rl_invoice_amount", "DECIMAL(10,2)"),
                ("has_oversized", "BOOLEAN DEFAULT FALSE"),
                ("li_quote_price", "DECIMAL(10,2)"),
                ("li_customer_price", "DECIMAL(10,2)"),
                ("actual_cost", "DECIMAL(10,2)"),
                ("quote_url", "TEXT"),
                ("ps_quote_url", "TEXT"),
                ("ps_quote_price", "DECIMAL(10,2)"),
                ("quote_price", "DECIMAL(10,2)"),
                ("customer_price", "DECIMAL(10,2)"),
                ("tracking_number", "VARCHAR(100)")
            ]

            for field_name, field_type in fields_to_add:
                try:
                    cur.execute(f"ALTER TABLE order_shipments ADD COLUMN {field_name} {field_type}")
                except Exception:
                    conn.rollback()
                    pass

            conn.commit()
    return {"status": "ok", "message": "Shipping fields added to order_shipments"}


def add_ps_fields() -> dict:
    """Add Pirateship fields to order_shipments table"""
    with get_db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("ALTER TABLE order_shipments ADD COLUMN ps_quote_url TEXT")
                conn.commit()
            except:
                conn.rollback()
            try:
                cur.execute("ALTER TABLE order_shipments ADD COLUMN ps_quote_price DECIMAL(10,2)")
                conn.commit()
            except:
                conn.rollback()
    return {"status": "ok", "message": "PS fields added"}


def fix_shipment_columns() -> dict:
    """Fix column lengths in order_shipments table"""
    with get_db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("ALTER TABLE order_shipments ALTER COLUMN order_id TYPE VARCHAR(50)")
                conn.commit()
            except Exception:
                conn.rollback()
            try:
                cur.execute("ALTER TABLE order_shipments ALTER COLUMN shipment_id TYPE VARCHAR(100)")
                conn.commit()
            except Exception:
                conn.rollback()
    return {"status": "ok", "message": "Shipment columns fixed"}


def fix_sku_columns() -> dict:
    """Fix SKU column lengths in all tables"""
    with get_db() as conn:
        with conn.cursor() as cur:
            for table_col in [
                ("sku_warehouse_map", "sku_prefix"),
                ("warehouse_mapping", "sku_prefix"),
                ("order_items", "sku_prefix"),
                ("order_line_items", "sku_prefix"),
            ]:
                try:
                    cur.execute(f"ALTER TABLE {table_col[0]} ALTER COLUMN {table_col[1]} TYPE VARCHAR(100)")
                    conn.commit()
                except:
                    conn.rollback()
    return {"status": "ok", "message": "SKU columns fixed"}


def fix_order_id_length() -> dict:
    """Increase order_id column length from VARCHAR(20) to VARCHAR(50)"""
    with get_db() as conn:
        with conn.cursor() as cur:
            results = []

            try:
                cur.execute("""
                    SELECT viewname FROM pg_views
                    WHERE schemaname = 'public'
                """)
                views = cur.fetchall()
                for view in views:
                    try:
                        cur.execute(f"DROP VIEW IF EXISTS {view[0]} CASCADE")
                        results.append(f"Dropped view: {view[0]}")
                    except:
                        pass
            except Exception as e:
                results.append(f"View lookup: {str(e)}")

            try:
                cur.execute("""
                    SELECT rulename, tablename FROM pg_rules
                    WHERE schemaname = 'public'
                """)
                rules = cur.fetchall()
                for rule in rules:
                    try:
                        cur.execute(f"DROP RULE IF EXISTS {rule[0]} ON {rule[1]} CASCADE")
                        results.append(f"Dropped rule: {rule[0]}")
                    except:
                        pass
            except Exception as e:
                results.append(f"Rule lookup: {str(e)}")

            conn.commit()

            tables = ['orders', 'order_status', 'order_line_items', 'order_events', 'order_shipments']
            for table in tables:
                try:
                    cur.execute(f"ALTER TABLE {table} ALTER COLUMN order_id TYPE VARCHAR(50)")
                    results.append(f"{table}: updated")
                except Exception as e:
                    results.append(f"{table}: {str(e)}")

            conn.commit()
    return {"status": "ok", "results": results}


def recreate_order_status_view() -> dict:
    """Recreate the order_status view after it was dropped"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP VIEW IF EXISTS order_status CASCADE")
            cur.execute("""
                CREATE VIEW order_status AS
                SELECT
                    order_id,
                    CASE
                        WHEN is_complete THEN 'complete'
                        WHEN bol_sent AND NOT is_complete THEN 'awaiting_shipment'
                        WHEN warehouse_confirmed AND NOT bol_sent THEN 'needs_bol'
                        WHEN sent_to_warehouse AND NOT warehouse_confirmed THEN 'awaiting_warehouse'
                        WHEN payment_received AND NOT sent_to_warehouse THEN 'needs_warehouse_order'
                        WHEN payment_link_sent AND NOT payment_received THEN 'awaiting_payment'
                        ELSE 'needs_payment_link'
                    END as current_status,
                    EXTRACT(DAY FROM NOW() - order_date)::INTEGER as days_open,
                    payment_link_sent,
                    payment_received,
                    sent_to_warehouse,
                    warehouse_confirmed,
                    bol_sent,
                    is_complete,
                    updated_at
                FROM orders
            """)
            conn.commit()
    return {"status": "ok", "message": "order_status view recreated"}


def add_weight_column() -> dict:
    """Add total_weight column to orders table"""
    with get_db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("ALTER TABLE orders ADD COLUMN total_weight DECIMAL(10,2)")
                conn.commit()
                return {"status": "ok", "message": "total_weight column added"}
            except Exception as e:
                if "already exists" in str(e):
                    return {"status": "ok", "message": "total_weight column already exists"}
                return {"status": "error", "message": str(e)}


def add_is_residential_to_shipments() -> dict:
    """
    Add is_residential column to order_shipments table.

    Populated at checkout time via Smarty address validation.
    Used by the admin UI to show/hide the liftgate toggle for commercial addresses.
    Safe to run multiple times.
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("ALTER TABLE order_shipments ADD COLUMN is_residential BOOLEAN DEFAULT TRUE")
                conn.commit()
                return {"status": "ok", "message": "is_residential column added to order_shipments"}
            except Exception as e:
                if "already exists" in str(e):
                    conn.rollback()
                    return {"status": "ok", "message": "is_residential column already exists"}
                conn.rollback()
                return {"status": "error", "message": str(e)}


# =============================================================================
# PHASE 3B: LIFECYCLE FIELDS MIGRATION
# =============================================================================

def add_lifecycle_fields() -> dict:
    """
    Add lifecycle management columns to orders table.

    New columns:
      - last_customer_email_at
      - lifecycle_status
      - lifecycle_deadline_at
      - lifecycle_reminders_sent

    Safe to run multiple times.
    """
    results = []

    fields_to_add = [
        ("last_customer_email_at", "TIMESTAMP WITH TIME ZONE"),
        ("lifecycle_status", "VARCHAR(20) DEFAULT 'active'"),
        ("lifecycle_deadline_at", "TIMESTAMP WITH TIME ZONE"),
        ("lifecycle_reminders_sent", "JSONB DEFAULT '{}'::jsonb"),
    ]

    with get_db() as conn:
        with conn.cursor() as cur:
            for field_name, field_type in fields_to_add:
                try:
                    cur.execute(f"ALTER TABLE orders ADD COLUMN {field_name} {field_type}")
                    results.append(f"{field_name}: added")
                except Exception as e:
                    if "already exists" in str(e):
                        results.append(f"{field_name}: already exists")
                    else:
                        results.append(f"{field_name}: ERROR — {str(e)}")
                    conn.rollback()
                    continue

            try:
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_orders_lifecycle_status
                    ON orders(lifecycle_status)
                """)
                results.append("idx_orders_lifecycle_status: created")
            except Exception as e:
                results.append(f"lifecycle index: {str(e)}")
                conn.rollback()

            try:
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_orders_last_customer_email
                    ON orders(last_customer_email_at)
                """)
                results.append("idx_orders_last_customer_email: created")
            except Exception as e:
                results.append(f"email index: {str(e)}")
                conn.rollback()

            conn.commit()

    return {
        "status": "ok",
        "message": "Lifecycle fields migration complete",
        "results": results
    }


def backfill_lifecycle_from_emails() -> dict:
    """
    Backfill last_customer_email_at from existing order_email_snippets.
    Run AFTER add_lifecycle_fields().
    """
    updated = 0
    errors = []

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orders o
                SET last_customer_email_at = sub.latest_email
                FROM (
                    SELECT order_id, MAX(email_date) as latest_email
                    FROM order_email_snippets
                    GROUP BY order_id
                ) sub
                WHERE o.order_id = sub.order_id
                AND o.last_customer_email_at IS NULL
            """)
            updated += cur.rowcount

            cur.execute("""
                UPDATE orders
                SET last_customer_email_at = COALESCE(updated_at, order_date, created_at)
                WHERE last_customer_email_at IS NULL
                AND (is_complete = FALSE OR is_complete IS NULL)
            """)
            updated += cur.rowcount

            conn.commit()

    return {
        "status": "ok",
        "message": f"Backfilled {updated} orders with last_customer_email_at",
        "updated": updated,
        "errors": errors
    }

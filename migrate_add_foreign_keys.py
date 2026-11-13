"""One-time migration: adds foreign keys to an existing ims.db.

New databases created by main.py already have foreign keys. Databases created
by older versions of this project do not, and SQLite cannot add constraints to
existing tables — so this script rebuilds the three affected tables (keeping
all data) using SQLite's official table-rebuild procedure.

Run ONCE, with the API server STOPPED:

    .\\venv\\Scripts\\python.exe migrate_add_foreign_keys.py

A backup copy is created automatically. To migrate a different file:
    python migrate_add_foreign_keys.py some_other.db
"""
import shutil
import sqlite3
import sys
import time

def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "ims.db"

    backup_path = f"{db_path}.backup-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(db_path, backup_path)
    print(f"Backup created: {backup_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")

    # --- Clean up rows that would violate the new constraints -----------------
    # Inventory rows / order items / movements pointing at products that no
    # longer exist are meaningless, so they are removed. Movements pointing at
    # a missing user or order keep their history; only the link is cleared.
    orphans = conn.execute(
        "SELECT COUNT(*) FROM inventory WHERE product_id NOT IN (SELECT id FROM products)"
    ).fetchone()[0]
    if orphans:
        conn.execute("DELETE FROM inventory WHERE product_id NOT IN (SELECT id FROM products)")
        print(f"Removed {orphans} inventory row(s) for products that no longer exist.")

    orphans = conn.execute(
        """SELECT COUNT(*) FROM order_items
           WHERE order_id NOT IN (SELECT id FROM orders)
              OR product_id NOT IN (SELECT id FROM products)"""
    ).fetchone()[0]
    if orphans:
        conn.execute(
            """DELETE FROM order_items
               WHERE order_id NOT IN (SELECT id FROM orders)
                  OR product_id NOT IN (SELECT id FROM products)"""
        )
        print(f"Removed {orphans} order item(s) pointing at missing orders/products.")

    orphans = conn.execute(
        "SELECT COUNT(*) FROM stock_movements WHERE product_id NOT IN (SELECT id FROM products)"
    ).fetchone()[0]
    if orphans:
        conn.execute("DELETE FROM stock_movements WHERE product_id NOT IN (SELECT id FROM products)")
        print(f"Removed {orphans} movement(s) for products that no longer exist.")

    fixed = conn.execute(
        """UPDATE stock_movements SET user_id = NULL
           WHERE user_id IS NOT NULL AND user_id NOT IN (SELECT id FROM users)"""
    ).rowcount
    if fixed:
        print(f"Cleared the user link on {fixed} movement(s) (user no longer exists).")

    fixed = conn.execute(
        """UPDATE stock_movements SET order_id = NULL
           WHERE order_id IS NOT NULL AND order_id NOT IN (SELECT id FROM orders)"""
    ).rowcount
    if fixed:
        print(f"Cleared the order link on {fixed} movement(s) (order no longer exists).")

    # --- Rebuild the tables with foreign keys ---------------------------------
    REBUILDS = {
        "inventory": (
            """CREATE TABLE inventory_new (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id),
                quantity INTEGER DEFAULT 0
            )""",
            "CREATE INDEX ix_inventory_product_id ON inventory (product_id)",
        ),
        "order_items": (
            """CREATE TABLE order_items_new (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                product_id INTEGER NOT NULL REFERENCES products(id),
                quantity INTEGER NOT NULL
            )""",
            "CREATE INDEX ix_order_items_order_id ON order_items (order_id)",
        ),
        "stock_movements": (
            """CREATE TABLE stock_movements_new (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id),
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                order_id INTEGER REFERENCES orders(id),
                quantity_change INTEGER NOT NULL,
                reason VARCHAR NOT NULL,
                note VARCHAR DEFAULT '',
                created_at DATETIME
            )""",
            "CREATE INDEX ix_stock_movements_product_id ON stock_movements (product_id)",
        ),
    }

    for table, (create_sql, index_sql) in REBUILDS.items():
        already_has_fk = len(conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()) > 0
        if already_has_fk:
            print(f"{table}: already has foreign keys, skipping.")
            continue
        conn.execute(f"DROP TABLE IF EXISTS {table}_new")   # clean up any failed earlier run
        conn.execute(create_sql)
        conn.execute(f"INSERT INTO {table}_new SELECT * FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        conn.execute(index_sql)
        print(f"{table}: rebuilt with foreign keys.")

    conn.commit()

    # --- Verify ----------------------------------------------------------------
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        print(f"WARNING: foreign_key_check reported problems: {problems}")
    else:
        print("Verification passed: no foreign key violations.")
    conn.close()
    print("Done. Delete the .backup file once you are happy with the result.")

if __name__ == "__main__":
    main()

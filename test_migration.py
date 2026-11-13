"""Temporary verification for migrate_add_foreign_keys.py.

Builds a fake "legacy" database (old schema, no foreign keys, containing
orphan rows), runs the migration against it, and checks that:
  - foreign keys were added,
  - all valid data survived,
  - orphan rows were cleaned up sensibly.
Safe to delete after use — it never touches ims.db.
"""
import os
import sqlite3
import subprocess
import sys

DB = "migration_check.db"

# Start clean
if os.path.exists(DB):
    os.remove(DB)
for f in os.listdir("."):
    if f.startswith(DB + ".backup-"):
        os.remove(f)

conn = sqlite3.connect(DB)
conn.executescript("""
CREATE TABLE products (id INTEGER PRIMARY KEY, sku VARCHAR NOT NULL UNIQUE, name VARCHAR NOT NULL, price NUMERIC(10,2), reorder_point INTEGER);
CREATE TABLE inventory (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, quantity INTEGER);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_name VARCHAR NOT NULL, status VARCHAR);
CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity INTEGER NOT NULL);
CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL UNIQUE, full_name VARCHAR, hashed_password VARCHAR NOT NULL, role VARCHAR NOT NULL, is_active BOOLEAN, created_at DATETIME);
CREATE TABLE stock_movements (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, user_id INTEGER, order_id INTEGER, quantity_change INTEGER NOT NULL, reason VARCHAR NOT NULL, note VARCHAR, created_at DATETIME);

INSERT INTO products (id, sku, name) VALUES (1, 'A1', 'Product A'), (2, 'B2', 'Product B');
INSERT INTO users (id, email, hashed_password, role) VALUES (1, 'a@b.c', 'x', 'admin');
INSERT INTO orders (id, customer_name) VALUES (1, 'Jane');
-- row 2 references product 999 which does not exist (orphan)
INSERT INTO inventory (id, product_id, quantity) VALUES (1, 1, 10), (2, 999, 5);
-- row 2 references order 777 which does not exist (orphan)
INSERT INTO order_items (id, order_id, product_id, quantity) VALUES (1, 1, 1, 2), (2, 777, 1, 3);
-- movement 2 has a user 888 that does not exist (link should be cleared)
-- movement 3 has product 999 that does not exist (row should be removed)
INSERT INTO stock_movements (id, product_id, user_id, order_id, quantity_change, reason) VALUES
 (1, 1, 1, 1, -2, 'order'),
 (2, 1, 888, NULL, 5, 'manual_adjustment'),
 (3, 999, 1, 42, 1, 'order');
""")
conn.commit()
conn.close()

result = subprocess.run([sys.executable, "migrate_add_foreign_keys.py", DB],
                        capture_output=True, text=True)
print(result.stdout)
if result.returncode != 0:
    print("Migration script FAILED:")
    print(result.stderr)
    sys.exit(1)

conn = sqlite3.connect(DB)
conn.execute("PRAGMA foreign_keys=ON")

checks = []

def check(name, condition, info=""):
    checks.append(condition)
    print(("PASS  " if condition else "FAIL  ") + name + ("" if condition else f"  ->  {info}"))

def fk_count(table):
    return len(conn.execute(f"PRAGMA foreign_key_list({table})").fetchall())

check("inventory now has a foreign key", fk_count("inventory") == 1, fk_count("inventory"))
check("order_items now has two foreign keys", fk_count("order_items") == 2, fk_count("order_items"))
check("stock_movements now has three foreign keys", fk_count("stock_movements") == 3, fk_count("stock_movements"))

check("valid inventory row survived", conn.execute("SELECT quantity FROM inventory WHERE id = 1").fetchone()[0] == 10)
check("orphan inventory row was removed", conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 1)
check("orphan order item was removed", conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0] == 1)
check("movement with an unknown product was removed", conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 2)
check("movement with an unknown user kept its history (user link cleared)",
      conn.execute("SELECT user_id FROM stock_movements WHERE id = 2").fetchone()[0] is None)
check("all products/users/orders untouched",
      conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2
      and conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
      and conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1)

violations = conn.execute("PRAGMA foreign_key_check").fetchall()
check("foreign_key_check is clean", violations == [], violations)

try:
    conn.execute("INSERT INTO inventory (product_id, quantity) VALUES (12345, 1)")
    conn.rollback()
    check("the migrated database now rejects orphan inserts", False)
except sqlite3.IntegrityError:
    check("the migrated database now rejects orphan inserts", True)
conn.close()

passed = sum(1 for ok in checks if ok)
print(f"\n{passed}/{len(checks)} migration checks passed")
sys.exit(0 if passed == len(checks) else 1)

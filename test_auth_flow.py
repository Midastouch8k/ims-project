"""Smoke test for the authentication layer.

Starts against a running server (see README.md):
    $env:IMS_DATABASE_URL = "sqlite:///ims_test.db"
    venv\\Scripts\\python.exe -m uvicorn main:app --port 8001
    venv\\Scripts\\python.exe test_auth_flow.py

Uses a throwaway database (ims_test.db) so your real ims.db is untouched.
Set the same IMS_DATABASE_URL in this process so the schema checks below run
against the same database the server is using (SQLite or PostgreSQL).
"""
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8001"
DB_URL = os.environ.get("IMS_DATABASE_URL", "sqlite:///ims.db")


def wait_for_server(timeout_seconds=30):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + "/", timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise SystemExit(f"Server did not start at {BASE}")


def call(method, path, data=None, token=None, form=False):
    """Returns (status_code, parsed_body)."""
    url = BASE + path
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    def parse(raw):
        try:
            return json.loads(raw or "null")
        except json.JSONDecodeError:
            return raw   # HTML/CSS responses come back as plain text

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, parse(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, parse(e.read().decode())


results = []

def check(name, condition, info=""):
    results.append(condition)
    if condition:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  ->  {info}")


wait_for_server()

# --- 1. Everything is locked behind login -----------------------------------
status, body = call("GET", "/products")
check("GET /products without a token is rejected (401)", status == 401, (status, body))

status, body = call("POST", "/products", {"sku": "T-1", "name": "Widget", "price": 9.99, "reorder_point": 5})
check("Anonymous product creation is rejected (401)", status == 401, (status, body))

# --- 2. The first registered account becomes the admin ----------------------
status, body = call("POST", "/auth/register",
                    {"email": "admin@test.com", "password": "admin-pass-123", "full_name": "Test Admin"})
check("First registration becomes admin", status == 200 and body.get("user", {}).get("role") == "admin", (status, body))

# --- 3. Registration is locked down after that ------------------------------
status, body = call("POST", "/auth/register", {"email": "staff@test.com", "password": "staff-pass-123"})
check("Second registration without a token is rejected (403)", status == 403, (status, body))

# --- 4. Login ----------------------------------------------------------------
status, body = call("POST", "/auth/login", {"username": "admin@test.com", "password": "wrong-password"}, form=True)
check("Login with a wrong password is rejected (401)", status == 401, (status, body))

status, body = call("POST", "/auth/login", {"username": "admin@test.com", "password": "admin-pass-123"}, form=True)
check("Admin login returns a token", status == 200 and body.get("access_token"), (status, body))
admin_token = body.get("access_token", "")

status, body = call("GET", "/auth/me", token=admin_token)
check("GET /auth/me returns the admin account", status == 200 and body.get("role") == "admin", (status, body))

status, body = call("POST", "/auth/register",
                    {"email": "bad-email", "password": "x" * 10}, token=admin_token)
check("Invalid email is rejected (400)", status == 400, (status, body))

# --- 5. Admin creates and manages a staff account ---------------------------
status, body = call("POST", "/auth/register",
                    {"email": "staff@test.com", "password": "staff-pass-123", "full_name": "Test Staff"},
                    token=admin_token)
check("Admin can register a staff user", status == 200 and body.get("user", {}).get("role") == "staff", (status, body))

status, body = call("POST", "/auth/login", {"username": "staff@test.com", "password": "staff-pass-123"}, form=True)
check("Staff login works", status == 200 and body.get("access_token"), (status, body))
staff_token = body.get("access_token", "")

status, body = call("GET", "/users", token=staff_token)
check("Staff cannot list users (403)", status == 403, (status, body))

status, body = call("GET", "/users", token=admin_token)
check("Admin can list users", status == 200, (status, body))

# --- 6. Role enforcement on business endpoints ------------------------------
product_data = {"sku": "T-SKU-1", "name": "Test Widget", "price": 9.99, "reorder_point": 5}

status, body = call("POST", "/products", product_data, token=staff_token)
check("Staff cannot create products (403)", status == 403, (status, body))

status, body = call("POST", "/stock/adjust", {"product_id": 1, "quantity": 10}, token=staff_token)
check("Staff cannot adjust stock (403)", status == 403, (status, body))

status, body = call("POST", "/products", product_data, token=admin_token)
check("Admin creates a product", status == 200 and body.get("product_id"), (status, body))
product_id = body.get("product_id")

status, body = call("POST", "/products", product_data, token=admin_token)
check("Duplicate SKU is rejected (409)", status == 409, (status, body))

status, body = call("POST", "/stock/adjust", {"product_id": product_id, "quantity": 10, "note": "initial stock delivery"}, token=admin_token)
check("Admin adjusts stock", status == 200, (status, body))

status, body = call("POST", "/stock/adjust", {"product_id": product_id, "quantity": 3}, token=admin_token)
check("Adjustment without a note is rejected (422)", status == 422, (status, body))

status, body = call("POST", "/stock/adjust", {"product_id": product_id, "quantity": 3, "note": "   "}, token=admin_token)
check("Adjustment with a blank note is rejected (400)", status == 400, (status, body))

# --- 7. Staff can place and cancel orders -----------------------------------
status, body = call("POST", "/orders",
                    {"customer_name": "Test Customer", "product_id": product_id, "quantity": 2},
                    token=staff_token)
check("Staff places an order", status == 200 and body.get("order_id"), (status, body))
order_id = body.get("order_id")

status, body = call("POST", f"/orders/{order_id}/cancel", token=staff_token)
check("Staff cancels the order", status == 200, (status, body))

status, body = call("POST", f"/orders/{order_id}/cancel", token=staff_token)
check("Cancelling twice is rejected (409)", status == 409, (status, body))

# --- 8. Reading data works for both roles -----------------------------------
status, body = call("GET", "/inventory", token=staff_token)
check("Staff reads inventory", status == 200, (status, body))

status, body = call("GET", "/inventory/low-stock", token=admin_token)
check("Admin reads the low-stock report", status == 200, (status, body))

status, body = call("GET", "/orders", token=staff_token)
check("Staff reads orders", status == 200, (status, body))

# --- 9. Stock movement audit log ---------------------------------------------
status, body = call("GET", "/stock/movements")
check("Audit log without a token is rejected (401)", status == 401, (status, body))

status, body = call("GET", "/stock/movements", token=staff_token)
check("Staff can read the audit log", status == 200, (status, body))

movements = body if isinstance(body, list) else []
own = [m for m in movements if m.get("product_id") == product_id]

check("Manual adjustment was recorded (+10 by admin, with note)",
      any(m["quantity_change"] == 10 and m["reason"] == "manual_adjustment"
          and m.get("user_email") == "admin@test.com"
          and m.get("note") == "initial stock delivery" for m in own), own)

check("Placing the order was recorded (-2 by staff, linked to the order)",
      any(m["quantity_change"] == -2 and m["reason"] == "order"
          and m.get("user_email") == "staff@test.com"
          and m.get("order_id") == order_id for m in own), own)

check("Cancelling the order was recorded (+2)",
      any(m["quantity_change"] == 2 and m["reason"] == "order_cancelled"
          and m.get("order_id") == order_id for m in own), own)

check("Audit log is newest-first",
      len(own) >= 3 and own == sorted(own, key=lambda m: m["id"], reverse=True), own)

status, body = call("GET", f"/stock/movements?product_id={product_id}", token=admin_token)
filtered = body if isinstance(body, list) else []
check("Product filter returns only that product's history",
      status == 200 and len(filtered) == len(own)
      and all(m["product_id"] == product_id for m in filtered), (status, body))

# --- 10. Data integrity: atomic stock + foreign keys --------------------------
status, body = call("POST", "/orders",
                    {"customer_name": "Oversell Customer", "product_id": product_id, "quantity": 999999},
                    token=staff_token)
check("Overselling is rejected (409)", status == 409, (status, body))

status, body = call("POST", "/orders",
                    {"customer_name": "Ghost Customer", "product_id": 999999, "quantity": 1},
                    token=staff_token)
check("Ordering a nonexistent product is rejected (409)", status == 409, (status, body))

status, body = call("POST", "/orders",
                    {"customer_name": "Zero Customer", "product_id": product_id, "quantity": 0},
                    token=staff_token)
check("Order quantity of 0 is rejected (400)", status == 400, (status, body))

# The schema actually declares foreign keys (checked directly in the test DB).
# Works against SQLite (PRAGMA) and PostgreSQL (pg_constraint).
if DB_URL.startswith("sqlite"):
    sqlite_file = DB_URL.split("///", 1)[1] if "///" in DB_URL else "ims.db"
    conn = sqlite3.connect(sqlite_file)
    fk_counts = {t: len(conn.execute(f"PRAGMA foreign_key_list({t})").fetchall())
                 for t in ("inventory", "order_items", "stock_movements")}
    conn.close()

    # ...and the database enforces them (orphan rows are impossible now)
    enforced = False
    conn = sqlite3.connect(sqlite_file)
    try:
        conn.execute("PRAGMA foreign_keys=ON")   # enforcement is per-connection in SQLite
        conn.execute("INSERT INTO inventory (product_id, quantity) VALUES (999999, 5)")
        conn.rollback()
    except sqlite3.IntegrityError:
        enforced = True
    finally:
        conn.close()
else:
    # PostgreSQL: introspect the live catalog and attempt an orphan insert.
    import psycopg
    import psycopg.errors as pg_errors
    with psycopg.connect(DB_URL) as conn:
        rows = conn.execute(
            "SELECT r.relname, COUNT(*) FROM pg_constraint c "
            "JOIN pg_class r ON r.oid = c.conrelid "
            "WHERE c.contype = 'f' AND r.relname IN "
            "('inventory', 'order_items', 'stock_movements') GROUP BY r.relname"
        ).fetchall()
    fk_counts = {t: dict(rows).get(t, 0)
                 for t in ("inventory", "order_items", "stock_movements")}
    enforced = False
    conn = psycopg.connect(DB_URL)
    try:
        conn.execute("INSERT INTO inventory (product_id, quantity) VALUES (999999, 5)")
        conn.commit()
    except pg_errors.ForeignKeyViolation:
        enforced = True
    finally:
        conn.close()
check("inventory declares a foreign key", fk_counts["inventory"] >= 1, fk_counts)
check("order_items declares two foreign keys", fk_counts["order_items"] >= 2, fk_counts)
check("stock_movements declares three foreign keys", fk_counts["stock_movements"] >= 3, fk_counts)
check("Orphan inventory rows are rejected by the database", enforced)

# --- 11. Dashboard + themed docs, served by the API itself --------------------
status, body = call("GET", "/")
check("GET / serves the dashboard (with the name stamp)",
      isinstance(body, str) and "Michael Tucker" in body and "Sign in" in body,
      str(body)[:100])

status, body = call("GET", "/health")
check("GET /health responds with the API message",
      status == 200 and isinstance(body, dict) and "running" in str(body.get("message", "")),
      (status, body))

status, body = call("GET", "/swagger-theme.css")
check("The dark docs theme is served",
      status == 200 and isinstance(body, str) and ".topbar" in body, status)

passed = sum(1 for ok in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
raise SystemExit(0 if passed == len(results) else 1)

# Inventory & Order Management System (IMS)

A FastAPI + SQLite inventory and order management system with JWT authentication,
role-based access control, and a browser dashboard.

**🔴 Live demo:** https://ims-project-8zqy.onrender.com *(free tier — first load after
inactivity takes ~60 seconds to wake up; all later requests are instant)*

**Full-Stack Inventory & Order Management System • Designed & Built by Michael Tucker**

## Features

- **Products** — SKU, name, price, reorder point (duplicate SKUs are rejected)
- **Inventory** — stock levels per product, with admin-only adjustments that **require a reason**
- **Stock audit trail** — every stock change (order, cancellation, manual adjustment) is permanently
  recorded with who, when, how much, and why. Query via `GET /stock/movements` (newest first,
  optional `?product_id=` filter) or the dashboard's "Stock History" card
- **Orders** — place orders (stock is checked and deducted), cancel orders (stock is returned)
- **Low-stock report** — items at or below their reorder point
- **Authentication** — JWT bearer tokens with bcrypt-hashed passwords
- **Roles** — `admin` and `staff`, enforced per endpoint:

| Endpoint | Admin | Staff | Anonymous |
|---|---|---|---|
| `POST /products`, `POST /stock/adjust`, `GET /users` | ✅ | ❌ 403 | ❌ 401 |
| Place/cancel orders, list products/orders/inventory, low-stock report, audit trail | ✅ | ✅ | ❌ 401 |
| `POST /auth/register` | ✅ | ❌ 403 | first user only |

The **first account ever registered automatically becomes the admin**;
after that, only an admin can create further accounts.

## Setup

```powershell
# 1. Create/activate a virtual environment (already present as .\venv)
.\venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the API
python -m uvicorn main:app --port 8000
```

- **API docs (Swagger):** http://127.0.0.1:8000/docs — dark-themed to match the app,
  includes an "Authorize" button
- **Dashboard:** open **http://127.0.0.1:8000** in a browser and sign in — the API serves
  the dashboard itself, so don't open `index.html` directly from disk. Sidebar navigation
  with a KPI dashboard, Products / Inventory / Orders / Stock History sections (+ Users for
  admins), modal forms for placing orders, adjusting stock (reason required) and adding
  products, and toast notifications. Staff accounts see a limited, view-only UI.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `IMS_DATABASE_URL` | `sqlite:///ims.db` | Database location; can point at PostgreSQL later |
| `IMS_SECRET_KEY` | a dev value | **Set a long random value in production** — it signs login tokens |

## Testing

The smoke test runs against a throwaway database so your real data is untouched:

```powershell
$env:IMS_DATABASE_URL = "sqlite:///ims_test.db"
python -m uvicorn main:app --port 8001          # window 1
python test_auth_flow.py                        # window 2 (23 checks)
```

Delete `ims_test.db` (after stopping the server) to reset the test database —
the tests assume a fresh one.

## Roadmap

1. ~~Authentication + roles~~ ✅
2. ~~Stock movement audit log~~ ✅
3. ~~Foreign keys + transactional stock deduction~~ ✅
4. PostgreSQL + Alembic migrations + deployment
5. Suppliers & purchase orders (receiving stock)
6. CSV import/export, reports, low-stock email notifications

## Upgrading an existing ims.db (one-time)

Databases created before the foreign-key update lack the constraints. To add
them (keeping all data), with the API server STOPPED:

```powershell
.\venv\Scripts\python.exe migrate_add_foreign_keys.py
```

This automatically creates a backup file first, cleans up orphan rows, rebuilds
the three affected tables with foreign keys, and verifies the result. It is
safe to re-run (already-migrated tables are skipped).

## Deploying (making it public)

The repository is deploy-ready: the API serves the dashboard at `/`, so one
hosted service runs everything.

1. **GitHub** — create an empty repository (no README), then from the project folder:
   ```powershell
   git remote add origin https://github.com/<your-username>/<repo>.git
   git push -u origin main
   ```
2. **Host** — [Render.com](https://render.com) free tier: *New → Web Service → connect the repo*
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Environment variables:
     | Variable | Why |
     |---|---|
     | `IMS_SECRET_KEY` | a long random string — signs login tokens |
     | `IMS_ADMIN_EMAIL` / `IMS_ADMIN_PASSWORD` | creates **your** admin at startup, so a stranger can't claim the first-admin slot |
3. **Database** — the free tier wipes local files on redeploy, so point
   `IMS_DATABASE_URL` at a free PostgreSQL (e.g. [Neon](https://neon.tech)).
   Tables are created automatically on first start.

---

Full-Stack Inventory & Order Management System • Designed & Built by **Michael Tucker**

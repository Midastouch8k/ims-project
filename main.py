"""Full-Stack Inventory & Order Management System • Designed & Built by Michael Tucker

Backend API (FastAPI + SQLAlchemy), providing:
- JWT authentication with admin/staff roles
- Products, inventory, orders and a permanent stock-movement audit log
- Foreign-key data integrity and atomic (oversell-proof) stock deduction

Run:  python -m uvicorn main:app --port 8000   →   docs at /docs
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Numeric, Boolean, DateTime, ForeignKey, event, update
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------
# The database can be swapped (e.g. PostgreSQL) without touching the code.
DATABASE_URL = os.environ.get("IMS_DATABASE_URL", "sqlite:///ims.db")

# SQLAlchemy needs the driver named explicitly for PostgreSQL. The app uses
# psycopg 3 (see requirements.txt), so rewrite postgresql:// → postgresql+psycopg://
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

# Used to sign login tokens. Set IMS_SECRET_KEY in the environment for production!
SECRET_KEY = os.environ.get("IMS_SECRET_KEY", "dev-only-secret-change-me-0123456789abcdef")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8  # tokens last one workday

# PostgreSQL through a pooler (e.g. Neon's PgBouncer): server-side prepared
# statements and stale pooled connections must both be handled.
if DATABASE_URL.startswith("postgresql+psycopg"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"prepare_threshold": None},  # PgBouncer-safe
        pool_pre_ping=True,                        # survive idle/cloud restarts
    )
else:
    engine = create_engine(DATABASE_URL)

# SQLite ignores foreign key constraints unless this pragma is enabled on
# every connection (it is OFF by default, even when the schema defines them).
@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    if DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

app = FastAPI(
    title="Full-Stack Inventory & Order Management System",
    description="Designed & Built by **Michael Tucker**",
    # The default /docs page is disabled so our themed version (registered
    # later, in section 9) takes over the same URL.
    docs_url=None,
)

# Only allow known front-ends to call the API. (The previous "*" + credentials
# combination is invalid and unsafe; this list can be extended as needed.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "null",                     # index.html opened straight from disk (file://)
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:5500",    # VS Code Live Server defaults
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 2. Database models
# ---------------------------------------------------------------------------
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    price = Column(Numeric(10, 2), default=0)
    reorder_point = Column(Integer, default=0)

class Inventory(Base):
    __tablename__ = "inventory"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Integer, default=0)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    customer_name = Column(String, nullable=False)
    status = Column(String, default="confirmed")

class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, default="")
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False, default="staff")   # "admin" or "staff"
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class StockMovement(Base):
    """The audit trail: one permanent row for every change to stock levels.
    Answers "why do we have 86 left?" — who, when, how much, and why."""
    __tablename__ = "stock_movements"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)  # who made the change
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)                     # set when an order caused it
    quantity_change = Column(Integer, nullable=False)  # positive = stock in, negative = stock out
    reason = Column(String, nullable=False)            # "order", "order_cancelled", "manual_adjustment"
    note = Column(String, default="")                  # free-text detail (required for manual adjustments)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

Base.metadata.create_all(engine)

# ---------------------------------------------------------------------------
# 3. Shared dependency: one database session per request
# ---------------------------------------------------------------------------
def get_db():
    """One session per request, always closed afterwards (fixes session leaks)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def record_stock_movement(db: Session, product_id: int, quantity_change: int, reason: str,
                          user_id: int | None = None, order_id: int | None = None, note: str = "") -> None:
    """Write one permanent row describing why stock changed.
    Every endpoint that touches stock levels must call this before committing."""
    db.add(StockMovement(
        product_id=product_id,
        user_id=user_id,
        order_id=order_id,
        quantity_change=quantity_change,
        reason=reason,
        note=note,
    ))

# ---------------------------------------------------------------------------
# 4. Password hashing + login tokens
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))

def create_access_token(user: User) -> str:
    payload = {
        "sub": str(user.id),   # who the token belongs to
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def seed_first_admin():
    """Production safety: on a public deployment you don't want a race where a
    stranger registers first and becomes admin. Set IMS_ADMIN_EMAIL and
    IMS_ADMIN_PASSWORD in the environment and this account is created at startup."""
    email = os.environ.get("IMS_ADMIN_EMAIL")
    password = os.environ.get("IMS_ADMIN_PASSWORD")
    if not email or not password:
        return
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            db.add(User(
                email=email.strip().lower(),
                full_name="Administrator",
                hashed_password=hash_password(password),
                role="admin",
            ))
            db.commit()
            print(f"Seeded the admin account: {email}")
    finally:
        db.close()

seed_first_admin()

# ---------------------------------------------------------------------------
# 5. Security dependencies
# ---------------------------------------------------------------------------
# Reads "Authorization: Bearer <token>" from the request. auto_error=False means
# a missing header simply gives us None instead of an automatic 401.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    """Raises 401 unless the request carries a valid token for an active user."""
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (jwt.PyJWTError, TypeError, ValueError):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive.")
    return user

def get_optional_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User | None:
    """Same as get_current_user, but returns None instead of raising. Used by
    /auth/register so the very first account can be created anonymously."""
    if token is None:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (jwt.PyJWTError, TypeError, ValueError):
        return None
    return db.query(User).filter(User.id == user_id).first()

def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Add this to any endpoint that only admins may use."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    return current_user

# ---------------------------------------------------------------------------
# 6. Schemas (the shape of request bodies)
# ---------------------------------------------------------------------------
class ProductCreate(BaseModel):
    sku: str
    name: str
    price: float
    reorder_point: int

class StockAdjust(BaseModel):
    product_id: int
    quantity: int
    note: str   # required: why is stock being adjusted? (goes into the audit log)

class OrderCreate(BaseModel):
    customer_name: str
    product_id: int
    quantity: int

class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str = ""
    role: str = "staff"   # only honoured when an admin creates the account

VALID_ROLES = {"admin", "staff"}

def user_to_dict(user: User) -> dict:
    """Never include the password hash in a response."""
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
    }

# ---------------------------------------------------------------------------
# 7. Authentication endpoints
# ---------------------------------------------------------------------------
@app.post("/auth/register")
def register_account(user_in: UserCreate, db: Session = Depends(get_db), creator: User | None = Depends(get_optional_user)):
    """Create an account.
    - If the system has no users yet, the first account automatically becomes the Admin.
    - After that, only an admin can create further accounts."""
    is_first_user = db.query(User).count() == 0

    if not is_first_user and (creator is None or creator.role != "admin"):
        raise HTTPException(status_code=403, detail="Only an admin can create additional accounts.")

    email = user_in.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email address is required.")
    if len(user_in.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if user_in.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of: {', '.join(sorted(VALID_ROLES))}.")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    # Role logic: first user -> admin; admin-created -> requested role.
    role = "admin" if is_first_user else user_in.role

    new_user = User(
        email=email,
        full_name=user_in.full_name,
        hashed_password=hash_password(user_in.password),
        role=role,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": f"Account created for {new_user.email} ({role}).", "user": user_to_dict(new_user)}

@app.post("/auth/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Exchange email + password for a bearer token. Uses the standard OAuth2
    form so the 'Authorize' button in the Swagger docs works out of the box."""
    user = db.query(User).filter(User.email == form_data.username.strip().lower()).first()
    if user is None or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account is disabled.")
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": user_to_dict(user),
    }

@app.get("/auth/me")
def who_am_i(current_user: User = Depends(get_current_user)):
    """Returns the account the current token belongs to."""
    return user_to_dict(current_user)

@app.get("/users")
def list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [user_to_dict(u) for u in db.query(User).all()]

# ---------------------------------------------------------------------------
# 8. Product / stock / order endpoints
# ---------------------------------------------------------------------------
@app.get("/health", include_in_schema=False)
def health_check():
    """Liveness check. The dashboard itself is served at / — one URL runs everything."""
    return {"message": "Inventory system is up and running!"}

@app.post("/products")
def create_product(product: ProductCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if db.query(Product).filter(Product.sku == product.sku).first():
        raise HTTPException(status_code=409, detail=f"A product with SKU '{product.sku}' already exists.")
    new_product = Product(
        sku=product.sku,
        name=product.name,
        price=product.price,
        reorder_point=product.reorder_point,
    )
    db.add(new_product)
    db.commit()
    db.refresh(new_product)
    return {"message": "Product created!", "product_id": new_product.id}

@app.get("/products")
def list_products(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(Product).all()

@app.post("/stock/adjust")
def adjust_stock(data: StockAdjust, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    product = db.query(Product).filter(Product.id == data.product_id).first()
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product #{data.product_id} not found.")
    if not data.note.strip():
        raise HTTPException(status_code=400, detail="A note explaining the adjustment is required.")

    inv = db.query(Inventory).filter(Inventory.product_id == data.product_id).first()
    if inv:
        inv.quantity += data.quantity
    else:
        inv = Inventory(product_id=data.product_id, quantity=data.quantity)
        db.add(inv)

    record_stock_movement(db, product_id=data.product_id, quantity_change=data.quantity,
                          reason="manual_adjustment", user_id=admin.id, note=data.note.strip())

    db.commit()
    return {"message": f"Stock for '{product.name}' adjusted by {data.quantity}.", "new_quantity": inv.quantity}

@app.get("/inventory")
def view_inventory(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(Inventory).all()

@app.get("/inventory/low-stock")
def low_stock(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    # Find items where quantity is less than or equal to the reorder point
    # by joining the Inventory table with the Product table to get the names
    low_items = (
        db.query(Product, Inventory)
        .join(Inventory, Product.id == Inventory.product_id)
        .filter(Inventory.quantity <= Product.reorder_point)
        .all()
    )
    results = []
    for product, inv in low_items:
        results.append({
            "sku": product.sku,
            "name": product.name,
            "quantity_left": inv.quantity,
            "reorder_point": product.reorder_point,
        })
    return results

@app.get("/stock/movements")
def list_stock_movements(product_id: int | None = None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The stock audit trail, newest first. Add ?product_id=3 to see one
    product's history. Shows who changed what, when, and why."""
    query = (
        db.query(StockMovement, Product, User)
        .join(Product, Product.id == StockMovement.product_id)
        .outerjoin(User, User.id == StockMovement.user_id)   # outer: user may have been deleted
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
    )
    if product_id is not None:
        query = query.filter(StockMovement.product_id == product_id)

    results = []
    for movement, product, user in query.limit(100).all():
        results.append({
            "id": movement.id,
            "created_at": movement.created_at,
            "product_id": product.id,
            "product_name": product.name,
            "user_email": user.email if user else None,
            "quantity_change": movement.quantity_change,
            "reason": movement.reason,
            "note": movement.note,
            "order_id": movement.order_id,
        })
    return results

@app.post("/orders")
def create_order(order: OrderCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if order.quantity <= 0:
        raise HTTPException(status_code=400, detail="Order quantity must be at least 1.")

    # 1. Deduct the stock ATOMICALLY: the UPDATE only matches a row that still
    #    has enough quantity at the moment it runs, so two simultaneous orders
    #    can no longer both pass a Python-level check and oversell.
    result = db.execute(
        update(Inventory)
        .where(
            Inventory.product_id == order.product_id,
            Inventory.quantity >= order.quantity,
        )
        .values(quantity=Inventory.quantity - order.quantity)
    )
    if result.rowcount == 0:
        db.rollback()
        inv = db.query(Inventory).filter(Inventory.product_id == order.product_id).first()
        available = inv.quantity if inv else 0
        raise HTTPException(status_code=409, detail=f"Not enough stock! Only {available} available.")

    # 2. Create the order, its line item and the audit entry in ONE transaction,
    #    so a crash halfway through can never leave a half-created order behind.
    new_order = Order(customer_name=order.customer_name)
    db.add(new_order)
    db.flush()   # assigns new_order.id without committing

    db.add(OrderItem(order_id=new_order.id, product_id=order.product_id, quantity=order.quantity))

    # Audit trail: stock left the building because of this order
    record_stock_movement(db, product_id=order.product_id, quantity_change=-order.quantity,
                          reason="order", user_id=current_user.id, order_id=new_order.id,
                          note=f"Order #{new_order.id} for {order.customer_name}")
    db.commit()

    return {
        "message": f"Order #{new_order.id} placed successfully for {order.customer_name}!",
        "order_id": new_order.id,
    }

@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    # 1. Find the order
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail=f"Order #{order_id} not found.")
    if order.status == "cancelled":
        raise HTTPException(status_code=409, detail=f"Order #{order_id} is already cancelled.")

    # 2. Find the items in that order
    items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()

    # 3. Put the stock back
    for item in items:
        inv = db.query(Inventory).filter(Inventory.product_id == item.product_id).first()
        if inv:
            inv.quantity += item.quantity
            # Audit trail: stock came back because of this cancellation
            record_stock_movement(db, product_id=item.product_id, quantity_change=item.quantity,
                                  reason="order_cancelled", user_id=current_user.id, order_id=order_id,
                                  note=f"Order #{order_id} cancelled — stock returned")

    # 4. Mark as cancelled
    order.status = "cancelled"
    db.commit()

    return {"message": f"Order #{order_id} has been cancelled and stock returned."}

@app.get("/orders")
def list_orders(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(Order).all()

# ---------------------------------------------------------------------------
# 9. Dashboard + themed API docs (served by this same app)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

@app.get("/", include_in_schema=False)
def serve_dashboard():
    """The dashboard UI. Serving it from the API means one URL runs everything,
    and a deployed app needs no cross-origin configuration."""
    return FileResponse(BASE_DIR / "index.html")

@app.get("/docs", include_in_schema=False)
def themed_docs():
    """Swagger docs in the app's dark 'Deep Ocean' theme."""
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " — API Docs",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_css_url="/swagger-theme.css",
    )

@app.get("/swagger-theme.css", include_in_schema=False)
def swagger_theme():
    return FileResponse(BASE_DIR / "swagger-theme.css", media_type="text/css")

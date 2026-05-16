"""
Database utilities for supermarket price tracking.
Uses PostgreSQL with SQLAlchemy ORM for reliable, concurrent access.
"""

import os
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# DATABASE CONFIGURATION
# ═══════════════════════════════════════════════════════════════

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{os.getenv('DB_USER', 'postgresJuan')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'supermercado')}"
)

# Crear engine con connection pooling
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,  # Valida conexiones antes de usarlas
    pool_size=5,         # Tamaño del pool
    max_overflow=10,     # Conexiones adicionales permitidas
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db():
    """Context manager para conexiones de base de datos."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    """Initialize database: verify connection and create tables if they don't exist."""
    try:
        with get_db() as session:
            session.execute(text("SELECT 1"))
        print("✅ Conexión a PostgreSQL verificada")
    except Exception as e:
        print(f"❌ Error conectando a PostgreSQL: {e}")
        raise

    with get_db() as session:
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                barcode TEXT UNIQUE,
                name TEXT NOT NULL,
                brand TEXT,
                category TEXT,
                volume_ml INTEGER,
                quantity INTEGER,
                created_at TEXT,
                updated_at TEXT
            )
        """))
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS product_catalog (
                id SERIAL PRIMARY KEY,
                barcode TEXT NOT NULL,
                store TEXT NOT NULL DEFAULT '',
                product_name TEXT NOT NULL,
                brand TEXT,
                volume_ml INTEGER,
                quantity INTEGER,
                updated_by TEXT,
                updated_at TEXT,
                UNIQUE(barcode, store)
            )
        """))
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS price_observations (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                store TEXT NOT NULL,
                product_raw TEXT,
                product_normalized TEXT,
                price INTEGER,
                unit_price REAL,
                unit_type TEXT,
                quantity INTEGER,
                volume_ml INTEGER,
                barcode TEXT,
                plu TEXT,
                is_promo INTEGER DEFAULT 0,
                is_confirmed INTEGER DEFAULT 0,
                needs_confirmation INTEGER DEFAULT 0,
                ocr_confidence REAL,
                image_path TEXT,
                original_price INTEGER,
                has_discount INTEGER DEFAULT 0,
                discounted_price INTEGER,
                discount_verified_at TEXT,
                extraction_method TEXT DEFAULT 'ocr',
                promo_start_date TEXT,
                promo_end_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """))
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS price_history (
                id SERIAL PRIMARY KEY,
                product_id INTEGER,
                barcode TEXT,
                store TEXT NOT NULL,
                price INTEGER NOT NULL,
                unit_price REAL,
                is_promo INTEGER DEFAULT 0,
                has_discount INTEGER DEFAULT 0,
                discounted_price INTEGER,
                recorded_at TEXT NOT NULL
            )
        """))
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS scan_sessions (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                store TEXT NOT NULL,
                started_at TEXT,
                last_activity_at TEXT,
                is_active INTEGER DEFAULT 1,
                updated_count INTEGER DEFAULT 0
            )
        """))
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS user_shopping_lists (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                product_normalized TEXT NOT NULL,
                created_at TEXT,
                UNIQUE(user_id, product_normalized)
            )
        """))
        session.execute(text("CREATE INDEX IF NOT EXISTS idx_obs_user ON price_observations(user_id)"))
        session.execute(text("CREATE INDEX IF NOT EXISTS idx_obs_store ON price_observations(store)"))
        session.execute(text("CREATE INDEX IF NOT EXISTS idx_obs_barcode ON price_observations(barcode)"))
        session.execute(text("CREATE INDEX IF NOT EXISTS idx_obs_product ON price_observations(product_normalized)"))
        session.execute(text("CREATE INDEX IF NOT EXISTS idx_obs_created ON price_observations(created_at)"))
        session.execute(text("CREATE INDEX IF NOT EXISTS idx_history_barcode ON price_history(barcode)"))
        session.execute(text("CREATE INDEX IF NOT EXISTS idx_sessions_user ON scan_sessions(user_id)"))
        session.execute(text("CREATE INDEX IF NOT EXISTS idx_list_user ON user_shopping_lists(user_id)"))
        session.execute(text("CREATE INDEX IF NOT EXISTS idx_catalog_barcode ON product_catalog(barcode)"))
        print("✅ Tablas verificadas/creadas")


# ═══════════════════════════════════════════════════════════════
# OBSERVATIONS
# ═══════════════════════════════════════════════════════════════

def save_observation(row: dict) -> int:
    """Save a price observation and return the ID."""
    with get_db() as session:
        result = session.execute(text("""
            INSERT INTO price_observations
            (user_id, store, product_raw, product_normalized, price, unit_price,
             unit_type, quantity, volume_ml, barcode, plu, is_promo,
             needs_confirmation, ocr_confidence, image_path,
             original_price, has_discount, discounted_price,
             extraction_method, created_at, promo_start_date, promo_end_date)
            VALUES (:user_id, :store, :product_raw, :product_normalized, :price, :unit_price,
                    :unit_type, :quantity, :volume_ml, :barcode, :plu, :is_promo,
                    :needs_confirmation, :ocr_confidence, :image_path,
                    :original_price, :has_discount, :discounted_price,
                    :extraction_method, :created_at, :promo_start_date, :promo_end_date)
            RETURNING id
        """), {
            "user_id": row["user_id"],
            "store": row["store"],
            "product_raw": row.get("product_raw"),
            "product_normalized": row.get("product_normalized"),
            "price": row.get("price"),
            "unit_price": row.get("unit_price"),
            "unit_type": row.get("unit_type"),
            "quantity": row.get("quantity"),
            "volume_ml": row.get("volume_ml"),
            "barcode": row.get("barcode"),
            "plu": row.get("plu"),
            "is_promo": 1 if row.get("is_promo") else 0,
            "needs_confirmation": 1 if row.get("needs_confirmation") else 0,
            "ocr_confidence": row.get("ocr_confidence"),
            "image_path": row.get("image_path"),
            "original_price": row.get("original_price"),
            "has_discount": 1 if row.get("has_discount") else 0,
            "discounted_price": row.get("discounted_price"),
            "extraction_method": row.get("extraction_method", "ocr"),
            "created_at": row.get("created_at", datetime.utcnow().isoformat()),
            "promo_start_date": row.get("promo_start_date"),
            "promo_end_date": row.get("promo_end_date"),
        })
        return result.scalar()


def update_observation(observation_id: int, updates: dict) -> bool:
    """Update an observation with confirmed data."""
    with get_db() as session:
        set_parts = []
        values = {}

        for key, value in updates.items():
            if value is not None:
                set_parts.append(f"{key} = :{key}")
                values[key] = value

        if not set_parts:
            return False

        set_parts.append("updated_at = :updated_at")
        values["updated_at"] = datetime.utcnow().isoformat()
        values["id"] = observation_id

        query = f"UPDATE price_observations SET {', '.join(set_parts)} WHERE id = :id"
        result = session.execute(text(query), values)
        return result.rowcount > 0


def confirm_observation(observation_id: int, confirmed_data: dict) -> bool:
    """Confirm an observation with user-provided data."""
    updates = {
        "is_confirmed": 1,
        "needs_confirmation": 0,
    }

    if confirmed_data.get("price") is not None:
        updates["price"] = confirmed_data["price"]
    if confirmed_data.get("product"):
        updates["product_raw"] = confirmed_data["product"]
        updates["product_normalized"] = normalize_product_name(confirmed_data["product"])
    if confirmed_data.get("store"):
        updates["store"] = confirmed_data["store"]
    if confirmed_data.get("barcode"):
        updates["barcode"] = confirmed_data["barcode"]
    if confirmed_data.get("has_discount") is not None:
        updates["has_discount"] = 1 if confirmed_data["has_discount"] else 0
        updates["discount_verified_at"] = datetime.utcnow().isoformat()
    if confirmed_data.get("discounted_price") is not None:
        updates["discounted_price"] = confirmed_data["discounted_price"]
    if confirmed_data.get("volume_ml") is not None:
        updates["volume_ml"] = confirmed_data["volume_ml"]
    if confirmed_data.get("promo_end_date") is not None:
        updates["promo_end_date"] = confirmed_data["promo_end_date"]

    return update_observation(observation_id, updates)


def get_observation(observation_id: int) -> Optional[dict]:
    """Get a single observation by ID."""
    with get_db() as session:
        result = session.execute(
            text("SELECT * FROM price_observations WHERE id = :id"),
            {"id": observation_id}
        )
        row = result.first()
        return dict(row) if row else None


def search_products(
    query: str,
    store: Optional[str] = None,
    limit: int = 10
) -> List[dict]:
    """Search for products by name or barcode."""
    with get_db() as session:
        query_normalized = normalize_product_name(query)
        query_pattern = f"%{query_normalized}%"

        sql = """
            SELECT
                product_raw, product_normalized, store, price, unit_price, unit_type,
                is_promo, has_discount, original_price, discounted_price, barcode, created_at
            FROM price_observations
            WHERE (product_normalized ILIKE :pattern OR barcode = :barcode)
              AND (is_confirmed = 1 OR needs_confirmation = 0)
        """
        params = {"pattern": query_pattern, "barcode": query}

        if store:
            sql += " AND store = :store"
            params["store"] = store.lower()

        sql += " ORDER BY created_at DESC LIMIT :limit"
        params["limit"] = limit

        result = session.execute(text(sql), params)
        return [dict(row) for row in result.fetchall()]


def get_cheapest_store(
    barcode: Optional[str] = None,
    product_name: Optional[str] = None
) -> Optional[dict]:
    """Find the cheapest store for a product (considers discounted price)."""
    with get_db() as session:
        price_expr = "COALESCE(CASE WHEN has_discount=1 AND discounted_price IS NOT NULL THEN discounted_price ELSE price END, price)"

        if barcode:
            sql = f"""
                SELECT store, price, original_price, has_discount, discounted_price,
                       unit_price, is_promo, created_at, product_raw, barcode,
                       {price_expr} AS effective_price
                FROM price_observations
                WHERE barcode = :barcode AND price IS NOT NULL
                ORDER BY effective_price ASC
                LIMIT 1
            """
            result = session.execute(text(sql), {"barcode": barcode})
        elif product_name:
            pattern = f"%{normalize_product_name(product_name)}%"
            sql = f"""
                SELECT store, price, original_price, has_discount, discounted_price,
                       unit_price, is_promo, created_at, product_raw, barcode,
                       {price_expr} AS effective_price
                FROM price_observations
                WHERE product_normalized ILIKE :pattern AND price IS NOT NULL
                ORDER BY effective_price ASC
                LIMIT 1
            """
            result = session.execute(text(sql), {"pattern": pattern})
        else:
            return None

        row = result.first()
        return dict(row) if row else None


def compare_prices(
    barcode: Optional[str] = None,
    product_name: Optional[str] = None
) -> List[dict]:
    """Compare prices across all stores for a product."""
    with get_db() as session:
        price_expr = "COALESCE(CASE WHEN has_discount=1 AND discounted_price IS NOT NULL THEN discounted_price ELSE price END, price)"

        if barcode:
            sql = f"""
                SELECT store, price, original_price, has_discount, discounted_price,
                       unit_price, is_promo, MAX(created_at) as last_updated, product_raw,
                       {price_expr} AS effective_price
                FROM price_observations
                WHERE barcode = :barcode AND price IS NOT NULL
                GROUP BY store, price, original_price, has_discount, discounted_price,
                         unit_price, is_promo, product_raw
                ORDER BY effective_price ASC
            """
            result = session.execute(text(sql), {"barcode": barcode})
        elif product_name:
            pattern = f"%{normalize_product_name(product_name)}%"
            sql = f"""
                SELECT store, price, original_price, has_discount, discounted_price,
                       unit_price, is_promo, MAX(created_at) as last_updated, product_raw,
                       {price_expr} AS effective_price
                FROM price_observations
                WHERE product_normalized ILIKE :pattern AND price IS NOT NULL
                GROUP BY store, price, original_price, has_discount, discounted_price,
                         unit_price, is_promo, product_raw
                ORDER BY effective_price ASC
            """
            result = session.execute(text(sql), {"pattern": pattern})
        else:
            return []

        return [dict(row) for row in result.fetchall()]


def get_recent_observations(
    user_id: Optional[str] = None,
    store: Optional[str] = None,
    days: int = 7,
    limit: int = 50
) -> List[dict]:
    """Get recent price observations."""
    with get_db() as session:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        sql = "SELECT * FROM price_observations WHERE created_at >= :cutoff"
        params = {"cutoff": cutoff}

        if user_id:
            sql += " AND user_id = :user_id"
            params["user_id"] = user_id
        if store:
            sql += " AND store = :store"
            params["store"] = store.lower()

        sql += " ORDER BY created_at DESC LIMIT :limit"
        params["limit"] = limit

        result = session.execute(text(sql), params)
        return [dict(row) for row in result.fetchall()]


def get_price_stats(store: Optional[str] = None) -> dict:
    """Get statistics about prices."""
    with get_db() as session:
        where = "WHERE price IS NOT NULL"
        params = {}

        if store:
            where += " AND store = :store"
            params["store"] = store.lower()

        sql = f"""
            SELECT
                COUNT(*) as total_observations,
                COUNT(DISTINCT barcode) as unique_products,
                COUNT(DISTINCT store) as stores_count,
                AVG(price) as avg_price,
                SUM(CASE WHEN is_promo = 1 THEN 1 ELSE 0 END) as promo_count,
                SUM(CASE WHEN has_discount = 1 THEN 1 ELSE 0 END) as discount_count
            FROM price_observations
            {where}
        """

        result = session.execute(text(sql), params)
        row = result.first()
        return dict(row) if row else {}


# ═══════════════════════════════════════════════════════════════
# SESSIONS
# ═══════════════════════════════════════════════════════════════

def start_session(user_id: str, store: str) -> int:
    """Start a new scan session. Deactivates any previous active session."""
    with get_db() as session:
        # Deactivate previous sessions
        session.execute(
            text("UPDATE scan_sessions SET is_active = 0 WHERE user_id = :user_id AND is_active = 1"),
            {"user_id": user_id}
        )

        result = session.execute(text("""
            INSERT INTO scan_sessions (user_id, store, started_at, last_activity_at, is_active)
            VALUES (:user_id, :store, :started_at, :last_activity_at, 1)
            RETURNING id
        """), {
            "user_id": user_id,
            "store": store.lower(),
            "started_at": datetime.utcnow().isoformat(),
            "last_activity_at": datetime.utcnow().isoformat(),
        })
        return result.scalar()


def get_active_session(user_id: str) -> Optional[dict]:
    """Get the active session for a user (None if expired or not found)."""
    with get_db() as session:
        result = session.execute(
            text("""
                SELECT * FROM scan_sessions
                WHERE user_id = :user_id AND is_active = 1
                ORDER BY started_at DESC LIMIT 1
            """),
            {"user_id": user_id}
        )
        row = result.first()
        if not row:
            return None

        session_data = dict(row)

        # Auto-expire after 30 minutes of inactivity
        last_activity = datetime.fromisoformat(session_data["last_activity_at"])
        if (datetime.utcnow() - last_activity).total_seconds() > 1800:
            end_session(session_data["id"])
            return None

        return session_data


def touch_session(session_id: int):
    """Update last_activity_at to prevent expiration."""
    with get_db() as session:
        session.execute(
            text("UPDATE scan_sessions SET last_activity_at = :now WHERE id = :id"),
            {"now": datetime.utcnow().isoformat(), "id": session_id}
        )


def end_session(session_id: int) -> dict:
    """End a session and return summary."""
    with get_db() as session:
        session.execute(
            text("UPDATE scan_sessions SET is_active = 0 WHERE id = :id"),
            {"id": session_id}
        )


def increment_session_updated(session_id: int):
    """Increment the updated_count counter for a session."""
    with get_db() as session:
        session.execute(
            text("""
                UPDATE scan_sessions
                SET updated_count = updated_count + 1,
                    last_activity_at = :now
                WHERE id = :id
            """),
            {"now": datetime.utcnow().isoformat(), "id": session_id}
        )


def get_store_products(user_id: str, store: str) -> List[dict]:
    """
    Get the latest price record per product for a user+store combination.
    Used to show the numbered list when the user arrives at a store.
    """
    with get_db() as session:
        result = session.execute(text("""
            SELECT po.id, po.product_normalized, po.product_raw, po.price,
                   po.has_discount, po.original_price, po.discounted_price,
                   po.discount_verified_at, po.created_at,
                   po.promo_end_date, po.barcode, po.volume_ml
            FROM price_observations po
            INNER JOIN (
                SELECT product_normalized, MAX(created_at) AS latest
                FROM price_observations
                WHERE user_id = :user_id AND store = :store
                  AND price IS NOT NULL
                  AND product_normalized IS NOT NULL
                GROUP BY product_normalized
            ) latest ON po.product_normalized = latest.product_normalized
                    AND po.created_at = latest.latest
            WHERE po.user_id = :user_id AND po.store = :store
            ORDER BY po.product_normalized ASC
        """), {"user_id": user_id, "store": store.lower()})
        return [dict(row) for row in result.fetchall()]


# ═══════════════════════════════════════════════════════════════
# SHOPPING LIST
# ═══════════════════════════════════════════════════════════════

def shopping_list_add(user_id: str, products: List[str]):
    """Add products to the user's shopping list."""
    with get_db() as session:
        for product in products:
            normalized = normalize_product_name(product)
            if normalized:
                try:
                    session.execute(text("""
                        INSERT INTO user_shopping_lists (user_id, product_normalized)
                        VALUES (:user_id, :product_normalized)
                        ON CONFLICT (user_id, product_normalized) DO NOTHING
                    """), {
                        "user_id": user_id,
                        "product_normalized": normalized
                    })
                except Exception:
                    pass


def shopping_list_remove(user_id: str, product: str):
    """Remove a product from the user's shopping list."""
    normalized = normalize_product_name(product)
    with get_db() as session:
        session.execute(
            text("""
                DELETE FROM user_shopping_lists
                WHERE user_id = :user_id AND product_normalized ILIKE :pattern
            """),
            {"user_id": user_id, "pattern": f"%{normalized}%"}
        )


def shopping_list_clear(user_id: str) -> bool:
    """Elimina todos los productos de la lista de compras del usuario."""
    with get_db() as session:
        session.execute(
            text("DELETE FROM user_shopping_lists WHERE user_id = :user_id"),
            {"user_id": user_id}
        )
    return True


def shopping_list_get(user_id: str) -> List[str]:
    """Get the user's shopping list."""
    with get_db() as session:
        result = session.execute(
            text("""
                SELECT product_normalized FROM user_shopping_lists
                WHERE user_id = :user_id
                ORDER BY created_at
            """),
            {"user_id": user_id}
        )
        return [row[0] for row in result.fetchall()]


def get_shopping_recommendations(user_id: str) -> List[dict]:
    """
    For each product in the user's list, find the cheapest store.
    Returns recommendations with discount info.
    """
    products = shopping_list_get(user_id)
    recommendations = []

    for product in products:
        result = get_cheapest_store(product_name=product)
        if result:
            # Check if discount was recently verified (within 60 days)
            discount_unverified = False
            if result.get("has_discount") and result.get("discount_verified_at"):
                verified_at = datetime.fromisoformat(result["discount_verified_at"])
                discount_unverified = (datetime.utcnow() - verified_at).days > 60

            recommendations.append({
                "product": product,
                "store": result["store"],
                "price": result["price"],
                "has_discount": bool(result.get("has_discount")),
                "original_price": result.get("original_price"),
                "discounted_price": result.get("discounted_price"),
                "effective_price": result.get("effective_price", result["price"]),
                "discount_unverified": discount_unverified,
                "days_ago": (datetime.utcnow() - datetime.fromisoformat(result["created_at"])).days,
            })
        else:
            recommendations.append({
                "product": product,
                "store": None,
                "price": None,
                "not_found": True,
            })

    return recommendations


# ═══════════════════════════════════════════════════════════════
# PRODUCT CATALOG
# ═══════════════════════════════════════════════════════════════

def catalog_get(barcode: str, store: Optional[str] = None) -> Optional[dict]:
    """Look up a product in the catalog by barcode. Store-specific match wins over generic ('')."""
    if not barcode:
        return None
    with get_db() as session:
        # Try store-specific first
        if store:
            result = session.execute(
                text("SELECT * FROM product_catalog WHERE barcode = :barcode AND store = :store LIMIT 1"),
                {"barcode": barcode, "store": store.lower()}
            )
            row = result.first()
            if row:
                return dict(row)
        # Fall back to generic entry (store='')
        result = session.execute(
            text("SELECT * FROM product_catalog WHERE barcode = :barcode AND store = '' LIMIT 1"),
            {"barcode": barcode}
        )
        row = result.first()
        return dict(row) if row else None


def catalog_upsert(
    barcode: str,
    product_name: str,
    store: Optional[str] = None,
    brand: Optional[str] = None,
    volume_ml: Optional[int] = None,
    quantity: Optional[int] = None,
    updated_by: Optional[str] = None,
):
    """Insert or update a product in the catalog."""
    if not barcode or not product_name:
        return
    store_val = store.lower() if store else ""
    with get_db() as session:
        session.execute(text("""
            INSERT INTO product_catalog (barcode, store, product_name, brand, volume_ml, quantity, updated_by, updated_at)
            VALUES (:barcode, :store, :product_name, :brand, :volume_ml, :quantity, :updated_by, NOW())
            ON CONFLICT(barcode, store) DO UPDATE SET
                product_name = EXCLUDED.product_name,
                brand = COALESCE(EXCLUDED.brand, product_catalog.brand),
                volume_ml = COALESCE(EXCLUDED.volume_ml, product_catalog.volume_ml),
                quantity = COALESCE(EXCLUDED.quantity, product_catalog.quantity),
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
        """), {
            "barcode": barcode,
            "store": store_val,
            "product_name": product_name,
            "brand": brand,
            "volume_ml": volume_ml,
            "quantity": quantity,
            "updated_by": updated_by,
        })


def get_expired_promos() -> List[dict]:
    """Return observations with has_discount=1 and a promo_end_date that has passed."""
    import datetime as _dt
    today = _dt.date.today()
    with get_db() as session:
        result = session.execute(text("""
            SELECT DISTINCT user_id, id, product_raw, store, promo_end_date
            FROM price_observations
            WHERE has_discount = 1 AND promo_end_date IS NOT NULL
        """))
        rows = [dict(r) for r in result.fetchall()]
    expired = []
    for row in rows:
        try:
            end_date = _dt.datetime.strptime(row["promo_end_date"], "%d/%m/%Y").date()
            if end_date < today:
                expired.append(row)
        except ValueError:
            pass
    return expired


def clear_expired_promo(observation_id: int) -> bool:
    """Remove discount flag and promo_end_date from an observation."""
    with get_db() as session:
        session.execute(text("""
            UPDATE price_observations
            SET has_discount = 0, discounted_price = NULL, promo_end_date = NULL
            WHERE id = :id
        """), {"id": observation_id})
    return True


def get_all_products(user_id: str) -> dict:
    """Return all products for a user grouped by store (latest price per product per store)."""
    with get_db() as session:
        result = session.execute(text("""
            SELECT po.store, po.product_raw, po.product_normalized, po.price,
                   po.has_discount, po.original_price, po.discounted_price,
                   po.barcode, po.promo_end_date, po.volume_ml
            FROM price_observations po
            INNER JOIN (
                SELECT store, product_normalized, MAX(created_at) AS latest
                FROM price_observations
                WHERE user_id = :user_id AND price IS NOT NULL AND product_normalized IS NOT NULL
                GROUP BY store, product_normalized
            ) latest ON po.store = latest.store
                    AND po.product_normalized = latest.product_normalized
                    AND po.created_at = latest.latest
            WHERE po.user_id = :user_id
            ORDER BY po.store, po.product_normalized
        """), {"user_id": user_id})
        rows = [dict(row) for row in result.fetchall()]
    grouped: dict = {}
    for row in rows:
        store = row["store"] or "desconocido"
        grouped.setdefault(store, []).append(row)
    return grouped


def catalog_search(product_name: str, limit: int = 10) -> List[dict]:
    """Search the catalog by product name."""
    if not product_name:
        return []
    pattern = f"%{product_name.lower()}%"
    with get_db() as session:
        result = session.execute(
            text("""
                SELECT * FROM product_catalog
                WHERE LOWER(product_name) LIKE :pattern
                ORDER BY updated_at DESC
                LIMIT :limit
            """),
            {"pattern": pattern, "limit": limit}
        )
        return [dict(row) for row in result.fetchall()]


# ═══════════════════════════════════════════════════════════════
# NORMALIZATION
# ═══════════════════════════════════════════════════════════════

def normalize_product_name(name: str) -> str:
    """Normalize product name for better matching."""
    if not name:
        return ""

    normalized = name.lower()

    noise = ["x", "und", "unidades", "ml", "lt", "kg", "g", "gr"]
    words = normalized.split()
    words = [w for w in words if w not in noise and not w.isdigit()]

    normalized = " ".join(words)
    normalized = "".join(c for c in normalized if c.isalnum() or c == " ")

    return normalized.strip()

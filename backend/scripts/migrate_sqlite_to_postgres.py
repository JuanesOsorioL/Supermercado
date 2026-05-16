"""
Script de migración: SQLite → PostgreSQL

Uso:
  python migrate_sqlite_to_postgres.py

Requisitos:
  - Archivo prices.db en el directorio actual
  - PostgreSQL accesible (variables de entorno: DB_HOST, DB_USER, etc.)
  - Tabla de destino ya creada en PostgreSQL

Flujo:
  1. Lee todos los datos de SQLite (prices.db)
  2. Conecta a PostgreSQL
  3. Crea el esquema (tablas + índices)
  4. Migra datos tabla por tabla
  5. Reporte de éxito
"""

import sqlite3
import psycopg2
from psycopg2.extras import execute_values
import os
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime

# Cargar variables de entorno
load_dotenv()

SQLITE_DB = Path("./prices.db")

# Construir connection string de PostgreSQL
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "postgresJuan")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME", "supermercado")

if not DB_PASSWORD:
    print("❌ ERROR: DB_PASSWORD no configurada en .env")
    exit(1)

PG_CONN_STRING = (
    f"postgresql://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)


def log_message(msg: str, status: str = "ℹ️"):
    """Imprimir mensaje con timestamp."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {status} {msg}")


def create_pg_schema(cursor):
    """Crear todas las tablas en PostgreSQL."""
    log_message("Creando esquema en PostgreSQL...", "📋")

    # Tabla: products (catálogo maestro)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            barcode TEXT UNIQUE,
            name TEXT NOT NULL,
            brand TEXT,
            category TEXT,
            volume_ml INTEGER,
            quantity INTEGER,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP
        )
    """)

    # Tabla: product_catalog (catálogo por tienda)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_catalog (
            id SERIAL PRIMARY KEY,
            barcode TEXT NOT NULL,
            store TEXT NOT NULL DEFAULT '',
            product_name TEXT NOT NULL,
            brand TEXT,
            volume_ml INTEGER,
            quantity INTEGER,
            updated_by TEXT,
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(barcode, store)
        )
    """)

    # Tabla: price_observations (observaciones de precios escaneadas)
    cursor.execute("""
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
    """)

    # Tabla: price_history (histórico de precios)
    cursor.execute("""
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
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    # Tabla: scan_sessions (sesiones de escaneo)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scan_sessions (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            store TEXT NOT NULL,
            started_at TEXT DEFAULT NOW(),
            last_activity_at TEXT DEFAULT NOW(),
            is_active INTEGER DEFAULT 1,
            updated_count INTEGER DEFAULT 0
        )
    """)

    # Tabla: user_shopping_lists (listas de compra)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_shopping_lists (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            product_normalized TEXT NOT NULL,
            created_at TEXT DEFAULT NOW(),
            UNIQUE(user_id, product_normalized)
        )
    """)

    log_message("Tablas creadas", "✅")


def create_indexes(cursor):
    """Crear índices para optimizar queries."""
    log_message("Creando índices...", "📊")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_obs_barcode ON price_observations(barcode)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_obs_store ON price_observations(store)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_obs_user ON price_observations(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_obs_product ON price_observations(product_normalized)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_obs_created ON price_observations(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_barcode ON price_history(barcode)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_store ON price_history(store)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON scan_sessions(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_list_user ON user_shopping_lists(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_catalog_barcode ON product_catalog(barcode)")

    log_message("Índices creados", "✅")


def migrate_table(sqlite_cursor, pg_cursor, table_name: str, select_query: str):
    """Migrar una tabla de SQLite a PostgreSQL."""
    log_message(f"Migrando tabla: {table_name}...", "📤")

    # Leer datos de SQLite
    sqlite_cursor.execute(select_query)
    rows = sqlite_cursor.fetchall()

    if not rows:
        log_message(f"  Tabla vacía", "⚠️")
        return 0

    # Obtener nombres de columnas
    columns = [desc[0] for desc in sqlite_cursor.description]

    # Convertir rows a tuplas
    values = [tuple(row[col] for col in columns) for row in rows]

    # Insertar en PostgreSQL
    placeholders = ",".join(["%s"] * len(columns))
    insert_query = f"INSERT INTO {table_name} ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

    try:
        execute_values(pg_cursor, insert_query, values, page_size=1000)
        log_message(f"  {len(rows)} filas migradas", "✅")
        return len(rows)
    except Exception as e:
        log_message(f"  Error: {e}", "❌")
        raise


def main():
    """Función principal."""
    print("\n" + "="*60)
    print("  MIGRACIÓN: SQLite → PostgreSQL")
    print("="*60 + "\n")

    # Verificar que prices.db existe
    if not SQLITE_DB.exists():
        log_message(f"No encontrado: {SQLITE_DB}", "❌")
        exit(1)

    log_message(f"Conectando a SQLite: {SQLITE_DB}", "🔗")
    try:
        sqlite_conn = sqlite3.connect(SQLITE_DB)
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_cursor = sqlite_conn.cursor()
    except Exception as e:
        log_message(f"Error conectando a SQLite: {e}", "❌")
        exit(1)

    log_message(f"Conectando a PostgreSQL: {DB_HOST}:{DB_PORT}/{DB_NAME}", "🔗")
    try:
        pg_conn = psycopg2.connect(PG_CONN_STRING)
        pg_cursor = pg_conn.cursor()
    except Exception as e:
        log_message(f"Error conectando a PostgreSQL: {e}", "❌")
        exit(1)

    try:
        # Crear esquema
        create_pg_schema(pg_cursor)
        pg_conn.commit()

        # Crear índices
        create_indexes(pg_cursor)
        pg_conn.commit()

        # Tablas a migrar (en orden por dependencias)
        tables_to_migrate = [
            ("products", "SELECT * FROM products"),
            ("product_catalog", "SELECT * FROM product_catalog"),
            ("price_observations", "SELECT * FROM price_observations"),
            ("price_history", "SELECT * FROM price_history"),
            ("scan_sessions", "SELECT * FROM scan_sessions"),
            ("user_shopping_lists", "SELECT * FROM user_shopping_lists"),
        ]

        total_rows = 0
        for table_name, query in tables_to_migrate:
            rows = migrate_table(sqlite_cursor, pg_cursor, table_name, query)
            total_rows += rows
            pg_conn.commit()

        print("\n" + "="*60)
        log_message(f"MIGRACIÓN COMPLETADA: {total_rows} filas totales", "✨")
        print("="*60 + "\n")

    except Exception as e:
        log_message(f"Error durante migración: {e}", "❌")
        pg_conn.rollback()
        raise
    finally:
        sqlite_cursor.close()
        sqlite_conn.close()
        pg_cursor.close()
        pg_conn.close()


if __name__ == "__main__":
    main()

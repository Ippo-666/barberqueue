"""
BarberQueue (دورك) - Production SQLite Database Manager
======================================================
Handles data persistence for:
- Salons & Barber Profiles
- Salon Services & Dynamic Pricing
- Live Queue Tickets & Walk-ins
- Anti-Spam Strikes & Grace Periods
- B2C Product Inventory & Upsell Orders
- B2B Wholesale Supplies Orders
- Daily Settlements & Revenue History
"""

import sqlite3
import json
import os
import re
import threading
import decimal
from datetime import datetime, date, time
from typing import Dict, List, Optional, Any

# Load local .env if present
def _load_env_file():
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

_load_env_file()

DB_PATH = os.path.join(os.path.dirname(__file__), "barber_queue.db")

# =====================================================================
# DUAL-ENGINE ADAPTER: PostgreSQL (Production / Cloud) + SQLite (Local)
# =====================================================================

class RowWrapper(dict):
    """
    Drop-in replacement for sqlite3.Row in PostgreSQL.
    Provides dual access:
    1. By column name: row['id'], row['customer_name'] (via RealDictCursor dict keys)
    2. By integer index: row[0], row[1] (via tuple index)
    3. Dict methods: row.get('key'), row.keys(), row.items(), dict(row)
    4. Automatically stringifies datetime/date/time to match SQLite behavior for Jinja2 templates & JSON.
    5. Automatically converts Decimal objects to float.
    """
    def __init__(self, data_dict, tuple_data):
        cleaned_dict = {}
        for k, v in data_dict.items():
            if isinstance(v, datetime):
                cleaned_dict[k] = v.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(v, date):
                cleaned_dict[k] = v.strftime("%Y-%m-%d")
            elif isinstance(v, time):
                cleaned_dict[k] = v.strftime("%H:%M:%S")
            elif isinstance(v, decimal.Decimal):
                cleaned_dict[k] = float(v)
            else:
                cleaned_dict[k] = v
        super().__init__(cleaned_dict)

        cleaned_tuple = []
        for v in tuple_data:
            if isinstance(v, datetime):
                cleaned_tuple.append(v.strftime("%Y-%m-%d %H:%M:%S"))
            elif isinstance(v, date):
                cleaned_tuple.append(v.strftime("%Y-%m-%d"))
            elif isinstance(v, time):
                cleaned_tuple.append(v.strftime("%H:%M:%S"))
            elif isinstance(v, decimal.Decimal):
                cleaned_tuple.append(float(v))
            else:
                cleaned_tuple.append(v)
        self._tuple = tuple(cleaned_tuple)

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._tuple[item]
        return super().__getitem__(item)


TABLES_WITH_ID = {
    'salons', 'salon_chairs', 'services', 'queue_tickets', 'products',
    'wholesale_orders', 'daily_settlements', 'salon_expenses', 'salon_reviews',
    'groom_appointments', 'freelance_barbers', 'home_service_requests',
    'freelancer_wallet_recharges'
}


def sqlite_to_pg_query(query: str) -> str:
    """Translates SQLite dialect (placeholders, conflict clauses, types, DDL) to standard PostgreSQL."""
    q = query.strip()

    # 1. DDL Compatibility: INTEGER PRIMARY KEY AUTOINCREMENT -> SERIAL PRIMARY KEY
    q = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTO_?INCREMENT\b', 'SERIAL PRIMARY KEY', q, flags=re.IGNORECASE)

    # 2. DDL Compatibility: ALTER TABLE ... ADD COLUMN -> ADD COLUMN IF NOT EXISTS
    q = re.sub(r'\bALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?!IF\s+NOT\s+EXISTS\b)', r'ALTER TABLE \1 ADD COLUMN IF NOT EXISTS ', q, flags=re.IGNORECASE)

    # 3. Conflict resolution: INSERT OR IGNORE -> ON CONFLICT DO NOTHING
    if re.search(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', q, re.IGNORECASE):
        q = re.sub(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', q, flags=re.IGNORECASE)
        if 'ON CONFLICT' not in q.upper():
            q = q.rstrip('; ') + ' ON CONFLICT DO NOTHING;'
    # 4. Conflict resolution: INSERT OR REPLACE
    elif 'admin_settings' in q and re.search(r'\bINSERT\s+OR\s+REPLACE\s+INTO\b', q, re.IGNORECASE):
        q = re.sub(r'\bINSERT\s+OR\s+REPLACE\s+INTO\b', 'INSERT INTO', q, flags=re.IGNORECASE)
        if 'ON CONFLICT' not in q.upper():
            q = q.rstrip('; ') + ' ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;'
    elif 'daily_settlements' in q and re.search(r'\bINSERT\s+OR\s+REPLACE\s+INTO\b', q, re.IGNORECASE):
        q = re.sub(r'\bINSERT\s+OR\s+REPLACE\s+INTO\b', 'INSERT INTO', q, flags=re.IGNORECASE)
        if 'ON CONFLICT' not in q.upper():
            q = q.rstrip('; ') + ' ON CONFLICT (id) DO UPDATE SET total_clients = EXCLUDED.total_clients, app_clients = EXCLUDED.app_clients, walkin_clients = EXCLUDED.walkin_clients, gross_services_revenue = EXCLUDED.gross_services_revenue, products_sold_count = EXCLUDED.products_sold_count, products_sold_revenue = EXCLUDED.products_sold_revenue, barber_product_commission = EXCLUDED.barber_product_commission, total_barber_take_home = EXCLUDED.total_barber_take_home, no_show_strikes = EXCLUDED.no_show_strikes;'
    elif 'services' in q and re.search(r'\bINSERT\s+OR\s+REPLACE\s+INTO\b', q, re.IGNORECASE):
        q = re.sub(r'\bINSERT\s+OR\s+REPLACE\s+INTO\b', 'INSERT INTO', q, flags=re.IGNORECASE)
        if 'ON CONFLICT' not in q.upper():
            q = q.rstrip('; ') + ' ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, price = EXCLUDED.price, duration_minutes = EXCLUDED.duration_minutes, icon = EXCLUDED.icon, is_active = EXCLUDED.is_active;'
    elif re.search(r'\bINSERT\s+OR\s+REPLACE\s+INTO\b', q, re.IGNORECASE):
        q = re.sub(r'\bINSERT\s+OR\s+REPLACE\s+INTO\b', 'INSERT INTO', q, flags=re.IGNORECASE)
        if 'ON CONFLICT' not in q.upper():
            q = q.rstrip('; ') + ' ON CONFLICT (id) DO NOTHING;'

    # 5. Replace ? with %s strictly outside single and double quotes
    parts = []
    in_single_q = False
    in_double_q = False
    i = 0
    n = len(q)
    while i < n:
        ch = q[i]
        if ch == "'" and not in_double_q:
            if in_single_q and i + 1 < n and q[i + 1] == "'":
                parts.append("''")
                i += 2
                continue
            in_single_q = not in_single_q
            parts.append(ch)
        elif ch == '"' and not in_single_q:
            in_double_q = not in_double_q
            parts.append(ch)
        elif ch == '?' and not in_single_q and not in_double_q:
            parts.append('%s')
        else:
            parts.append(ch)
        i += 1
    return ''.join(parts)


class PostgresCursorWrapper:
    def __init__(self, cursor, raw_conn=None):
        self._cursor = cursor
        self._conn = raw_conn
        self._lastrowid = None

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def execute(self, query, params=None):
        q = query.strip()

        # Intercept SQLite PRAGMA table_info for PostgreSQL
        if q.upper().startswith("PRAGMA"):
            m = re.match(r'PRAGMA\s+table_info\((\w+)\)', q, re.IGNORECASE)
            if m:
                table_name = m.group(1).lower()
                pg_info_query = """
                SELECT 
                    ordinal_position - 1 AS cid, 
                    column_name AS name, 
                    data_type AS type, 
                    CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull, 
                    column_default AS dflt_value, 
                    0 AS pk 
                FROM information_schema.columns 
                WHERE LOWER(table_name) = %s 
                ORDER BY ordinal_position;
                """
                self._cursor.execute(pg_info_query, (table_name,))
                return self
            # Other pragmas (journal_mode, busy_timeout) are safe no-ops in PostgreSQL
            return self

        pg_query = sqlite_to_pg_query(query)
        self._lastrowid = None

        # Check if this is an INSERT statement
        is_insert = bool(re.match(r'^\s*INSERT\s+INTO\s+', pg_query, re.IGNORECASE))
        has_returning = 'RETURNING' in pg_query.upper()

        if is_insert:
            m_tbl = re.search(r'INSERT\s+INTO\s+(\w+)', pg_query, re.IGNORECASE)
            tbl_name = m_tbl.group(1).lower() if m_tbl else ''

            m_cols = re.search(r'\((.*?)\)\s*VALUES', pg_query, re.IGNORECASE | re.DOTALL)
            cols = [c.strip().strip('"').lower() for c in m_cols.group(1).split(',')] if m_cols else []

            # Capture explicitly supplied ID from params if present
            if cols and params:
                for target_col in ('id', 'phone', 'key'):
                    if target_col in cols:
                        idx = cols.index(target_col)
                        if idx < len(params):
                            self._lastrowid = params[idx]
                        break

            # If table has an id column and no explicit id was provided, append RETURNING id
            if not has_returning and tbl_name in TABLES_WITH_ID and 'id' not in cols:
                pg_query = pg_query.rstrip('; ') + ' RETURNING id;'
                has_returning = True

        # Execute on PostgreSQL
        if params is not None:
            if isinstance(params, list):
                params = tuple(params)
            self._cursor.execute(pg_query, params)
        else:
            self._cursor.execute(pg_query)

        # If RETURNING id was appended, fetch and populate self._lastrowid
        if is_insert and has_returning and self._lastrowid is None:
            try:
                ret = self._cursor.fetchone()
                if ret:
                    self._lastrowid = ret['id'] if isinstance(ret, dict) else ret[0]
            except Exception:
                pass

        return self

    def executemany(self, query, seq_of_params):
        pg_query = sqlite_to_pg_query(query)
        self._cursor.executemany(pg_query, seq_of_params)
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        if not row:
            return None
        col_names = [desc[0] for desc in self._cursor.description]
        tuple_data = tuple(row[col] for col in col_names)
        return RowWrapper(dict(row), tuple_data)

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not rows:
            return []
        col_names = [desc[0] for desc in self._cursor.description]
        return [RowWrapper(dict(r), tuple(r[col] for col in col_names)) for r in rows]

    def close(self):
        self._cursor.close()


class PostgresConnectionWrapper:
    def __init__(self, raw_conn, pool=None):
        self._conn = raw_conn
        self._pool = pool

    def cursor(self):
        return PostgresCursorWrapper(self._conn.cursor(), self._conn)

    def execute(self, query, params=None):
        cur = self.cursor()
        cur.execute(query, params)
        return cur

    def executemany(self, query, seq_of_params):
        cur = self.cursor()
        cur.executemany(query, seq_of_params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._conn is not None:
            try:
                if not self._conn.closed:
                    self._conn.rollback()
            except Exception:
                pass
        if self._pool is not None:
            try:
                self._pool.putconn(self._conn)
                return
            except Exception:
                pass
        try:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                self.rollback()
            else:
                self.commit()
        finally:
            self.close()


def insert_and_get_id(conn, query: str, params=None):
    """Uniform helper to execute INSERT and return the inserted row id across both SQLite and PostgreSQL."""
    cur = conn.execute(query, params)
    return cur.lastrowid


_pg_pool = None
_pg_pool_lock = threading.Lock()


def get_pg_pool(db_url, conn_args):
    global _pg_pool
    if _pg_pool is None or _pg_pool.closed:
        with _pg_pool_lock:
            if _pg_pool is None or _pg_pool.closed:
                from psycopg2.pool import ThreadedConnectionPool
                _pg_pool = ThreadedConnectionPool(minconn=1, maxconn=16, dsn=db_url, **conn_args)
    return _pg_pool


def get_db_connection():
    # Enforce SQLite in test mode to guarantee Neon production database is NEVER touched
    if os.environ.get("IS_TESTING") == "1" or os.environ.get("USE_SQLITE_TESTING") == "1":
        db_url = None
    else:
        db_url = os.environ.get("DATABASE_URL")
    if db_url:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor

            # Normalize postgres:// to postgresql://
            if db_url.startswith("postgres://"):
                db_url = "postgresql://" + db_url[len("postgres://"):]

            # Enforce sslmode=require for cloud PostgreSQL (Neon / Supabase)
            if "sslmode=" not in db_url:
                delimiter = "&" if "?" in db_url else "?"
                db_url = f"{db_url}{delimiter}sslmode=require"

            # Connect with TCP keepalive parameters to prevent idle socket drops
            conn_args = {
                "cursor_factory": RealDictCursor,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
                "connect_timeout": 5
            }

            # If the database URL is already pointing to Neon's cloud pooler (-pooler),
            # connect directly through Neon's cloud PgBouncer for optimal latency without client pool deadlock
            if "-pooler" in db_url:
                raw_conn = psycopg2.connect(db_url, **conn_args)
                return PostgresConnectionWrapper(raw_conn)

            # Otherwise, use a safe ThreadedConnectionPool
            pool = get_pg_pool(db_url, conn_args)
            raw_conn = None
            try:
                raw_conn = pool.getconn()
                if raw_conn.closed:
                    raise psycopg2.OperationalError("Connection is closed")
            except Exception:
                if raw_conn is not None:
                    try:
                        pool.putconn(raw_conn, close=True)
                    except Exception:
                        pass
                raw_conn = psycopg2.connect(db_url, **conn_args)
                return PostgresConnectionWrapper(raw_conn)

            if raw_conn is not None:
                return PostgresConnectionWrapper(raw_conn, pool=pool)

        except Exception as e:
            print(f"[Database Adapter] Warning: PostgreSQL connection failed ({e}), falling back to SQLite.")
            try:
                import psycopg2
                from psycopg2.extras import RealDictCursor
                conn_args = {
                    "cursor_factory": RealDictCursor,
                    "keepalives": 1,
                    "keepalives_idle": 30,
                    "keepalives_interval": 10,
                    "keepalives_count": 5,
                    "connect_timeout": 5
                }
                raw_conn = psycopg2.connect(db_url, **conn_args)
                return PostgresConnectionWrapper(raw_conn)
            except Exception as direct_e:
                print(f"[Database Adapter] Fallback to SQLite: {direct_e}")

    # SQLite Local Fallback
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def init_db():
    """Initializes all database tables and seeds default Algerian barbershop data."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Salons Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS salons (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        barber_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        wilaya TEXT NOT NULL,
        commune TEXT NOT NULL,
        status TEXT DEFAULT 'open', -- 'open' or 'closed'
        manual_crowd_override TEXT DEFAULT 'auto', -- 'auto', 'low', 'medium', 'high'
        baridimob_rip TEXT DEFAULT '00799999000123456789',
        happy_hour_active INTEGER DEFAULT 0,
        happy_hour_discount REAL DEFAULT 100.0,
        happy_hour_note TEXT DEFAULT 'تخفيض الساعات الهادئة (100 د.ج)',
        enable_tv_mode INTEGER DEFAULT 0,
        enable_voice_caller INTEGER DEFAULT 1,
        enable_groom_packs INTEGER DEFAULT 1,
        enable_receipt_print INTEGER DEFAULT 1,
        subscription_status TEXT DEFAULT 'active', -- 'active' or 'suspended'
        subscription_expires_at TEXT,
        subscription_price REAL DEFAULT 3000.0,
        current_in_chair_id TEXT,
        rating REAL DEFAULT 4.9,
        reviews_count INTEGER DEFAULT 50,
        amenities TEXT,
        maps_query TEXT DEFAULT 'Algeria',
        is_eid_mode INTEGER DEFAULT 0,
        active_chair_id INTEGER DEFAULT 1,
        chair_2_in_chair_id TEXT,
        pin_code TEXT DEFAULT '1234',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 1.5 Salon Chairs Table (Multi-chair & Performance tracking)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS salon_chairs (
        id TEXT PRIMARY KEY,
        salon_id TEXT NOT NULL,
        chair_number INTEGER NOT NULL,
        barber_name TEXT NOT NULL,
        commission_rate REAL DEFAULT 50.0,
        current_ticket_id TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (salon_id) REFERENCES salons(id)
    )
    """)

    # 2. Services Catalog Table (with Dynamic Pricing)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS services (
        id TEXT PRIMARY KEY,
        salon_id TEXT NOT NULL,
        name TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL,
        price REAL NOT NULL,
        icon TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        FOREIGN KEY (salon_id) REFERENCES salons(id)
    )
    """)

    # 3. Queue Tickets Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS queue_tickets (
        id TEXT PRIMARY KEY,
        salon_id TEXT NOT NULL,
        customer_name TEXT NOT NULL,
        customer_phone TEXT NOT NULL,
        services_json TEXT NOT NULL, -- JSON list of services selected
        total_duration INTEGER NOT NULL,
        total_service_price REAL NOT NULL,
        status TEXT NOT NULL, -- 'waiting', 'in_chair', 'completed', 'on_the_way', 'traffic_grace', 'no_show', 'cancelled'
        is_walkin INTEGER DEFAULT 0,
        traffic_grace_minutes INTEGER DEFAULT 0,
        grace_expires_at TIMESTAMP,
        estimated_arrival_minutes INTEGER,
        ordered_products_json TEXT, -- JSON list of products added
        preferred_chair_id INTEGER DEFAULT 0,
        preferred_barber_name TEXT DEFAULT 'أي حلاق متاح',
        hair_style_note TEXT DEFAULT '',
        payment_method TEXT DEFAULT 'cash',
        discount_applied REAL DEFAULT 0.0,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        FOREIGN KEY (salon_id) REFERENCES salons(id)
    )
    """)

    # 4. Anti-Spam Customer Strikes Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS customer_strikes (
        phone TEXT PRIMARY KEY,
        customer_name TEXT NOT NULL,
        strike_count INTEGER DEFAULT 0,
        is_blocked INTEGER DEFAULT 0,
        last_strike_at TIMESTAMP
    )
    """)

    # 5. Products Catalog (B2C Hero Products + B2B Barber Supplies)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL, -- 'b2c_hero' or 'b2b_supply'
        unit_spec TEXT,
        price REAL NOT NULL, -- retail price for B2C, wholesale price for B2B
        cost REAL NOT NULL, -- COGS / import cost
        commission_or_profit REAL NOT NULL, -- barber commission for B2C, platform profit for B2B
        stock_count INTEGER DEFAULT 10,
        icon TEXT NOT NULL,
        benefit TEXT NOT NULL,
        recommended_for_service TEXT
    )
    """)

    # 6. Wholesale Supplies Orders Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS wholesale_orders (
        id TEXT PRIMARY KEY,
        salon_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        item_name TEXT NOT NULL,
        unit TEXT NOT NULL,
        wholesale_price REAL NOT NULL,
        platform_profit REAL NOT NULL,
        status TEXT DEFAULT 'pending', -- 'pending', 'dispatched', 'delivered'
        ordered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (salon_id) REFERENCES salons(id)
    )
    """)

    # 7. Daily Settlements Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_settlements (
        id TEXT PRIMARY KEY,
        salon_id TEXT NOT NULL,
        settlement_date DATE NOT NULL,
        total_clients INTEGER NOT NULL,
        app_clients INTEGER NOT NULL,
        walkin_clients INTEGER NOT NULL,
        gross_services_revenue REAL NOT NULL,
        products_sold_count INTEGER NOT NULL,
        products_sold_revenue REAL NOT NULL,
        barber_product_commission REAL NOT NULL,
        total_barber_take_home REAL NOT NULL,
        no_show_strikes INTEGER NOT NULL,
        closed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (salon_id) REFERENCES salons(id)
    )
    """)

    # 8. Customer Loyalty & Rewards Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS customer_loyalty (
        phone TEXT PRIMARY KEY,
        customer_name TEXT,
        visits_count INTEGER DEFAULT 0,
        free_rewards_claimed INTEGER DEFAULT 0,
        last_visit_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 9. Customer Hair Style Profiles Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS customer_hair_profiles (
        phone TEXT PRIMARY KEY,
        customer_name TEXT,
        favorite_cut TEXT,
        notes TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 10. Daily Salon Expenses (Sorties de Caisse) Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS salon_expenses (
        id TEXT PRIMARY KEY,
        salon_id TEXT NOT NULL,
        amount REAL NOT NULL,
        category TEXT NOT NULL,
        description TEXT,
        recorded_by TEXT DEFAULT 'المعلم',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (salon_id) REFERENCES salons(id)
    )
    """)

    # 11. Salon Reviews & Secret Complaints (تقييمات الزبائن وصندوق الشكاوى السري)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS salon_reviews (
        id TEXT PRIMARY KEY,
        salon_id TEXT NOT NULL,
        ticket_id TEXT,
        customer_name TEXT,
        customer_phone TEXT,
        rating INTEGER NOT NULL,
        comment TEXT,
        is_secret INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (salon_id) REFERENCES salons(id)
    )
    """)

    # 12. Groom VIP Appointments (باقات العرسان والمناسبات)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS groom_appointments (
        id TEXT PRIMARY KEY,
        salon_id TEXT NOT NULL,
        customer_name TEXT NOT NULL,
        customer_phone TEXT NOT NULL,
        pack_name TEXT NOT NULL,
        price REAL NOT NULL,
        event_date TEXT NOT NULL,
        event_time TEXT NOT NULL,
        acompte_paid REAL DEFAULT 0.0,
        notes TEXT,
        status TEXT DEFAULT 'confirmed',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (salon_id) REFERENCES salons(id)
    )
    """)

    # 13. Admin Master Settings
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS admin_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('master_pin', '1226')")
    cursor.execute("UPDATE admin_settings SET value = '1226' WHERE key = 'master_pin' AND value = '2026'")
    cursor.execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('admin_phone', '0555 12 34 56')")
    cursor.execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('admin_rip', '00799999000123456789')")

    # 14. Freelance Barbers Table (الحلاقون الأحرار / المتنقلون للخدمات المنزلية)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS freelance_barbers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        wilaya TEXT NOT NULL,
        commune TEXT NOT NULL,
        working_zones TEXT NOT NULL,
        vehicle_type TEXT DEFAULT 'moto',
        bio TEXT,
        rating REAL DEFAULT 4.9,
        reviews_count INTEGER DEFAULT 18,
        completed_jobs INTEGER DEFAULT 25,
        is_online INTEGER DEFAULT 1,
        is_verified INTEGER DEFAULT 1,
        base_price REAL DEFAULT 1200.0,
        services_offered_json TEXT,
        baridimob_rip TEXT DEFAULT '00799999000123456789',
        wallet_balance REAL DEFAULT 2000.0,
        avatar_url TEXT DEFAULT '',
        pin_code TEXT DEFAULT '1234',
        id_card_number TEXT DEFAULT '',
        id_card_front TEXT DEFAULT '',
        id_card_back TEXT DEFAULT '',
        selfie_photo TEXT DEFAULT '',
        equipment_photo TEXT DEFAULT '',
        kyc_status TEXT DEFAULT 'approved', -- 'unsubmitted', 'pending', 'approved', 'rejected'
        kyc_rejection_reason TEXT DEFAULT '',
        kyc_submitted_at TIMESTAMP,
        kyc_reviewed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 15. Home Service Requests Table (طلبات الحلاقة المنزلية المباشرة)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS home_service_requests (
        id TEXT PRIMARY KEY,
        customer_name TEXT NOT NULL,
        customer_phone TEXT NOT NULL,
        wilaya TEXT NOT NULL,
        commune TEXT NOT NULL,
        address_details TEXT NOT NULL,
        service_type TEXT NOT NULL,
        price REAL NOT NULL,
        barber_id TEXT,
        status TEXT DEFAULT 'pending',
        scheduled_at TEXT DEFAULT 'الآن (فوري)',
        notes TEXT DEFAULT '',
        payment_method TEXT DEFAULT 'cash',
        customer_rating INTEGER DEFAULT 0,
        customer_review TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (barber_id) REFERENCES freelance_barbers(id)
    )
    """)

    # 16. Freelancer Wallet Recharges Table (شحن رصيد الحلاق ببريدي موب)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS freelancer_wallet_recharges (
        id TEXT PRIMARY KEY,
        barber_id TEXT NOT NULL,
        amount REAL NOT NULL,
        transaction_ref TEXT NOT NULL,
        notes TEXT,
        receipt_image TEXT DEFAULT '',
        status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        approved_at TIMESTAMP,
        FOREIGN KEY (barber_id) REFERENCES freelance_barbers(id)
    )
    """)

    # Automatic Migrations: Ensure all security, KYC & operations columns exist in existing databases
    cursor.execute("PRAGMA table_info(freelance_barbers)")
    fb_cols = [r[1] for r in cursor.fetchall()]
    fb_new_cols = [
        ("wallet_balance", "REAL DEFAULT 2000.0"),
        ("pin_code", "TEXT DEFAULT '1234'"),
        ("id_card_number", "TEXT DEFAULT ''"),
        ("id_card_front", "TEXT DEFAULT ''"),
        ("id_card_back", "TEXT DEFAULT ''"),
        ("selfie_photo", "TEXT DEFAULT ''"),
        ("equipment_photo", "TEXT DEFAULT ''"),
        ("kyc_status", "TEXT DEFAULT 'approved'"),
        ("kyc_rejection_reason", "TEXT DEFAULT ''"),
        ("kyc_submitted_at", "TIMESTAMP"),
        ("kyc_reviewed_at", "TIMESTAMP"),
    ]
    for col_name, col_type in fb_new_cols:
        if col_name not in fb_cols:
            cursor.execute(f"ALTER TABLE freelance_barbers ADD COLUMN {col_name} {col_type}")

    cursor.execute("PRAGMA table_info(freelancer_wallet_recharges)")
    rech_cols = [r[1] for r in cursor.fetchall()]
    if "receipt_image" not in rech_cols:
        cursor.execute("ALTER TABLE freelancer_wallet_recharges ADD COLUMN receipt_image TEXT DEFAULT ''")

    cursor.execute("PRAGMA table_info(salons)")
    salon_cols = [r[1] for r in cursor.fetchall()]
    if "pin_code" not in salon_cols:
        cursor.execute("ALTER TABLE salons ADD COLUMN pin_code TEXT DEFAULT '1234'")
    if "is_eid_mode" not in salon_cols:
        cursor.execute("ALTER TABLE salons ADD COLUMN is_eid_mode INTEGER DEFAULT 0")
    if "active_chair_id" not in salon_cols:
        cursor.execute("ALTER TABLE salons ADD COLUMN active_chair_id INTEGER DEFAULT 1")
    if "chair_2_in_chair_id" not in salon_cols:
        cursor.execute("ALTER TABLE salons ADD COLUMN chair_2_in_chair_id TEXT")

    # Ensure barber_free_1 exists for instant testing
    cursor.execute("SELECT id FROM freelance_barbers WHERE id = 'barber_free_1'")
    if not cursor.fetchone():
        cursor.execute("""
        INSERT OR IGNORE INTO freelance_barbers 
        (id, name, phone, wilaya, commune, working_zones, vehicle_type, bio, rating, reviews_count, completed_jobs, is_online, is_verified, base_price, baridimob_rip, wallet_balance, pin_code, kyc_status, id_card_number)
        VALUES ('barber_free_1', 'كريم الحلاق المتنقل 🛵', '0555001122', 'الجزائر العاصمة (16)', 'الشراقة', 'الشراقة، دالي ابراهيم، درارية، أولاد فايت', 'moto', 'حلاق محترف معتمد بالهوية الوطنية البيومترية، خبرة 6 سنوات وحقيبة معقمة.', 4.95, 28, 64, 1, 1, 1200.0, '00799999000123456789', 2000.0, '1234', 'approved', '118822334455001')
        """)

    conn.commit()

    # Seed Default Data if empty
    cursor.execute("SELECT COUNT(*) FROM salons")
    if cursor.fetchone()[0] == 0:
        _seed_default_data(cursor)

    conn.commit()
    conn.close()


def _seed_default_data(cursor: sqlite3.Cursor):
    # 1. Default Salon
    salon_id = "salon_dz_1"
    cursor.execute("""
    INSERT INTO salons (id, name, barber_name, phone, wilaya, commune, status)
    VALUES (?, ?, ?, ?, ?, ?, 'open')
    """, (
        salon_id,
        "صالون الأناقة الملكي (Chez Rachid)",
        "المعلم رشيد",
        "0555123456",
        "الجزائر العاصمة (16)",
        "باب الزوار (Bab Ezzouar)"
    ))

    # 2. Default Services
    services = [
        ("srv_haircut", salon_id, "قص شعر كلاسيكي / مدرج (Fade)", 25, 500.0, "✂️"),
        ("srv_beard", salon_id, "تحديد لحية ملكي بالمنشفة الساخنة", 15, 400.0, "🧔"),
        ("srv_combo", salon_id, "باقة VIP (شعر + لحية + غسيل)", 40, 900.0, "👑"),
        ("srv_facial", salon_id, "ماسك طين البحر الميت وتنظيف بشرة", 20, 600.0, "🧖‍♂️"),
        ("srv_keratin", salon_id, "كيراتين وبروتين لتمليس الشعر", 60, 3500.0, "✨"),
    ]
    cursor.executemany("""
    INSERT INTO services (id, salon_id, name, duration_minutes, price, icon)
    VALUES (?, ?, ?, ?, ?, ?)
    """, services)

    # 3. Default B2C Products
    b2c_products = [
        ("prod_wax", "واكس التصفيف المطفي (RedOne/Bandido)", "b2c_hero", "علبة 150 مل", 1000.0, 380.0, 180.0, 8, "🏺", "ثبات فائق ومظهر طبيعي يدوم طوال اليوم بدون قشرة", "srv_haircut"),
        ("prod_oil", "زيت اللحية العضوي الفاخر (Beard Oil)", "b2c_hero", "قارورة 50 مل", 1200.0, 400.0, 200.0, 5, "🌿", "زيوت أرجان وجوجوبا لترطيب اللحية وتنعيمها ومنع الحكة", "srv_beard"),
        ("prod_powder", "بودرة تكثيف الشعر (Nishman Volume Powder)", "b2c_hero", "عبوة 20 جم", 1200.0, 420.0, 200.0, 6, "✨", "كثافة فورية ورفع للجذور بدون لمعان لتسريحات الكروب والفييد", "srv_combo"),
        ("prod_serum", "شامبو وسيروم ما بعد الكيراتين (Sans Sulfate)", "b2c_hero", "عبوة 250 مل", 1500.0, 550.0, 250.0, 4, "🧴", "خالي تماماً من الأملاح للحفاظ على لمعان ونعومة الكيراتين", "srv_keratin"),
        ("prod_cologne", "كولونيا ما بعد الحلاقة المعقمة (Aftershave)", "b2c_hero", "قارورة 400 مل", 600.0, 220.0, 100.0, 12, "💈", "تعقيم مسام البشرة وتهدئة الاحمرار مع انتعاش يدوم لساعات", "srv_beard"),
    ]
    cursor.executemany("""
    INSERT INTO products (id, name, type, unit_spec, price, cost, commission_or_profit, stock_count, icon, benefit, recommended_for_service)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, b2c_products)

    # 4. Default B2B Wholesale Supplies
    b2b_supplies = [
        ("b2b_shampoo_5l", "بيدون شامبو الصالونات الاحترافي", "b2b_supply", "5 لتر", 1800.0, 1300.0, 500.0, 20, "🧴", "يكفي 180 إلى 200 غسلة رأس (تكلفة الغسلة 9 د.ج فقط)", None),
        ("b2b_gel_1l", "جل الحلاقة الشفاف للكونتور وتحديد اللحية", "b2b_supply", "1000 مل", 650.0, 450.0, 200.0, 30, "💧", "شفافية كاملة لرؤية مليمترية أثناء تحديد خطوط الموس", None),
        ("b2b_keratin_1l", "كيراتين / بروتين برازيلي احترافي (Sans Formol)", "b2b_supply", "1 لتر", 6800.0, 5200.0, 1600.0, 10, "👑", "يكفي 20 جلسة تمليس شعر (مدخول الجلسات يفوق 60,000 د.ج)", None),
        ("b2b_blades", "باك شفرات لورد بلاتينيوم (100 شفرة) + كحول 70°", "b2b_supply", "100 شفرة + 1 لتر", 1400.0, 1000.0, 400.0, 25, "✂️", "شفرات حادة معقمة مع لتر كحول طبي لحماية الزبائن", None),
        ("b2b_wax_carton", "كرتون واكس تصفيف 12 علبة (RedOne / Bandido)", "b2b_supply", "12 علبة × 150 مل", 4800.0, 3800.0, 1000.0, 15, "📦", "سعر العلبة 400 د.ج فقط لإعادة بيعها للزبائن بـ 1000 د.ج (+7200 د.ج ربح)", None),
    ]
    cursor.executemany("""
    INSERT INTO products (id, name, type, unit_spec, price, cost, commission_or_profit, stock_count, icon, benefit, recommended_for_service)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, b2b_supplies)

    # 5. Seed some initial queue clients for realistic demonstration
    sample_tickets = [
        ("ticket_101", salon_id, "ياسين بن علي", "0550112233", json.dumps([{"id": "srv_combo", "name": "باقة VIP (شعر + لحية + غسيل)", "price": 900.0, "duration": 40}]), 40, 900.0, "in_chair", 0, None),
        ("ticket_102", salon_id, "أمين قادري", "0661223344", json.dumps([{"id": "srv_haircut", "name": "قص شعر كلاسيكي / مدرج", "price": 500.0, "duration": 25}]), 25, 500.0, "waiting", 0, json.dumps([{"id": "prod_powder", "name": "بودرة تكثيف الشعر", "price": 1200.0}])),
        ("ticket_103", salon_id, "كريم بوزيد", "0770334455", json.dumps([{"id": "srv_beard", "name": "تحديد لحية ملكي", "price": 400.0, "duration": 15}]), 15, 400.0, "waiting", 0, None),
    ]
    cursor.executemany("""
    INSERT INTO queue_tickets (id, salon_id, customer_name, customer_phone, services_json, total_duration, total_service_price, status, is_walkin, ordered_products_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, sample_tickets)

    cursor.execute("UPDATE salons SET current_in_chair_id = 'ticket_101' WHERE id = ?", (salon_id,))

    # 6. Default Freelance Barbers & Sample Home Requests
    services_amine = json.dumps([
        {"id": "fsrv_1", "name": "حلاقة شعر عصرية (Fade / التدرج)", "price": 800, "duration": 30},
        {"id": "fsrv_2", "name": "تحديد لحية ملكي بالمنشفة الساخنة", "price": 500, "duration": 20},
        {"id": "fsrv_3", "name": "باقة كاملة VIP (شعر + لحية + تنظيف بشرة)", "price": 1500, "duration": 50},
        {"id": "fsrv_4", "name": "حلاقة أطفال في المنزل", "price": 600, "duration": 25}
    ], ensure_ascii=False)

    services_sofiane = json.dumps([
        {"id": "fsrv_s1", "name": "حلاقة VIP للشخصيات والعرسان", "price": 2500, "duration": 60},
        {"id": "fsrv_s2", "name": "قص شعر وتصفيف كلاسيكي راقي", "price": 1200, "duration": 35},
        {"id": "fsrv_s3", "name": "عناية كاملة باللحية والزيوت الطبيعية", "price": 800, "duration": 25}
    ], ensure_ascii=False)

    cursor.execute("""
    INSERT OR IGNORE INTO freelance_barbers 
    (id, name, phone, wilaya, commune, working_zones, vehicle_type, bio, rating, reviews_count, completed_jobs, is_online, is_verified, base_price, services_offered_json, baridimob_rip)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        'barber_free_amine',
        'أمين الحلاق المتنقل 🛵',
        '0550112233',
        'الجزائر العاصمة (16)',
        'باب الزوار',
        'باب الزوار، الدار البيضاء، برج الكيفان، المحمدية، الحراش',
        'moto',
        'حلاق محترف خبرة 7 سنوات، عتاد معقم 100% داخل حقيبة مجهزة، أتنقل بدراجة نارية سريعة لتوفير الوقت.',
        4.96,
        34,
        82,
        1,
        1,
        1200.0,
        services_amine,
        '00799999000123456789'
    ))

    cursor.execute("""
    INSERT OR IGNORE INTO freelance_barbers 
    (id, name, phone, wilaya, commune, working_zones, vehicle_type, bio, rating, reviews_count, completed_jobs, is_online, is_verified, base_price, services_offered_json, baridimob_rip)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        'barber_free_sofiane',
        'سفيان لوك برستيج (VIP Grooming) 🚗',
        '0555998877',
        'الجزائر العاصمة (16)',
        'درارية',
        'درارية، العاشور، دالي ابراهيم، الأبيار، حيدرة، الشراقة',
        'car',
        'متخصص في حلاقة العرسان والخدمات المنزلية الفاخرة للرجال ورجال الأعمال. حقيبة عناية متكاملة VIP.',
        4.98,
        48,
        115,
        1,
        1,
        2000.0,
        services_sofiane,
        '00799999000987654321'
    ))

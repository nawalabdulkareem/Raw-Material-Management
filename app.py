#!/usr/bin/env python3
"""
Manufacturing Management System V2 - Revised Based on Requirements
"""

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import sqlite3
from datetime import datetime
import json
import traceback
import os

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = 'manufacturing-secret-key-change-this'

DB_NAME = "manufacturing.db"

def get_db():
    """Get database connection"""
    # Increase timeout and allow cross-thread connections to reduce 'database is locked' errors
    conn = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def create_rm_lot(conn, ingredient_name, qty, unit_cost, received_date, reference_type=None, reference_id=None, notes=''):
    c = conn.cursor()
    c.execute("INSERT INTO rm_lots (ingredient_name, qty_remaining, unit_cost, received_date, reference_type, reference_id, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (ingredient_name, qty, unit_cost, received_date, reference_type, reference_id, notes))
    return c.lastrowid


def consume_rm_fifo(conn, ingredient_name, qty_needed, date, reference_type, reference_id, notes=''):
    """Consume raw material using FIFO lots. Returns list of allocations and may create rm_stock_movements OUT per allocation.
    allocations: [{lot_id, qty, unit_cost}]
    """
    c = conn.cursor()
    remaining = float(qty_needed)
    allocations = []
    # select lots in FIFO order
    c.execute("SELECT id, qty_remaining, unit_cost FROM rm_lots WHERE ingredient_name = ? AND qty_remaining > 0 ORDER BY received_date ASC, id ASC", (ingredient_name,))
    for lot in c.fetchall():
        if remaining <= 0:
            break
        lot_id = lot['id']
        lot_qty = float(lot['qty_remaining'] or 0)
        if lot_qty <= 0:
            continue
        use = min(lot_qty, remaining)
        # update lot
        c.execute("UPDATE rm_lots SET qty_remaining = qty_remaining - ? WHERE id = ?", (use, lot_id))
        # update ingredient master qty
        c.execute("UPDATE ingredients SET qty_kg = qty_kg - ? WHERE name = ? COLLATE NOCASE", (use, ingredient_name))
        # insert movement with unit_cost
        c.execute("INSERT INTO rm_stock_movements (date, ingredient_name, movement_type, quantity_kg, unit_cost, reference_type, reference_id, notes) VALUES (?, ?, 'OUT', ?, ?, ?, ?, ?)",
                  (date, ingredient_name, use, lot['unit_cost'], reference_type, reference_id, notes or f'Consumed from lot {lot_id}'))
        allocations.append({'lot_id': lot_id, 'qty': use, 'unit_cost': lot['unit_cost']})
        remaining -= use

    if remaining > 0:
        # Not enough in lots; consume remaining from master inventory and record movement with unit_cost as current ingredient cost
        # (this covers legacy data where lots were not created)
        c.execute("SELECT cost_per_kg FROM ingredients WHERE name = ? COLLATE NOCASE", (ingredient_name,))
        row = c.fetchone()
        unit_cost = row['cost_per_kg'] if row else 0
        # Reduce master qty
        c.execute("UPDATE ingredients SET qty_kg = qty_kg - ? WHERE name = ? COLLATE NOCASE", (remaining, ingredient_name))
        c.execute("INSERT INTO rm_stock_movements (date, ingredient_name, movement_type, quantity_kg, unit_cost, reference_type, reference_id, notes) VALUES (?, ?, 'OUT', ?, ?, ?, ?, ?)",
                  (date, ingredient_name, remaining, unit_cost, reference_type, reference_id, notes or 'Consumed (no lot)'),)
        allocations.append({'lot_id': None, 'qty': remaining, 'unit_cost': unit_cost})
        remaining = 0

    return allocations


def create_packaging_lot(conn, component_name, qty, unit_cost, received_date, reference_type=None, reference_id=None, notes=''):
    c = conn.cursor()
    c.execute("INSERT INTO packaging_lots (component_name, qty_remaining, unit_cost, received_date, reference_type, reference_id, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (component_name, qty, unit_cost, received_date, reference_type, reference_id, notes))
    return c.lastrowid


def consume_packaging_fifo(conn, component_name, qty_needed, date, reference_type, reference_id, notes=''):
    c = conn.cursor()
    remaining = float(qty_needed)
    allocations = []
    c.execute("SELECT id, qty_remaining, unit_cost FROM packaging_lots WHERE component_name = ? AND qty_remaining > 0 ORDER BY received_date ASC, id ASC", (component_name,))
    for lot in c.fetchall():
        if remaining <= 0:
            break
        lot_id = lot['id']
        lot_qty = float(lot['qty_remaining'] or 0)
        if lot_qty <= 0:
            continue
        use = min(lot_qty, remaining)
        c.execute("UPDATE packaging_lots SET qty_remaining = qty_remaining - ? WHERE id = ?", (use, lot_id))
        c.execute("UPDATE packaging_components SET current_qty = current_qty - ? WHERE component_name = ? COLLATE NOCASE", (use, component_name))
        c.execute("INSERT INTO packaging_movements (date, component_name, movement_type, quantity, unit_cost, lot_id, reference_type, reference_id, notes) VALUES (?, ?, 'OUT', ?, ?, ?, ?, ?, ?)",
                  (date, component_name, use, lot['unit_cost'], lot_id, reference_type, reference_id, notes or f'Used from lot {lot_id}'))
        allocations.append({'lot_id': lot_id, 'qty': use, 'unit_cost': lot['unit_cost']})
        remaining -= use

    if remaining > 0:
        # consume remaining from master (no lot)
        c.execute("SELECT cost_per_unit FROM packaging_components WHERE component_name = ? COLLATE NOCASE", (component_name,))
        row = c.fetchone()
        unit_cost = row['cost_per_unit'] if row else 0
        c.execute("UPDATE packaging_components SET current_qty = current_qty - ? WHERE component_name = ? COLLATE NOCASE", (remaining, component_name))
        c.execute("INSERT INTO packaging_movements (date, component_name, movement_type, quantity, unit_cost, reference_type, reference_id, notes) VALUES (?, ?, 'OUT', ?, ?, ?, ?, ?)",
                  (date, component_name, remaining, unit_cost, reference_type, reference_id, notes or 'Used (no lot)'))
        allocations.append({'lot_id': None, 'qty': remaining, 'unit_cost': unit_cost})
        remaining = 0

    return allocations

def log_rm_movement(conn, date, ingredient_name, movement_type, quantity_kg, reference_type, reference_id, notes):
    """Log raw material stock movement"""
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO rm_stock_movements 
            (date, ingredient_name, movement_type, quantity_kg, reference_type, reference_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (date, ingredient_name, movement_type, quantity_kg, reference_type, reference_id, notes))
    except Exception as e:
        print(f"Error logging RM movement: {e}")

def init_database():
    """Initialize all database tables"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # ========== RAW MATERIALS ==========
    
    # Ingredients table (from existing system)
    c.execute("""
    CREATE TABLE IF NOT EXISTS ingredients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        qty_kg REAL NOT NULL DEFAULT 0,
        supplier TEXT DEFAULT '',
        cost_per_kg REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # Ensure ingredient names are treated case-insensitively for uniqueness
    c.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_ingredients_name_nocase
    ON ingredients(name COLLATE NOCASE)
    """)
    
    # Products table (from existing system)
    c.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        kg_to_litre_factor REAL DEFAULT 1.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Product ingredients / formula (from existing system)
    c.execute("""
    CREATE TABLE IF NOT EXISTS product_ingredients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        ingredient_name TEXT NOT NULL,
        percentage REAL NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    """)
    
    # RM Stock Movements (NEW)
    c.execute("""
    CREATE TABLE IF NOT EXISTS rm_stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        ingredient_name TEXT NOT NULL,
        movement_type TEXT NOT NULL,
        quantity_kg REAL NOT NULL,
        unit_cost REAL DEFAULT 0,
        reference_type TEXT,
        reference_id INTEGER,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # Ensure unit_cost exists for older DBs
    c.execute("PRAGMA table_info(rm_stock_movements)")
    rm_cols = [row[1] for row in c.fetchall()]
    if 'unit_cost' not in rm_cols:
        try:
            c.execute("ALTER TABLE rm_stock_movements ADD COLUMN unit_cost REAL DEFAULT 0")
        except Exception:
            pass
    
    # ========== BULK PRODUCTION ==========
    
    # First check if batch_number column exists
    c.execute("PRAGMA table_info(bulk_production)")
    columns = [row[1] for row in c.fetchall()]
    
    if 'bulk_production' not in [row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
        # Table doesn't exist, create it
        c.execute("""
        CREATE TABLE bulk_production (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            product_name TEXT NOT NULL,
            batch_number TEXT NOT NULL UNIQUE,
            batch_size_kg REAL NOT NULL,
            actual_yield_kg REAL NOT NULL,
            yield_loss_kg REAL NOT NULL,
            yield_percentage REAL NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    elif 'batch_number' not in columns:
        # Table exists but missing batch_number - need to recreate
        c.execute("DROP TABLE IF EXISTS bulk_production")
        c.execute("""
        CREATE TABLE bulk_production (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            product_name TEXT NOT NULL,
            batch_number TEXT NOT NULL UNIQUE,
            batch_size_kg REAL NOT NULL,
            actual_yield_kg REAL NOT NULL,
            yield_loss_kg REAL NOT NULL,
            yield_percentage REAL NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    
    # ========== PACKAGING ==========
    
    # Packaging Components (Dynamic)
    c.execute("""
    CREATE TABLE IF NOT EXISTS packaging_components (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        component_name TEXT UNIQUE NOT NULL,
        current_qty REAL DEFAULT 0,
        cost_per_unit REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # Ensure supplier column exists (for older DBs) and case-insensitive uniqueness
    c.execute("PRAGMA table_info(packaging_components)")
    pkg_cols = [row[1] for row in c.fetchall()]
    if 'supplier' not in pkg_cols:
        c.execute("ALTER TABLE packaging_components ADD COLUMN supplier TEXT DEFAULT ''")
    c.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_packaging_component_name_nocase
    ON packaging_components(component_name COLLATE NOCASE)
    """)
    
    # Packaging Movements
    c.execute("""
    CREATE TABLE IF NOT EXISTS packaging_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        component_name TEXT NOT NULL,
        movement_type TEXT NOT NULL,
        quantity REAL NOT NULL,
        unit_cost REAL DEFAULT 0,
        reference_type TEXT,
        reference_id INTEGER,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # Ensure unit_cost exists for older DBs
    c.execute("PRAGMA table_info(packaging_movements)")
    pkg_cols = [row[1] for row in c.fetchall()]
    if 'unit_cost' not in pkg_cols:
        try:
            c.execute("ALTER TABLE packaging_movements ADD COLUMN unit_cost REAL DEFAULT 0")
        except Exception:
            pass
    if 'lot_id' not in pkg_cols:
        try:
            c.execute("ALTER TABLE packaging_movements ADD COLUMN lot_id INTEGER DEFAULT NULL")
        except Exception:
            pass

    # ========== FILLING OPERATIONS ==========
    
    # Check if filling table exists and has correct schema
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='filling_operations'")
    if c.fetchone():
        c.execute("PRAGMA table_info(filling_operations)")
        fill_columns = [row[1] for row in c.fetchall()]
        if 'batch_number' not in fill_columns or 'pack_size_kg' not in fill_columns:
            c.execute("DROP TABLE IF EXISTS filling_operations")
            c.execute("DROP TABLE IF EXISTS filling_packaging_usage")
    
    c.execute("""
    CREATE TABLE IF NOT EXISTS filling_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        product_name TEXT NOT NULL,
        batch_number TEXT NOT NULL,
        pack_size_kg REAL NOT NULL,
        bulk_used_kg REAL NOT NULL,
        bulk_poured_kg REAL DEFAULT 0,
        actual_bottles_filled INTEGER NOT NULL,
        theoretical_bottles INTEGER NOT NULL,
        bottles_diff INTEGER NOT NULL,
        bottles_diff_percentage REAL NOT NULL,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Filling Packaging Usage
    c.execute("""
    CREATE TABLE IF NOT EXISTS filling_packaging_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filling_id INTEGER NOT NULL,
        component_name TEXT NOT NULL,
        quantity_used REAL NOT NULL,
        FOREIGN KEY(filling_id) REFERENCES filling_operations(id)
    )
    """)
    # Ensure 'bulk_poured_kg' exists on older DBs
    c.execute("PRAGMA table_info(filling_operations)")
    fill_cols = [row[1] for row in c.fetchall()]
    if 'bulk_poured_kg' not in fill_cols:
        c.execute("ALTER TABLE filling_operations ADD COLUMN bulk_poured_kg REAL DEFAULT 0")
    
    # ========== BOXING OPERATIONS ==========
    
    # Check if boxing table exists and has correct schema
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='boxing_operations'")
    if c.fetchone():
        c.execute("PRAGMA table_info(boxing_operations)")
        box_columns = [row[1] for row in c.fetchall()]
        if 'units_per_box' not in box_columns:
            c.execute("DROP TABLE IF EXISTS boxing_operations")
            c.execute("DROP TABLE IF EXISTS boxing_products")
            c.execute("DROP TABLE IF EXISTS boxing_packaging_usage")
    
    c.execute("""
    CREATE TABLE IF NOT EXISTS boxing_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        boxes_made INTEGER NOT NULL,
        units_per_box INTEGER NOT NULL,
        total_units_boxed INTEGER NOT NULL,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Boxing Products (for multi-product boxes)
    c.execute("""
    CREATE TABLE IF NOT EXISTS boxing_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        boxing_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        units_in_box INTEGER NOT NULL,
        pack_size_kg REAL DEFAULT 0,
        FOREIGN KEY(boxing_id) REFERENCES boxing_operations(id)
    )
    """)
    # Ensure pack_size_kg exists for older DBs
    c.execute("PRAGMA table_info(boxing_products)")
    box_prod_cols = [row[1] for row in c.fetchall()]
    if 'pack_size_kg' not in box_prod_cols:
        c.execute("ALTER TABLE boxing_products ADD COLUMN pack_size_kg REAL DEFAULT 0")
    
    # Boxing Packaging Usage
    c.execute("""
    CREATE TABLE IF NOT EXISTS boxing_packaging_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        boxing_id INTEGER NOT NULL,
        component_name TEXT NOT NULL,
        quantity_used REAL NOT NULL,
        FOREIGN KEY(boxing_id) REFERENCES boxing_operations(id)
    )
    """)
    
    # ========== MANPOWER ==========
    
    # Check if manpower table exists and has correct schema
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='manpower_daily'")
    if c.fetchone():
        c.execute("PRAGMA table_info(manpower_daily)")
        man_columns = [row[1] for row in c.fetchall()]
        if 'production_male' not in man_columns:
            c.execute("DROP TABLE IF EXISTS manpower_daily")
    
    c.execute("""
    CREATE TABLE IF NOT EXISTS manpower_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL UNIQUE,
        production_male INTEGER DEFAULT 0,
        production_female INTEGER DEFAULT 0,
        production_hours REAL DEFAULT 0,
        filling_male INTEGER DEFAULT 0,
        filling_female INTEGER DEFAULT 0,
        filling_hours REAL DEFAULT 0,
        boxing_male INTEGER DEFAULT 0,
        boxing_female INTEGER DEFAULT 0,
        boxing_hours REAL DEFAULT 0,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # Ensure hours columns exist on older DBs
    c.execute("PRAGMA table_info(manpower_daily)")
    man_cols = [row[1] for row in c.fetchall()]
    if 'production_hours' not in man_cols:
        c.execute("ALTER TABLE manpower_daily ADD COLUMN production_hours REAL DEFAULT 0")
    if 'filling_hours' not in man_cols:
        c.execute("ALTER TABLE manpower_daily ADD COLUMN filling_hours REAL DEFAULT 0")
    if 'boxing_hours' not in man_cols:
        c.execute("ALTER TABLE manpower_daily ADD COLUMN boxing_hours REAL DEFAULT 0")
    
    # ========== AUDITS ==========
    
    c.execute("""
    CREATE TABLE IF NOT EXISTS audits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        audit_date TEXT NOT NULL UNIQUE,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Audit Ingredient Records
    c.execute("""
    CREATE TABLE IF NOT EXISTS audit_ingredients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        audit_id INTEGER NOT NULL,
        ingredient_name TEXT NOT NULL,
        previous_weight_kg REAL NOT NULL,
        expected_weight_kg REAL NOT NULL,
        actual_weight_kg REAL NOT NULL,
        difference_kg REAL NOT NULL,
        FOREIGN KEY(audit_id) REFERENCES audits(id)
    )
    """)

    # ========== SAMPLES ==========
    c.execute("""
    CREATE TABLE IF NOT EXISTS samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        product_name TEXT NOT NULL,
        batch_number TEXT,
        pack_size_kg REAL,
        quantity INTEGER NOT NULL,
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # ========== FILLING DAMAGES ==========
    c.execute("""
    CREATE TABLE IF NOT EXISTS filling_damages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        product_name TEXT NOT NULL,
        batch_number TEXT,
        pack_size_kg REAL,
        quantity INTEGER NOT NULL,
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # ========== FINISHED GOODS SHIPMENTS ==========
    c.execute("""
    CREATE TABLE IF NOT EXISTS finished_goods_shipments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        boxing_id INTEGER,
        product_name TEXT,
        pack_size_kg REAL,
        boxes_sent INTEGER DEFAULT 0,
        units_sent INTEGER DEFAULT 0,
        notes TEXT,
        invoice_number TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(boxing_id) REFERENCES boxing_operations(id)
    )
    """)
    # Ensure invoice_number exists for older DBs
    c.execute("PRAGMA table_info(finished_goods_shipments)")
    fg_cols = [row[1] for row in c.fetchall()]
    if 'invoice_number' not in fg_cols:
        try:
            c.execute("ALTER TABLE finished_goods_shipments ADD COLUMN invoice_number TEXT")
        except Exception:
            pass
    # Defer final commit/close until all tables are created (avoid operating on closed DB)

    # ========== SYNC LOG ==========

    c.execute("""
    CREATE TABLE IF NOT EXISTS sync_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sync_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sync_type TEXT,
        status TEXT,
        message TEXT
    )
    """)

    conn.commit()
    # Create rm_lots and packaging_lots tables for FIFO tracking
    c.execute("""
    CREATE TABLE IF NOT EXISTS rm_lots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ingredient_name TEXT NOT NULL,
        qty_remaining REAL NOT NULL,
        unit_cost REAL DEFAULT 0,
        received_date TEXT,
        reference_type TEXT,
        reference_id INTEGER,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS packaging_lots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        component_name TEXT NOT NULL,
        qty_remaining REAL NOT NULL,
        unit_cost REAL DEFAULT 0,
        received_date TEXT,
        reference_type TEXT,
        reference_id INTEGER,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()
    print("✅ Database initialized successfully!")
    print("✅ All V2 tables created/updated!")

# ============================================================================
# ROUTES - HOME & DASHBOARD
# ============================================================================

@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/api/dashboard/stats')
def dashboard_stats():
    """Get dashboard statistics for selected month"""
    try:
        month = request.args.get('month', datetime.now().strftime('%Y-%m'))
        conn = get_db()
        c = conn.cursor()
        
        # Get all products produced this month
        c.execute("""
            SELECT product_name, 
                   SUM(batch_size_kg) as total_planned,
                   SUM(actual_yield_kg) as total_yield
            FROM bulk_production
            WHERE date LIKE ?
            GROUP BY product_name
        """, (f"{month}%",))
        products = [dict(row) for row in c.fetchall()]
        
        # For each product, get filling and boxing data
        # Also compute monthly boxing aggregates across products and pack sizes
        # Precompute boxing breakdowns: total units per product+pack_size in the month
        c.execute("""
            SELECT bp.product_name, bp.pack_size_kg, COALESCE(SUM(bp.units_in_box),0) as total_units
            FROM boxing_products bp
            JOIN boxing_operations bo ON bo.id = bp.boxing_id
            WHERE bo.date LIKE ?
            GROUP BY bp.product_name, bp.pack_size_kg
        """, (f"{month}%",))
        boxing_breakdown = [dict(r) for r in c.fetchall()]

        # Total boxes made in month
        c.execute("SELECT COALESCE(SUM(boxes_made),0) as boxes_made FROM boxing_operations WHERE date LIKE ?", (f"{month}%",))
        total_boxes_made = int(c.fetchone()['boxes_made'] or 0)

        for product in products:
            # Filling data by pack size
            c.execute("""
                SELECT pack_size_kg,
                       SUM(actual_bottles_filled) as total_bottles,
                       SUM(theoretical_bottles) as theoretical_bottles
                FROM filling_operations
                WHERE product_name = ? AND date LIKE ?
                GROUP BY pack_size_kg
            """, (product['product_name'], f"{month}%"))
            product['filling'] = [dict(row) for row in c.fetchall()]

            # Attach boxing breakdown entries for this product
            product['boxing'] = [b for b in boxing_breakdown if b['product_name'] == product['product_name']]
            product['boxing_total_boxes_made'] = total_boxes_made
            # Boxes made for this product in the month
            c.execute("SELECT COALESCE(SUM(bo.boxes_made),0) as boxes_for_product FROM boxing_products bp JOIN boxing_operations bo ON bo.id = bp.boxing_id WHERE bp.product_name = ? COLLATE NOCASE AND bo.date LIKE ?", (product['product_name'], f"{month}%"))
            product['boxes_made_for_product'] = int(c.fetchone()['boxes_for_product'] or 0)
            # Boxes dispatched for this product in the month (by product reference)
            c.execute("SELECT COALESCE(SUM(boxes_sent),0) as boxes_sent_prod FROM finished_goods_shipments WHERE product_name = ? COLLATE NOCASE AND date LIKE ?", (product['product_name'], f"{month}%"))
            sent_by_prod = int(c.fetchone()['boxes_sent_prod'] or 0)
            # Boxes dispatched referencing boxing ops in the month that include this product
            c.execute("SELECT COALESCE(SUM(fgs.boxes_sent),0) as boxes_sent_bo FROM finished_goods_shipments fgs JOIN boxing_products bp ON bp.boxing_id = fgs.boxing_id WHERE LOWER(bp.product_name) = LOWER(?) AND fgs.date LIKE ?", (product['product_name'], f"{month}%"))
            sent_by_bo = int(c.fetchone()['boxes_sent_bo'] or 0)
            product['boxes_dispatched_month'] = sent_by_prod + sent_by_bo
        
        # Unfilled bulk
        c.execute("""
            SELECT bp.batch_number, bp.product_name, bp.actual_yield_kg,
                   COALESCE(SUM(fo.bulk_used_kg), 0) as used_kg
            FROM bulk_production bp
            LEFT JOIN filling_operations fo ON fo.batch_number = bp.batch_number
            WHERE bp.date LIKE ?
            GROUP BY bp.batch_number, bp.product_name, bp.actual_yield_kg
            HAVING (bp.actual_yield_kg - COALESCE(SUM(fo.bulk_used_kg), 0)) > 0.01
        """, (f"{month}%",))
        unfilled = [dict(row) for row in c.fetchall()]
        
        # Unboxed bottles — computed from separate subqueries to avoid Cartesian product bugs
        # 1. Total filled this month per product+pack_size
        c.execute("""
            SELECT product_name, pack_size_kg, SUM(actual_bottles_filled) as total_filled
            FROM filling_operations WHERE date LIKE ?
            GROUP BY product_name, pack_size_kg
        """, (f"{month}%",))
        filled_map = {(r['product_name'], r['pack_size_kg']): int(r['total_filled'] or 0) for r in c.fetchall()}

        # 2. All-time boxed per product+pack_size
        c.execute("""
            SELECT product_name, pack_size_kg, SUM(units_in_box) as total_boxed
            FROM boxing_products GROUP BY product_name, pack_size_kg
        """)
        boxed_map = {(r['product_name'], r['pack_size_kg']): int(r['total_boxed'] or 0) for r in c.fetchall()}

        # 3. All-time samples per product+pack_size
        c.execute("SELECT product_name, pack_size_kg, SUM(quantity) as total FROM samples GROUP BY product_name, pack_size_kg")
        samples_map = {(r['product_name'], r['pack_size_kg']): int(r['total'] or 0) for r in c.fetchall()}

        # 4. All-time damages per product+pack_size
        c.execute("SELECT product_name, pack_size_kg, SUM(quantity) as total FROM filling_damages GROUP BY product_name, pack_size_kg")
        damages_map = {(r['product_name'], r['pack_size_kg']): int(r['total'] or 0) for r in c.fetchall()}

        unboxed = []
        for (pname, psize), total_filled in filled_map.items():
            boxed   = boxed_map.get((pname, psize), 0)
            samples = samples_map.get((pname, psize), 0)
            damages = damages_map.get((pname, psize), 0)
            available = total_filled - boxed - samples - damages
            if available > 0:
                unboxed.append({
                    'product_name': pname,
                    'pack_size_kg': psize,
                    'total_bottles': total_filled,
                    'boxed_units': boxed,
                    'samples_sent': samples,
                    'damages': damages,
                    'available': available
                })
        
        conn.close()
        
        return jsonify({
            'success': True,
            'data': {
                'products': products,
                'unfilled': unfilled,
                'unboxed': unboxed
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/dashboard/consumption-costs')
def dashboard_consumption_costs():
    """Get consumption costs for dashboard display"""
    try:
        month = request.args.get('month', datetime.now().strftime('%Y-%m'))
        products = request.args.get('products', '')
        
        # Reuse the monthly consumption cost endpoint
        return get_monthly_consumption_cost()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/delete-all', methods=['POST'])
def admin_delete_all():
    try:
        data = request.json or {}
        password = data.get('password') if data else None
        # Allow client-side password check; accept if provided or if omitted (we still proceed only when client confirmed)
        # For safety require the exact master password
        import os
        MASTER_PASSWORD = os.environ.get('MASTER_PASSWORD', '')
        if password is None or password != MASTER_PASSWORD:
            return jsonify({'success': False, 'error': 'Invalid password'}), 403

        conn = get_db()
        c = conn.cursor()
        # List of tables to clear (preserve schema)
        tables_to_clear = [
            'rm_stock_movements', 'bulk_production', 'filling_packaging_usage', 'filling_operations',
            'boxing_packaging_usage', 'boxing_products', 'boxing_operations', 'manpower_daily',
            'audits', 'audit_ingredients', 'samples', 'filling_damages', 'finished_goods_shipments',
            'packaging_movements', 'sync_log'
        ]
        for t in tables_to_clear:
            c.execute(f"DELETE FROM {t}")
        # Reset packaging quantities but keep component rows
        c.execute("UPDATE packaging_components SET current_qty = 0")
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'All data deleted (schema preserved)'}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/dashboard/wastage-report')
def dashboard_wastage_report():
    """Get wastage report in litres for selected month and products"""
    try:
        month = request.args.get('month', datetime.now().strftime('%Y-%m'))
        product_filter = request.args.get('products', '')
        
        conn = get_db()
        c = conn.cursor()
        
        start_date = f"{month}-01"
        from calendar import monthrange
        year, mon = map(int, month.split('-'))
        last_day = monthrange(year, mon)[1]
        end_date = f"{month}-{last_day:02d}"
        
        # Get products to analyze
        if product_filter:
            products = [p.strip() for p in product_filter.split(',')]
        else:
            c.execute("SELECT DISTINCT product_name FROM bulk_production WHERE date >= ? AND date <= ?", 
                     (start_date, end_date))
            products = [row[0] for row in c.fetchall()]
        
        results = []
        
        for product_name in products:
            # Get kg-to-litre factor
            c.execute("SELECT kg_to_litre_factor FROM products WHERE name = ?", (product_name,))
            product_row = c.fetchone()
            factor = product_row['kg_to_litre_factor'] if product_row else 1.0
            
            wastage_data = {
                'product': product_name,
                'kg_to_litre_factor': factor,
                'production_wastage': {},
                'filling_wastage': {},
                'total_wastage_litres': 0
            }
            
            # 1. Production wastage (planned - actual)
            c.execute("""
                SELECT SUM(batch_size_kg) as planned, SUM(actual_yield_kg) as actual
                FROM bulk_production
                WHERE product_name = ? AND date >= ? AND date <= ?
            """, (product_name, start_date, end_date))
            prod = c.fetchone()
            if prod and prod['planned']:
                planned_kg = prod['planned'] or 0
                actual_kg = prod['actual'] or 0
                loss_kg = planned_kg - actual_kg
                loss_litres = loss_kg * factor
                
                wastage_data['production_wastage'] = {
                    'planned_kg': round(planned_kg, 4),
                    'actual_kg': round(actual_kg, 4),
                    'loss_kg': round(loss_kg, 4),
                    'planned_litres': round(planned_kg * factor, 4),
                    'actual_litres': round(actual_kg * factor, 4),
                    'loss_litres': round(loss_litres, 4),
                    'loss_percentage': round((loss_kg / planned_kg * 100) if planned_kg > 0 else 0, 2)
                }
                wastage_data['total_wastage_litres'] += loss_litres
            
            # 2. Filling wastage (bulk poured - bulk in bottles + theoretical - actual)
            c.execute("""
                SELECT SUM(bulk_poured_kg) as poured, SUM(bulk_used_kg) as in_bottles,
                       SUM(theoretical_bottles) as theoretical, SUM(actual_bottles_filled) as actual,
                       pack_size_kg
                FROM filling_operations
                WHERE product_name = ? AND date >= ? AND date <= ?
                GROUP BY pack_size_kg
            """, (product_name, start_date, end_date))
            
            filling_losses = []
            for row in c.fetchall():
                poured_kg = row['poured'] or 0
                in_bottles_kg = row['in_bottles'] or 0
                theoretical = row['theoretical'] or 0
                actual = row['actual'] or 0
                pack_size_kg = row['pack_size_kg'] or 0

                # Wastage = bulk poured - bulk that made it into bottles
                waste_kg = poured_kg - in_bottles_kg
                waste_litres = waste_kg * factor

                # Bottle difference: if theoretical > actual, that's a loss of bottles
                bottle_diff = (theoretical - actual)
                bottle_diff_litres = 0
                if bottle_diff > 0 and pack_size_kg:
                    bottle_diff_litres = bottle_diff * pack_size_kg * factor

                filling_losses.append({
                    'pack_size_kg': pack_size_kg,
                    'pack_size_litres': round(pack_size_kg * factor, 4),
                    'poured_kg': round(poured_kg, 4),
                    'poured_litres': round(poured_kg * factor, 4),
                    'in_bottles_kg': round(in_bottles_kg, 4),
                    'in_bottles_litres': round(in_bottles_kg * factor, 4),
                    'waste_kg': round(waste_kg, 4),
                    'waste_litres': round(waste_litres, 4),
                    'theoretical_bottles': theoretical,
                    'actual_bottles': actual,
                    'bottle_diff': bottle_diff,
                    'bottle_diff_litres': round(bottle_diff_litres, 4)
                })
                wastage_data['total_wastage_litres'] += (waste_litres + bottle_diff_litres)
            
            wastage_data['filling_wastage'] = filling_losses
            wastage_data['total_wastage_litres'] = round(wastage_data['total_wastage_litres'], 4)
            
            results.append(wastage_data)
        
        conn.close()
        return jsonify({'success': True, 'data': results, 'month': month})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500



# ============================================================================
# ROUTES - RAW MATERIALS
# ============================================================================

@app.route('/raw-materials')
def raw_materials():
    return render_template('raw_materials.html')

@app.route('/api/ingredients')
def get_ingredients():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM ingredients ORDER BY name COLLATE NOCASE")
        ingredients = [dict(row) for row in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': ingredients})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/ingredients/add', methods=['POST'])
def add_ingredient():
    try:
        data = request.json
        conn = get_db()
        c = conn.cursor()
        # Case-insensitive check to prevent duplicates like 'Sugar' vs 'sugar'
        c.execute("SELECT id FROM ingredients WHERE name = ? COLLATE NOCASE", (data['name'],))
        if c.fetchone():
            conn.close()
            return jsonify({'success': False, 'error': 'Ingredient already exists'}), 400
        name = data['name'].strip()
        qty = float(data.get('qty_kg', 0) or 0)
        supplier = data.get('supplier', '')
        cost = float(data.get('cost_per_kg', 0) or 0)
        c.execute("""
            INSERT INTO ingredients (name, qty_kg, supplier, cost_per_kg)
            VALUES (?, ?, ?, ?)
        """, (name, qty, supplier, cost))
        ingredient_id = c.lastrowid
        # Log initial stock movement and create lot (unit_cost from provided cost)
        c.execute("""
            INSERT INTO rm_stock_movements
            (date, ingredient_name, movement_type, quantity_kg, unit_cost, reference_type, reference_id, notes)
            VALUES (?, ?, 'IN', ?, ?, 'Initial', ?, ?)
        """, (datetime.now().strftime('%Y-%m-%d'), name, qty, cost, ingredient_id, f'Initial stock for {name}'))
        try:
            create_rm_lot(conn, name, qty, cost, datetime.now().strftime('%Y-%m-%d'), 'Initial', ingredient_id, f'Initial lot for {name}')
        except Exception:
            pass
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Ingredient added successfully'})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Ingredient already exists'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/ingredients/update/<int:id>', methods=['PUT'])
def update_ingredient(id):
    try:
        data = request.json
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT qty_kg, name, supplier, cost_per_kg FROM ingredients WHERE id = ?", (id,))
        row = c.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Ingredient not found'}), 404
        old_qty = float(row['qty_kg'] or 0)
        name = row['name']
        new_qty = float(data.get('qty_kg', old_qty) or 0)
        qty_diff = new_qty - old_qty
        new_supplier = data.get('supplier', row['supplier'] if row else '')
        new_cost = data.get('cost_per_kg', None)
        if new_cost is None:
            new_cost = row['cost_per_kg']
        # Update to the new absolute quantity
        c.execute("""
            UPDATE ingredients
            SET qty_kg = ?, supplier = ?, cost_per_kg = ?
            WHERE id = ?
        """, (new_qty, new_supplier, new_cost, id))
        # Log the movement if there was a change
        if abs(qty_diff) > 0.0001:
            movement_type = 'IN' if qty_diff > 0 else 'OUT'
            lot_id = data.get('lot_id')
            lot_cost = 0.0
            if lot_id:
                c.execute("SELECT unit_cost FROM rm_lots WHERE id = ?", (lot_id,))
                lot_row = c.fetchone()
                if lot_row:
                    lot_cost = float(lot_row['unit_cost'] or 0)
                    # Adjust the specific lot's qty_remaining
                    c.execute("UPDATE rm_lots SET qty_remaining = qty_remaining + ? WHERE id = ?", (qty_diff, lot_id))
            else:
                lot_cost = float(new_cost or 0)
            note = f"{name} of cost \u20b9{round(lot_cost, 2)} changed from {round(old_qty, 4)} to {round(new_qty, 4)}"
            c.execute("""
                INSERT INTO rm_stock_movements
                (date, ingredient_name, movement_type, quantity_kg, unit_cost, reference_type, notes)
                VALUES (?, ?, ?, ?, ?, 'Manual', ?)
            """, (datetime.now().strftime('%Y-%m-%d'), name, movement_type, abs(qty_diff), lot_cost, note))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Ingredient updated successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/api/ingredients/restock/<int:id>', methods=['POST'])
def restock_ingredient(id):
    try:
        data = request.json
        qty_to_add = float(data.get('quantity', 0) or 0)
        if qty_to_add <= 0:
            return jsonify({'success': False, 'error': 'Quantity must be greater than 0'}), 400
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT name, supplier FROM ingredients WHERE id = ?", (id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Ingredient not found'}), 404
        name = row['name']
        unit_cost = float(data.get('unit_cost', data.get('cost_per_kg', 0) or 0) or 0)
        if unit_cost <= 0:
            conn.close()
            return jsonify({'success': False, 'error': 'Please provide a valid cost per kg for this restock'}), 400
        restock_date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        notes = data.get('notes', 'Restocked')
        supplier = data.get('supplier', '').strip()
        c.execute("UPDATE ingredients SET qty_kg = qty_kg + ? WHERE id = ?", (qty_to_add, id))
        # Update supplier if provided for this restock
        if supplier:
            c.execute("UPDATE ingredients SET supplier = ? WHERE id = ?", (supplier, id))
        c.execute("""
            INSERT INTO rm_stock_movements 
            (date, ingredient_name, movement_type, quantity_kg, unit_cost, reference_type, notes)
            VALUES (?, ?, 'IN', ?, ?, 'Restock', ?)
        """, (restock_date, name, qty_to_add, unit_cost, notes))
        create_rm_lot(conn, name, qty_to_add, unit_cost, restock_date, 'Restock', id, notes)
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': f'Restocked {qty_to_add} kg of {name} at ₹{unit_cost}/kg'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/raw-materials/movement-history')
def get_rm_movement_history():
    """Get detailed RM history with optional ingredient filtering"""
    try:
        ingredient_filter = request.args.get('ingredients', '')  # Comma-separated ingredient names
        start_date = request.args.get('start_date', '')
        end_date = request.args.get('end_date', '')
        
        conn = get_db()
        c = conn.cursor()
        
        query = """
            SELECT date, ingredient_name, movement_type, quantity_kg, unit_cost,
                   reference_type, notes, created_at
            FROM rm_stock_movements
            WHERE 1=1
        """
        params = []
        
        if ingredient_filter:
            ingredients = [i.strip() for i in ingredient_filter.split(',')]
            placeholders = ','.join(['?' for _ in ingredients])
            query += f" AND ingredient_name COLLATE NOCASE IN ({placeholders})"
            params.extend(ingredients)
        
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        
        query += " ORDER BY date DESC, created_at DESC"
        
        c.execute(query, params)
        movements = [dict(row) for row in c.fetchall()]
        
        conn.close()
        return jsonify({'success': True, 'data': movements})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



# ============================================================================
# ROUTES - PRODUCTS
# ============================================================================

@app.route('/products')
def products():
    return render_template('products.html')

@app.route('/api/products')
def get_products():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, name FROM products ORDER BY name")
        products = []
        for row in c.fetchall():
            product = dict(row)
            c.execute("""
                SELECT ingredient_name, percentage
                FROM product_ingredients
                WHERE product_id = ?
                ORDER BY id
            """, (product['id'],))
            product['ingredients'] = [dict(r) for r in c.fetchall()]
            products.append(product)
        conn.close()
        return jsonify({'success': True, 'data': products})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/products/add', methods=['POST'])
def add_product():
    try:
        data = request.json
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO products (name, kg_to_litre_factor) VALUES (?, ?)", 
          (data['name'], data.get('kg_to_litre_factor', 1.0)))
        product_id = c.lastrowid
        for ing in data['ingredients']:
            if ing['ingredient_name'].strip() and ing['percentage'] > 0:
                c.execute("""
                    INSERT INTO product_ingredients (product_id, ingredient_name, percentage)
                    VALUES (?, ?, ?)
                """, (product_id, ing['ingredient_name'], ing['percentage']))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Product added successfully'})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Product already exists'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/products/update/<int:id>', methods=['PUT'])
def update_product(id):
    try:
        data = request.json
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE products SET name = ?, kg_to_litre_factor = ? WHERE id = ?", 
          (data['name'], data.get('kg_to_litre_factor', 1.0), id))
        c.execute("DELETE FROM product_ingredients WHERE product_id = ?", (id,))
        for ing in data['ingredients']:
            if ing['ingredient_name'].strip() and ing['percentage'] > 0:
                c.execute("""
                    INSERT INTO product_ingredients (product_id, ingredient_name, percentage)
                    VALUES (?, ?, ?)
                """, (id, ing['ingredient_name'], ing['percentage']))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Product updated successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/products/delete/<int:id>', methods=['DELETE'])
def delete_product(id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM product_ingredients WHERE product_id = ?", (id,))
        c.execute("DELETE FROM products WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Product deleted successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# ROUTES - BULK PRODUCTION
# ============================================================================

@app.route('/bulk-production')
def bulk_production():
    return render_template('bulk_production.html')

@app.route('/api/bulk-production/check', methods=['POST'])
def check_production_requirements():
    try:
        data = request.json
        product_name = data['product_name']
        batch_size = float(data['batch_size_kg'])
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM products WHERE name = ?", (product_name,))
        product = c.fetchone()
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
        c.execute("""
            SELECT ingredient_name, percentage
            FROM product_ingredients
            WHERE product_id = ?
        """, (product['id'],))
        requirements = []
        insufficient = False
        for row in c.fetchall():
            ing_name = row['ingredient_name']
            percentage = row['percentage']
            required_kg = batch_size * (percentage / 100.0)
            c.execute("SELECT qty_kg FROM ingredients WHERE name = ? COLLATE NOCASE", (ing_name,))
            ing = c.fetchone()
            available_kg = ing['qty_kg'] if ing else 0
            is_sufficient = available_kg >= required_kg
            if not is_sufficient:
                insufficient = True
            requirements.append({
                'ingredient': ing_name,
                'percentage': percentage,
                'required_kg': round(required_kg, 3),
                'available_kg': round(available_kg, 3),
                'sufficient': is_sufficient
            })
        conn.close()
        return jsonify({
            'success': True,
            'data': {
                'requirements': requirements,
                'can_produce': not insufficient
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/bulk-production/confirm', methods=['POST'])
def confirm_bulk_production():
    try:
        data = request.json
        product_name = data['product_name']
        batch_size = float(data['batch_size_kg'])
        actual_yield = float(data['actual_yield_kg'])
        batch_number = data['batch_number']
        date = data['date']
        notes = data.get('notes', '')
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM products WHERE name = ?", (product_name,))
        product = c.fetchone()
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
        c.execute("""
            SELECT ingredient_name, percentage
            FROM product_ingredients
            WHERE product_id = ?
        """, (product['id'],))
        ingredients = c.fetchall()
        for row in ingredients:
            ing_name = row['ingredient_name']
            percentage = row['percentage']
            required_kg = batch_size * (percentage / 100.0)
            c.execute("SELECT qty_kg FROM ingredients WHERE name = ? COLLATE NOCASE", (ing_name,))
            ing = c.fetchone()
            if not ing or ing['qty_kg'] < required_kg:
                return jsonify({
                    'success': False,
                    'error': f'Insufficient {ing_name}. Need {required_kg:.2f}kg'
                }), 400
        # Insert production record first so we can reference it in movements
        yield_loss = round(batch_size - actual_yield, 4)
        yield_percentage = (actual_yield / batch_size * 100) if batch_size > 0 else 0
        c.execute("""
            INSERT INTO bulk_production 
            (date, product_name, batch_number, batch_size_kg, actual_yield_kg, 
             yield_loss_kg, yield_percentage, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (date, product_name, batch_number, batch_size, actual_yield, 
              yield_loss, yield_percentage, notes))
        production_id = c.lastrowid
        # Deduct ingredients and log movements referencing the production id
        allocations_summary = {}
        for row in ingredients:
            ing_name = row['ingredient_name']
            percentage = row['percentage']
            qty_used = batch_size * (percentage / 100.0)
            # allocate using FIFO lots and record movements
            allocs = consume_rm_fifo(conn, ing_name, qty_used, date, 'Production', production_id, f'{round(qty_used,4)} kg of {ing_name} used for {product_name} batch {batch_number}')
            allocations_summary[ing_name] = allocs
        conn.commit()
        conn.close()
        return jsonify({
            'success': True,
            'message': 'Production confirmed successfully',
            'data': {
                'production_id': production_id,
                'yield_loss_kg': round(yield_loss, 3),
                'yield_percentage': round(yield_percentage, 2)
            },
            'allocations': allocations_summary
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/bulk-production/history')
def get_bulk_production_history():
    try:
        # Optional filters: products (comma-separated), month (YYYY-MM)
        products_q = request.args.get('products', request.args.get('product', '')).strip()
        month = request.args.get('month', '').strip()
        conn = get_db()
        c = conn.cursor()
        query = "SELECT * FROM bulk_production WHERE 1=1"
        params = []
        products = [p.strip() for p in products_q.split(',') if p.strip()]
        if products:
            placeholders = ','.join(['?' for _ in products])
            query += f" AND LOWER(product_name) IN ({placeholders})"
            params.extend([p.lower() for p in products])
        if month:
            query += " AND date LIKE ?"
            params.append(f"{month}-%")
        query += " ORDER BY date DESC, id DESC"
        c.execute(query, params)
        history = [dict(row) for row in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': history})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/filling/delete/<int:id>', methods=['DELETE'])
def delete_filling(id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, batch_number, bulk_used_kg, product_name, actual_bottles_filled, pack_size_kg FROM filling_operations WHERE id = ?", (id,))
        record = c.fetchone()
        if not record:
            return jsonify({'success': False, 'error': 'Filling record not found'}), 404
        bulk_used = float(record['bulk_used_kg'] or 0)
        product_name = record['product_name']
        pack_size = float(record['pack_size_kg'] or 0)
        bottles_in_record = int(record['actual_bottles_filled'] or 0)

        # Ensure deleting this filling won't leave boxed units without source
        # Only consider boxed units for the same pack size as this filling
        if pack_size > 0:
            c.execute("SELECT COALESCE(SUM(units_in_box),0) as boxed FROM boxing_products bp JOIN boxing_operations bo ON bo.id = bp.boxing_id WHERE bp.product_name = ? AND bp.pack_size_kg = ?", (product_name, pack_size))
        else:
            c.execute("SELECT COALESCE(SUM(units_in_box),0) as boxed FROM boxing_products bp JOIN boxing_operations bo ON bo.id = bp.boxing_id WHERE bp.product_name = ?", (product_name,))
        total_boxed = int(c.fetchone()['boxed'] or 0)
        # total filled across all fillings
        c.execute("SELECT COALESCE(SUM(actual_bottles_filled),0) as total FROM filling_operations WHERE product_name = ?", (product_name,))
        total_filled = int(c.fetchone()['total'] or 0)
        if total_boxed > (total_filled - bottles_in_record):
            return jsonify({'success': False, 'error': 'Cannot delete filling — some bottles have already been boxed and would be left without source.'}), 400
        # Deleting a filling restores bulk availability implicitly by removing the filling record.
        # Do NOT modify raw material (ingredients) quantities here because they were consumed during production.

        # Restore packaging components used — per FIFO lot so each lot gets its qty back
        # and the packaging history reflects the correct unit cost per allocation.
        today = datetime.now().strftime('%Y-%m-%d')
        batch_number = record['batch_number']

        # Look up the individual OUT movements that were created when this filling consumed packaging.
        # These movements carry the exact lot_id and unit_cost per allocation.
        c.execute("""
            SELECT component_name, quantity, unit_cost, lot_id
            FROM packaging_movements
            WHERE reference_type = 'Filling' AND reference_id = ? AND movement_type = 'OUT'
        """, (id,))
        pkg_movements = c.fetchall()

        if pkg_movements:
            # Restore per-lot allocations
            for m in pkg_movements:
                comp = m['component_name']
                qty = float(m['quantity'])
                unit_cost = float(m['unit_cost'] or 0)
                lot_id = m['lot_id']

                # Restore the specific packaging lot so FIFO order is preserved
                if lot_id:
                    c.execute("UPDATE packaging_lots SET qty_remaining = qty_remaining + ? WHERE id = ?", (qty, lot_id))

                # Restore master component qty
                c.execute("UPDATE packaging_components SET current_qty = current_qty + ? WHERE component_name = ? COLLATE NOCASE", (qty, comp))

                # Log an IN movement with the correct unit_cost for this allocation
                qty_display = int(qty) if qty == int(qty) else qty
                note = f"{qty_display} pieces of {comp} restored from filling of batch {batch_number}"
                c.execute("""
                    INSERT INTO packaging_movements
                    (date, component_name, movement_type, quantity, unit_cost, reference_type, reference_id, notes)
                    VALUES (?, ?, 'IN', ?, ?, 'Filling Deletion', ?, ?)
                """, (today, comp, qty, unit_cost, id, note))
        else:
            # Fallback for older fillings that pre-date lot_id tracking:
            # restore aggregate quantities from filling_packaging_usage only.
            c.execute("SELECT component_name, quantity_used FROM filling_packaging_usage WHERE filling_id = ?", (id,))
            for r in c.fetchall():
                comp = r['component_name']
                qty = float(r['quantity_used'])
                c.execute("UPDATE packaging_components SET current_qty = current_qty + ? WHERE component_name = ? COLLATE NOCASE", (qty, comp))
                qty_display = int(qty) if qty == int(qty) else qty
                note = f"{qty_display} pieces of {comp} restored from filling of batch {batch_number}"
                c.execute("""
                    INSERT INTO packaging_movements
                    (date, component_name, movement_type, quantity, reference_type, reference_id, notes)
                    VALUES (?, ?, 'IN', ?, 'Filling Deletion', ?, ?)
                """, (today, comp, qty, id, note))
        # Delete usage records and the filling record
        c.execute("DELETE FROM filling_packaging_usage WHERE filling_id = ?", (id,))
        c.execute("DELETE FROM filling_operations WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Filling deleted and materials restored'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/bulk-production/delete/<int:id>', methods=['DELETE'])
def delete_bulk_production(id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT product_name, batch_size_kg, batch_number
            FROM bulk_production
            WHERE id = ?
        """, (id,))
        prod = c.fetchone()
        if not prod:
            return jsonify({'success': False, 'error': 'Production not found'}), 404
        product_name = prod['product_name']
        batch_size = prod['batch_size_kg']
        batch_number = prod['batch_number']
        c.execute("SELECT id FROM products WHERE name = ?", (product_name,))
        product = c.fetchone()
        if product:
            c.execute("""
                SELECT ingredient_name, percentage
                FROM product_ingredients
                WHERE product_id = ?
            """, (product['id'],))
            for row in c.fetchall():
                ing_name = row['ingredient_name']
                percentage = row['percentage']
                required_kg = batch_size * (percentage / 100.0)
                c.execute("UPDATE ingredients SET qty_kg = qty_kg + ? WHERE name = ? COLLATE NOCASE", (required_kg, ing_name))
                c.execute("""
                    INSERT INTO rm_stock_movements 
                    (date, ingredient_name, movement_type, quantity_kg, reference_type, notes)
                    VALUES (?, ?, 'IN', ?, 'Production Deletion', ?)
                """, (datetime.now().strftime('%Y-%m-%d'), ing_name, required_kg,
                      f'{round(required_kg, 4)} kg of {ing_name} restored from production of {product_name} \u2013 batch {batch_number}'))
        c.execute("DELETE FROM bulk_production WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Production deleted and stock restored'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================================
# ROUTES - PACKAGING
# ============================================================================

@app.route('/packaging')
def packaging():
    return render_template('packaging.html')

@app.route('/api/packaging/components')
def get_packaging_components():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, component_name, current_qty, cost_per_unit, supplier FROM packaging_components ORDER BY component_name COLLATE NOCASE")
        components = [dict(row) for row in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': components})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/packaging/components/add', methods=['POST'])
def add_packaging_component():
    try:
        data = request.json
        conn = get_db()
        c = conn.cursor()
        # Ensure migration columns exist
        c.execute("PRAGMA table_info(packaging_components)")
        pkg_cols = [r[1] for r in c.fetchall()]
        if 'cost_per_unit' not in pkg_cols:
            c.execute("ALTER TABLE packaging_components ADD COLUMN cost_per_unit REAL DEFAULT 0")
        if 'supplier' not in pkg_cols:
            c.execute("ALTER TABLE packaging_components ADD COLUMN supplier TEXT DEFAULT ''")

        name = data['component_name'].strip()
        qty = float(data.get('quantity', 0) or 0)
        cost = float(data.get('cost_per_unit', 0) or 0)
        supplier = data.get('supplier', '')
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        notes = data.get('notes', 'Initial stock')

        c.execute("INSERT INTO packaging_components (component_name, current_qty, cost_per_unit, supplier) VALUES (?, ?, ?, ?)",
                  (name, qty, cost, supplier))
        component_id = c.lastrowid
        c.execute("INSERT INTO packaging_movements (date, component_name, movement_type, quantity, unit_cost, reference_type, notes) VALUES (?, ?, 'IN', ?, ?, 'Initial', ?)",
                  (date, name, qty, cost, notes))
        try:
            create_packaging_lot(conn, name, qty, cost, date, 'Initial', component_id, notes)
        except Exception:
            pass
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Component added successfully', 'id': component_id})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Component already exists'}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sync-to-sheets/consumption', methods=['POST'])
def sync_consumption_to_sheets():
    try:
        payload = request.json or {}
        month = payload.get('month', datetime.now().strftime('%Y-%m'))
        product_filter = payload.get('products', '')
        # Reuse DB logic from monthly consumption
        conn = get_db()
        c = conn.cursor()
        start_date = f"{month}-01"
        from calendar import monthrange
        year, mon = map(int, month.split('-'))
        last_day = monthrange(year, mon)[1]
        end_date = f"{month}-{last_day:02d}"

        if product_filter:
            products = [p.strip() for p in product_filter.split(',')]
        else:
            c.execute("SELECT DISTINCT product_name FROM bulk_production WHERE date >= ? AND date <= ?", (start_date, end_date))
            products = [row[0] for row in c.fetchall()]

        # Build rows: Product | Type | Name | Qty Used | Unit Cost | Subtotal
        rows = []
        for product_name in products:
            # Raw materials
            c.execute("SELECT id FROM products WHERE name = ? COLLATE NOCASE", (product_name,))
            product_row = c.fetchone()
            if product_row:
                product_id = product_row['id']
                # Instead of using current cost_per_kg, derive consumption allocations from rm_stock_movements (OUT) grouped by unit_cost
                c.execute("SELECT SUM(batch_size_kg) as total_kg FROM bulk_production WHERE product_name = ? AND date >= ? AND date <= ?", (product_name, start_date, end_date))
                total_production = c.fetchone()['total_kg'] or 0
                if total_production > 0:
                    # find rm_stock_movements OUT for this production month referencing Production
                    c.execute("SELECT ingredient_name, unit_cost, SUM(quantity_kg) as qty_used FROM rm_stock_movements WHERE movement_type = 'OUT' AND reference_type = 'Production' AND date >= ? AND date <= ? GROUP BY ingredient_name, unit_cost", (start_date, end_date))
                    for row in c.fetchall():
                        ing_name = row['ingredient_name']
                        unit_cost = row['unit_cost'] or 0
                        kg_used = row['qty_used'] or 0
                        subtotal = kg_used * unit_cost
                        rows.append([product_name, 'Raw Material', ing_name, round(kg_used, 4), round(unit_cost, 4), round(subtotal, 2)])
            # Packaging used during filling: use packaging_movements OUT linked to filling operations and group by unit_cost
            c.execute("""
                SELECT pm.component_name, pm.unit_cost, SUM(pm.quantity) as qty
                FROM packaging_movements pm
                JOIN filling_operations fo ON pm.reference_type = 'Filling' AND pm.reference_id = fo.id
                WHERE fo.product_name = ? AND fo.date >= ? AND fo.date <= ? AND pm.movement_type = 'OUT'
                GROUP BY pm.component_name, pm.unit_cost
            """, (product_name, start_date, end_date))
            for prow in c.fetchall():
                comp = prow['component_name']
                unit_cost = prow['unit_cost'] or 0
                qty = prow['qty'] or 0
                rows.append([product_name, 'Packaging', comp, round(qty, 4), round(unit_cost, 4), round(qty * unit_cost, 2)])

        # Write to Google Sheets
        try:
            from google_sheets_sync import get_sheets_client, get_or_create_worksheet, write_tab, GOOGLE_SHEET_ID
        except Exception as e:
            conn.close()
            return jsonify({'success': False, 'error': 'Google Sheets sync not available: ' + str(e)}), 500
        client = get_sheets_client()
        sheet = client.open_by_key(__import__('config').GOOGLE_SHEET_ID)
        ws = get_or_create_worksheet(sheet, f'Consumption {month}')
        header = ['Product', 'Type', 'Name', 'Qty Used', 'Unit Cost', 'Subtotal']
        write_tab(ws, header, rows)
        conn.close()
        return jsonify({'success': True, 'message': f'Consumption costs synced ({len(rows)} rows) to sheet Consumption {month}'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sync-to-sheets/wastage', methods=['POST'])
def sync_wastage_to_sheets():
    try:
        payload = request.json or {}
        month = payload.get('month', datetime.now().strftime('%Y-%m'))
        product_filter = payload.get('products', '')
        conn = get_db()
        c = conn.cursor()
        start_date = f"{month}-01"
        from calendar import monthrange
        year, mon = map(int, month.split('-'))
        last_day = monthrange(year, mon)[1]
        end_date = f"{month}-{last_day:02d}"

        if product_filter:
            products = [p.strip() for p in product_filter.split(',')]
        else:
            c.execute("SELECT DISTINCT product_name FROM bulk_production WHERE date >= ? AND date <= ?", (start_date, end_date))
            products = [row[0] for row in c.fetchall()]

        rows = []
        for product_name in products:
            c.execute("SELECT kg_to_litre_factor FROM products WHERE name = ? COLLATE NOCASE", (product_name,))
            prow = c.fetchone()
            factor = prow['kg_to_litre_factor'] if prow else 1.0
            # Production wastage
            c.execute("SELECT SUM(batch_size_kg) as planned, SUM(actual_yield_kg) as actual FROM bulk_production WHERE product_name = ? AND date >= ? AND date <= ?", (product_name, start_date, end_date))
            prod = c.fetchone()
            planned = prod['planned'] or 0
            actual = prod['actual'] or 0
            loss_kg = planned - actual
            rows.append([product_name, 'Production', '', round(planned,4), round(actual,4), round(loss_kg,4), round(loss_kg * factor,4), '', '', ''])
            # Filling wastage per pack size — includes both poured-vs-in-bottles wastage AND bottle fill difference
            c.execute("SELECT SUM(bulk_poured_kg) as poured, SUM(bulk_used_kg) as in_bottles, SUM(theoretical_bottles) as theoretical, SUM(actual_bottles_filled) as actual_bottles, pack_size_kg FROM filling_operations WHERE product_name = ? AND date >= ? AND date <= ? GROUP BY pack_size_kg", (product_name, start_date, end_date))
            for row in c.fetchall():
                poured = row['poured'] or 0
                in_bottles = row['in_bottles'] or 0
                theoretical = row['theoretical'] or 0
                actual_bottles = row['actual_bottles'] or 0
                waste = poured - in_bottles
                pack_size = row['pack_size_kg'] or 0
                # Bottle fill difference: positive means under-filled (lost bottles)
                bottle_diff = theoretical - actual_bottles
                bottle_diff_kg = bottle_diff * pack_size if pack_size > 0 else 0
                bottle_diff_litres = bottle_diff_kg * factor
                rows.append([product_name, 'Filling', pack_size,
                              round(poured,4), round(in_bottles,4),
                              round(waste,4), round(waste * factor,4),
                              bottle_diff, round(bottle_diff_kg,4), round(bottle_diff_litres,4)])

        # Write to sheets
        try:
            from google_sheets_sync import get_sheets_client, get_or_create_worksheet, write_tab
        except Exception as e:
            conn.close()
            return jsonify({'success': False, 'error': 'Google Sheets sync not available: ' + str(e)}), 500
        client = get_sheets_client()
        sheet = client.open_by_key(__import__('config').GOOGLE_SHEET_ID)
        ws = get_or_create_worksheet(sheet, f'Wastage {month}')
        header = ['Product', 'Stage', 'Pack Size (kg)', 'Poured (kg)', 'In Bottles (kg)',
                  'Liquid Waste (kg)', 'Liquid Waste (litres)',
                  'Bottle Diff (bottles)', 'Bottle Diff (kg)', 'Bottle Diff (litres)']
        write_tab(ws, header, rows)
        conn.close()
        return jsonify({'success': True, 'message': f'Wastage report synced ({len(rows)} rows) to sheet Wastage {month}'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/packaging/components/restock', methods=['POST'])
def restock_packaging_component():
    try:
        data = request.json
        conn = get_db()
        c = conn.cursor()
        qty = float(data.get('quantity', 0) or 0)
        if qty <= 0:
            conn.close()
            return jsonify({'success': False, 'error': 'Quantity must be greater than 0'}), 400
        unit_cost = float(data.get('unit_cost', 0) or 0)
        if unit_cost <= 0:
            conn.close()
            return jsonify({'success': False, 'error': 'Please provide a valid cost per unit for this restock'}), 400
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        notes = data.get('notes', 'Restocked')
        supplier = data.get('supplier', '').strip()
        # Resolve component
        if 'component_id' in data and data['component_id']:
            c.execute("SELECT component_name FROM packaging_components WHERE id = ?", (data['component_id'],))
            row = c.fetchone()
            if not row:
                conn.close()
                return jsonify({'success': False, 'error': 'Component id not found'}), 404
            comp_name = row['component_name']
            c.execute("UPDATE packaging_components SET current_qty = current_qty + ? WHERE id = ?", (qty, data['component_id']))
            if supplier:
                c.execute("UPDATE packaging_components SET supplier = ? WHERE id = ?", (supplier, data['component_id']))
        else:
            comp_name = data.get('component_name')
            c.execute("UPDATE packaging_components SET current_qty = current_qty + ? WHERE component_name = ? COLLATE NOCASE", (qty, comp_name))
            if supplier:
                c.execute("UPDATE packaging_components SET supplier = ? WHERE component_name = ? COLLATE NOCASE", (supplier, comp_name))
        c.execute("""
            INSERT INTO packaging_movements
            (date, component_name, movement_type, quantity, unit_cost, reference_type, notes)
            VALUES (?, ?, 'IN', ?, ?, 'Restock', ?)
        """, (date, comp_name, qty, unit_cost, notes))
        create_packaging_lot(conn, comp_name, qty, unit_cost, date, 'Restock', None, notes)
        c.execute("SELECT current_qty FROM packaging_components WHERE component_name = ? COLLATE NOCASE", (comp_name,))
        row = c.fetchone()
        new_qty = row['current_qty'] if row else None
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': f'Restocked {qty} of {comp_name} at ₹{unit_cost}/unit', 'component_name': comp_name, 'new_qty': new_qty})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/packaging/components/update', methods=['POST'])
def update_packaging_component():
    """Update packaging component fields (quantity, cost, supplier) and record movement history"""
    try:
        data = request.json
        name = data['component_name']
        new_qty = data.get('current_qty', None)
        new_cost = data.get('cost_per_unit', None)
        new_supplier = data.get('supplier', None)
        lot_id = data.get('lot_id', None)
        conn = get_db()
        c = conn.cursor()
        # Find component case-insensitively
        c.execute("SELECT id, component_name, current_qty, cost_per_unit, supplier FROM packaging_components WHERE component_name = ? COLLATE NOCASE", (name,))
        row = c.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Component not found'}), 404
        comp_name = row['component_name']
        old_qty = float(row['current_qty'] or 0)
        old_cost = float(row['cost_per_unit'] or 0)
        # Update fields
        updates = []
        params = []
        if new_qty is not None:
            updates.append('current_qty = ?')
            params.append(float(new_qty))
        if new_cost is not None:
            updates.append('cost_per_unit = ?')
            params.append(float(new_cost))
        if new_supplier is not None:
            updates.append('supplier = ?')
            params.append(new_supplier)
        if updates:
            params.append(row['id'])
            c.execute(f"UPDATE packaging_components SET {', '.join(updates)} WHERE id = ?", params)
        # Record movements if qty changed
        if new_qty is not None:
            qty_diff = float(new_qty) - old_qty
            if abs(qty_diff) > 0.0001:
                mtype = 'IN' if qty_diff > 0 else 'OUT'
                lot_cost = 0.0
                if lot_id:
                    c.execute("SELECT unit_cost FROM packaging_lots WHERE id = ?", (lot_id,))
                    lot_row = c.fetchone()
                    if lot_row:
                        lot_cost = float(lot_row['unit_cost'] or 0)
                        # Adjust the specific lot's qty_remaining
                        c.execute("UPDATE packaging_lots SET qty_remaining = qty_remaining + ? WHERE id = ?", (qty_diff, lot_id))
                else:
                    lot_cost = old_cost
                note = f"{comp_name} of cost \u20b9{round(lot_cost, 2)} changed from {round(old_qty, 4)} to {round(float(new_qty), 4)}"
                c.execute("""
                    INSERT INTO packaging_movements
                    (date, component_name, movement_type, quantity, unit_cost, reference_type, reference_id, notes)
                    VALUES (?, ?, ?, ?, ?, 'Manual', NULL, ?)
                """, (datetime.now().strftime('%Y-%m-%d'), comp_name, mtype, abs(qty_diff), lot_cost, note))
        # Record cost change
        if new_cost is not None and abs(float(new_cost) - old_cost) > 0.0001:
            c.execute("""
                INSERT INTO packaging_movements
                (date, component_name, movement_type, quantity, reference_type, reference_id, notes)
                VALUES (?, ?, 'PRICE_UPDATE', 0, 'Adjustment', NULL, ?)
            """, (datetime.now().strftime('%Y-%m-%d'), comp_name, f'Cost changed from {old_cost} to {new_cost}'))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Component updated and history recorded'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/packaging/report-damage', methods=['POST'])
def report_packaging_damage():
    try:
        data = request.json
        name = data['component_name']
        qty = float(data['quantity'])
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        notes = data.get('notes', 'Reported damage')
        lot_id = data.get('lot_id')
        conn = get_db()
        c = conn.cursor()
        # Subtract from inventory (case-insensitive)
        c.execute("UPDATE packaging_components SET current_qty = current_qty - ? WHERE component_name = ? COLLATE NOCASE", (qty, name))
        # Subtract from the specific lot if provided, otherwise FIFO
        if lot_id:
            c.execute("UPDATE packaging_lots SET qty_remaining = qty_remaining - ? WHERE id = ?", (qty, lot_id))
        else:
            # FIFO deduction from lots
            remaining = qty
            c.execute("SELECT id, qty_remaining FROM packaging_lots WHERE component_name = ? COLLATE NOCASE AND qty_remaining > 0 ORDER BY received_date ASC, id ASC", (name,))
            for lot in c.fetchall():
                if remaining <= 0:
                    break
                use = min(float(lot['qty_remaining'] or 0), remaining)
                c.execute("UPDATE packaging_lots SET qty_remaining = qty_remaining - ? WHERE id = ?", (use, lot['id']))
                remaining -= use
        c.execute("""
            INSERT INTO packaging_movements
            (date, component_name, movement_type, quantity, reference_type, notes)
            VALUES (?, ?, 'DAMAGE', ?, 'Damage', ?)
        """, (date, name, qty, notes))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Damage recorded and inventory updated'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/packaging/movements')
def get_packaging_movements():
    try:
        conn = get_db()
        c = conn.cursor()
        component_filter = request.args.get('components', '')
        start_date = request.args.get('start_date', '')
        end_date = request.args.get('end_date', '')
        month = request.args.get('month', '')  # YYYY-MM
        movement_types = request.args.get('types', '')  # comma-separated, e.g. IN,OUT,DAMAGE

        query = "SELECT date, component_name, movement_type, quantity, unit_cost, reference_type, notes, created_at FROM packaging_movements WHERE 1=1"
        params = []
        if component_filter:
            comps = [c_.strip() for c_ in component_filter.split(',')]
            placeholders = ','.join(['?' for _ in comps])
            query += f" AND component_name COLLATE NOCASE IN ({placeholders})"
            params.extend(comps)
        if movement_types:
            mts = [m.strip().upper() for m in movement_types.split(',') if m.strip()]
            if mts:
                placeholders = ','.join(['?' for _ in mts])
                query += f" AND movement_type IN ({placeholders})"
                params.extend(mts)
        if month:
            query += " AND date LIKE ?"
            params.append(f"{month}-%")
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date DESC, id DESC LIMIT 500"
        c.execute(query, params)
        movements = [dict(row) for row in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': movements})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================================
# ROUTES - FILLING (REVISED)
# ============================================================================

@app.route('/filling')
def filling():
    return render_template('filling.html')

@app.route('/api/filling/calculate-theoretical', methods=['POST'])
def calculate_theoretical_bottles():
    """Calculate theoretical bottles from batch yield and pack size"""
    try:
        data = request.json
        batch_number = data['batch_number']
        pack_size_kg = float(data['pack_size_kg'])
        bulk_poured_override = float(data.get('bulk_poured_kg')) if data.get('bulk_poured_kg') is not None and str(data.get('bulk_poured_kg')) != '' else None
        conn = get_db()
        c = conn.cursor()
        # Get yield for this batch
        c.execute("""
            SELECT actual_yield_kg FROM bulk_production WHERE batch_number = ?
        """, (batch_number,))
        batch = c.fetchone()
        if not batch:
            return jsonify({'success': False, 'error': 'Batch not found'}), 404
        yield_kg = batch['actual_yield_kg']
        # Get already used bulk
        c.execute("""
            SELECT COALESCE(SUM(bulk_used_kg), 0) as used
            FROM filling_operations
            WHERE batch_number = ?
        """, (batch_number,))
        used = c.fetchone()['used']
        remaining_kg = yield_kg - used
        if remaining_kg < 0:
            remaining_kg = 0
        # Theoretical bottles should be calculated based on bulk_poured (what was poured into the machine)
        if bulk_poured_override is not None:
            theoretical = int(bulk_poured_override / pack_size_kg) if pack_size_kg > 0 else 0
        else:
            theoretical = int(remaining_kg / pack_size_kg) if pack_size_kg > 0 else 0
        conn.close()
        return jsonify({
            'success': True,
            'data': {
                'total_yield_kg': round(yield_kg, 3),
                'already_used_kg': round(used, 3),
                'remaining_kg': round(remaining_kg, 3),
                'theoretical_bottles': theoretical
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/filling/confirm', methods=['POST'])
def confirm_filling():
    try:
        data = request.json
        date = data['date']
        product_name = data['product_name']
        batch_number = data['batch_number']
        pack_size_kg = float(data['pack_size_kg'])
        bulk_used = float(data['bulk_used_kg'])
        actual_bottles = int(data['actual_bottles_filled'])
        packaging_usage = data['packaging_usage']  # [{component_name, quantity}]
        notes = data.get('notes', '')
        conn = get_db()
        c = conn.cursor()
        # Calculate theoretical
        theoretical = int(bulk_used / pack_size_kg) if pack_size_kg > 0 else 0
        diff = actual_bottles - theoretical
        diff_pct = (diff / theoretical * 100) if theoretical > 0 else 0
        # Insert filling record
        bulk_poured = float(data.get('bulk_poured_kg', bulk_used))  # Add this line before INSERT
        c.execute("""
         INSERT INTO filling_operations
    (date, product_name, batch_number, pack_size_kg, bulk_used_kg, bulk_poured_kg,
     actual_bottles_filled, theoretical_bottles, bottles_diff, bottles_diff_percentage, notes)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (date, product_name, batch_number, pack_size_kg, bulk_used, bulk_poured,actual_bottles, theoretical, diff, diff_pct, notes))
        filling_id = c.lastrowid
        # Pre-check packaging stock before consuming
        for pkg in packaging_usage:
            component_name = pkg['component_name']
            quantity = float(pkg['quantity'])
            c.execute("SELECT current_qty FROM packaging_components WHERE component_name = ? COLLATE NOCASE", (component_name,))
            comp_row = c.fetchone()
            available = float(comp_row['current_qty'] or 0) if comp_row else 0
            if available < quantity:
                conn.close()
                return jsonify({'success': False, 'error': f'Not enough {component_name} for filling. Available: {available}, needed: {quantity}'}), 400
        # Record packaging usage
        for pkg in packaging_usage:
            component_name = pkg['component_name']
            quantity = float(pkg['quantity'])
            # Consume using FIFO packaging lots; this will update current_qty and insert packaging_movements
            allocs = consume_packaging_fifo(conn, component_name, quantity, date, 'Filling', filling_id, f'{int(quantity)} units of {component_name} used for filling {product_name} batch {batch_number}')
            # Record usage (aggregate quantity used)
            c.execute("INSERT INTO filling_packaging_usage (filling_id, component_name, quantity_used) VALUES (?, ?, ?)", (filling_id, component_name, quantity))
        conn.commit()
        conn.close()
        return jsonify({
            'success': True,
            'message': 'Filling recorded successfully',
            'data': {
                'filling_id': filling_id,
                'theoretical_bottles': theoretical,
                'bottles_diff': diff,
                'bottles_diff_percentage': round(diff_pct, 2)
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/filling/history')
def get_filling_history():
    try:
        conn = get_db()
        c = conn.cursor()
        # Accept optional filters: products (comma-separated), month (YYYY-MM)
        products_q = request.args.get('products', request.args.get('product', '')).strip()
        month = request.args.get('month', '').strip()
        query = "SELECT fo.* FROM filling_operations fo WHERE 1=1"
        params = []
        products = [p.strip() for p in products_q.split(',') if p.strip()]
        if products:
            placeholders = ','.join(['?' for _ in products])
            query += f" AND LOWER(fo.product_name) IN ({placeholders})"
            params.extend([p.lower() for p in products])
        if month:
            query += " AND fo.date LIKE ?"
            params.append(f"{month}-%")
        query += " ORDER BY fo.date DESC, fo.id DESC"
        c.execute(query, params)
        history = []
        for row in c.fetchall():
            record = dict(row)
            # Get packaging used
            c.execute("""
                SELECT component_name, quantity_used
                FROM filling_packaging_usage
                WHERE filling_id = ?
            """, (record['id'],))
            record['packaging_used'] = [dict(r) for r in c.fetchall()]
            # Get sample count for this filling's product and pack size
            c.execute("SELECT COALESCE(SUM(quantity),0) as samples FROM samples WHERE product_name = ? AND batch_number = ? AND pack_size_kg = ?", (record['product_name'], record['batch_number'], record['pack_size_kg']))
            record['samples_sent'] = int(c.fetchone()['samples'] or 0)
            # Get damages for this filling's product and pack size
            c.execute("SELECT COALESCE(SUM(quantity),0) as damages FROM filling_damages WHERE product_name = ? AND batch_number = ? AND pack_size_kg = ?", (record['product_name'], record['batch_number'], record['pack_size_kg']))
            record['damages_reported'] = int(c.fetchone()['damages'] or 0)
            history.append(record)
        conn.close()
        # optional status filtering: status=sample|damage|both|none (default all)
        status = request.args.get('status', '').strip().lower()
        if status:
            filtered = []
            for r in history:
                has_sample = (r.get('samples_sent', 0) or 0) > 0
                has_damage = (r.get('damages_reported', 0) or 0) > 0
                if status == 'sample' and has_sample:
                    filtered.append(r)
                elif status == 'damage' and has_damage:
                    filtered.append(r)
                elif status == 'both' and has_sample and has_damage:
                    filtered.append(r)
                elif status == 'none' and not has_sample and not has_damage:
                    filtered.append(r)
            history = filtered
        return jsonify({'success': True, 'data': history})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/filling/report-damage', methods=['POST'])
def report_filling_damage():
    try:
        data = request.json
        product = data['product_name']
        qty = int(data['quantity'])
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        batch = data.get('batch_number')
        pack = data.get('pack_size_kg')
        reason = data.get('reason', 'Reported damage')
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO filling_damages (date, product_name, batch_number, pack_size_kg, quantity, reason) VALUES (?, ?, ?, ?, ?, ?)", (date, product, batch, pack, qty, reason))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Filling damage recorded'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/filling/current-stock')
def api_filling_current_stock():
    """Return current filled bottles per product and pack size left to be boxed.
       Optional filters: product, month (YYYY-MM) to limit fillings considered.
    """
    try:
        products_q = request.args.get('products', request.args.get('product', '')).strip()
        month = request.args.get('month', '').strip()
        conn = get_db()
        c = conn.cursor()
        # Sum filled bottles
        query = "SELECT product_name, COALESCE(pack_size_kg, 0) as pack_size_kg, SUM(actual_bottles_filled) as total_filled FROM filling_operations WHERE 1=1"
        params = []
        products = [p.strip() for p in products_q.split(',') if p.strip()]
        if products:
            placeholders = ','.join(['?' for _ in products])
            query += f" AND LOWER(product_name) IN ({placeholders})"
            params.extend([p.lower() for p in products])
        if month:
            query += " AND date LIKE ?"
            params.append(f"{month}-%")
        query += " GROUP BY product_name, pack_size_kg"
        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]
        results = []
        for r in rows:
            pname = r['product_name']
            psize = r['pack_size_kg']
            total_filled = int(r['total_filled'] or 0)
            # boxed
            if psize and float(psize) > 0:
                c.execute("SELECT COALESCE(SUM(units_in_box),0) as boxed FROM boxing_products bp JOIN boxing_operations bo ON bo.id = bp.boxing_id WHERE bp.product_name = ? AND bp.pack_size_kg = ?", (pname, psize))
            else:
                c.execute("SELECT COALESCE(SUM(units_in_box),0) as boxed FROM boxing_products bp JOIN boxing_operations bo ON bo.id = bp.boxing_id WHERE bp.product_name = ?", (pname,))
            boxed = int(c.fetchone()['boxed'] or 0)
            # samples
            if psize and float(psize) > 0:
                c.execute("SELECT COALESCE(SUM(quantity),0) as samples FROM samples WHERE product_name = ? AND pack_size_kg = ?", (pname, psize))
            else:
                c.execute("SELECT COALESCE(SUM(quantity),0) as samples FROM samples WHERE product_name = ?", (pname,))
            samples = int(c.fetchone()['samples'] or 0)
            # damages
            if psize and float(psize) > 0:
                c.execute("SELECT COALESCE(SUM(quantity),0) as damages FROM filling_damages WHERE product_name = ? AND pack_size_kg = ?", (pname, psize))
            else:
                c.execute("SELECT COALESCE(SUM(quantity),0) as damages FROM filling_damages WHERE product_name = ?", (pname,))
            damages = int(c.fetchone()['damages'] or 0)
            remaining = total_filled - boxed - samples - damages
            results.append({'product_name': pname, 'pack_size_kg': psize, 'total_filled': total_filled, 'boxed': boxed, 'samples': samples, 'damages': damages, 'remaining_to_box': remaining})
        conn.close()
        return jsonify({'success': True, 'data': results})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/samples/add', methods=['POST'])
def add_sample():
    try:
        data = request.json
        conn = get_db()
        c = conn.cursor()
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        product = data['product_name']
        batch = data.get('batch_number')
        pack = data.get('pack_size_kg')
        qty = int(data.get('quantity', 0))
        reason = data.get('reason', '')
        c.execute("INSERT INTO samples (date, product_name, batch_number, pack_size_kg, quantity, reason) VALUES (?, ?, ?, ?, ?, ?)",
                  (date, product, batch, pack, qty, reason))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Sample recorded'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/samples/history')
def samples_history():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, date, product_name, batch_number, pack_size_kg, quantity, reason FROM samples ORDER BY date DESC, id DESC LIMIT 500")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/samples/delete/<int:id>', methods=['DELETE'])
def delete_sample(id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM samples WHERE id = ?", (id,))
        if not c.fetchone():
            conn.close()
            return jsonify({'success': False, 'error': 'Sample not found'}), 404
        c.execute("DELETE FROM samples WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Sample deleted'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/filling/damage/delete/<int:id>', methods=['DELETE'])
def delete_filling_damage(id):
    """Delete a filling damage record and restore the bottle count"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM filling_damages WHERE id = ?", (id,))
        if not c.fetchone():
            conn.close()
            return jsonify({'success': False, 'error': 'Damage record not found'}), 404
        c.execute("DELETE FROM filling_damages WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Damage record deleted and bottles restored'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/filling/damage-history')
def get_filling_damage_history():
    """Return filling damage records with optional filters"""
    try:
        conn = get_db()
        c = conn.cursor()
        month = request.args.get('month', '').strip()
        product = request.args.get('product', '').strip()
        query = "SELECT id, date, product_name, batch_number, pack_size_kg, quantity, reason FROM filling_damages WHERE 1=1"
        params = []
        if month:
            query += " AND date LIKE ?"
            params.append(f"{month}-%")
        if product:
            query += " AND LOWER(product_name) = LOWER(?)"
            params.append(product)
        query += " ORDER BY date DESC, id DESC"
        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/filling/pack-sizes')
def filling_pack_sizes():
    """Return distinct pack sizes available for a product from filling records"""
    try:
        product = request.args.get('product')
        conn = get_db()
        c = conn.cursor()
        if not product:
            c.execute("SELECT DISTINCT pack_size_kg FROM filling_operations ORDER BY pack_size_kg DESC")
            rows = [r['pack_size_kg'] for r in c.fetchall()]
        else:
            c.execute("SELECT DISTINCT pack_size_kg FROM filling_operations WHERE product_name = ? ORDER BY pack_size_kg DESC", (product,))
            rows = [r['pack_size_kg'] for r in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/boxing/delete/<int:id>', methods=['DELETE'])
def delete_boxing(id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM boxing_operations WHERE id = ?", (id,))
        row = c.fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Boxing record not found'}), 404
        # Restore packaging components
        c.execute("SELECT component_name, quantity_used FROM boxing_packaging_usage WHERE boxing_id = ?", (id,))
        for r in c.fetchall():
            comp = r['component_name']
            qty = r['quantity_used']
            c.execute("UPDATE packaging_components SET current_qty = current_qty + ? WHERE component_name = ? COLLATE NOCASE", (qty, comp))
            qty_display = int(qty) if qty == int(qty) else qty
            c.execute("""
                INSERT INTO packaging_movements
                (date, component_name, movement_type, quantity, reference_type, reference_id, notes)
                VALUES (?, ?, 'IN', ?, 'Boxing Deletion', ?, ?)
            """, (datetime.now().strftime('%Y-%m-%d'), comp, qty, id, f'{qty_display} pieces of {comp} restored from boxing'))
        # Delete usage and product rows, then the boxing record
        c.execute("DELETE FROM boxing_packaging_usage WHERE boxing_id = ?", (id,))
        c.execute("DELETE FROM boxing_products WHERE boxing_id = ?", (id,))
        c.execute("DELETE FROM boxing_operations WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Boxing deleted and materials restored'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/filling/batch-summary/<batch_number>')
def get_filling_batch_summary(batch_number):
    """Get combined filling summary for a batch"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT 
                COALESCE(SUM(bulk_used_kg),0) as total_bulk_used,
                COALESCE(SUM(actual_bottles_filled),0) as total_bottles,
                COALESCE(SUM(theoretical_bottles),0) as total_theoretical,
                pack_size_kg
            FROM filling_operations
            WHERE batch_number = ?
            GROUP BY pack_size_kg
        """, (batch_number,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================================
# ROUTES - BOXING (REVISED)
# ============================================================================

@app.route('/boxing')
def boxing():
    return render_template('boxing.html')


@app.route('/finished-goods')
def finished_goods():
    return render_template('finished_goods.html')


@app.route('/api/finished-goods')
def api_finished_goods():
    try:
        conn = get_db()
        c = conn.cursor()
        # Filters
        month = request.args.get('month', '')
        # accept either 'product' (single or comma-separated) or 'products' param
        product_filter = request.args.get('products', request.args.get('product', '')).strip()

        # Select boxing operations for month (or all) optionally filtered by product inclusion
        # fetch boxing ops for month (or all) then filter by exact product set match if products provided
        if month:
            like = f"{month}%"
            c.execute("SELECT id, date, boxes_made FROM boxing_operations WHERE date LIKE ? ORDER BY date DESC", (like,))
        else:
            c.execute("SELECT id, date, boxes_made FROM boxing_operations ORDER BY date DESC")

        ops = [dict(r) for r in c.fetchall()]
        results = []
        # prepare selected products set if provided
        selected = [p.strip().lower() for p in product_filter.split(',') if p.strip()]
        selected_set = set(selected) if selected else None

        for op in ops:
            boxing_id = op['id']
            boxes_made = int(op.get('boxes_made') or 0)
            # fetch products for this boxing
            c.execute("SELECT product_name, pack_size_kg, units_in_box FROM boxing_products WHERE boxing_id = ?", (boxing_id,))
            parts = [dict(r) for r in c.fetchall()]
            # if filtering by products, include only boxing entries whose product name set exactly matches the selected set
            if selected_set is not None:
                names = set([p['product_name'].lower() for p in parts])
                if names != selected_set:
                    continue
            product_names = ', '.join([p['product_name'] for p in parts])
            pack_sizes = ', '.join([str(p.get('pack_size_kg') or '-') for p in parts])

            per_product = []
            total_bottles_display_items = []
            for p in parts:
                pname = p['product_name']
                pack = p.get('pack_size_kg')
                total_bottles = int(p.get('units_in_box') or 0)  # stored as total bottles for this boxing
                units_per_box = (total_bottles // boxes_made) if boxes_made > 0 else 0
                total_boxed = total_bottles
                # shipped bottles for this product and boxing
                c.execute("SELECT COALESCE(SUM(units_sent),0) as shipped FROM finished_goods_shipments WHERE boxing_id = ? AND LOWER(product_name) = LOWER(?)", (boxing_id, pname))
                shipped = int(c.fetchone()['shipped'] or 0)
                remaining = total_boxed - shipped
                per_product.append({'product_name': pname, 'pack_size_kg': pack, 'units_per_box': units_per_box, 'total_boxed': total_boxed, 'shipped_bottles': shipped, 'remaining_bottles': remaining})
                total_bottles_display_items.append(f"{pname}: {remaining}")

            total_bottles_display = ', '.join(total_bottles_display_items)

            # subtract boxes already dispatched for this boxing
            c.execute("SELECT COALESCE(SUM(boxes_sent),0) as boxes_sent_total FROM finished_goods_shipments WHERE boxing_id = ?", (boxing_id,))
            boxes_sent_total = int(c.fetchone()['boxes_sent_total'] or 0)
            display_boxes = int(boxes_made or 0) - boxes_sent_total
            results.append({
                'boxing_id': boxing_id,
                'date': op.get('date'),
                'product_names': product_names,
                'pack_sizes': pack_sizes,
                'boxes_count': display_boxes,
                'total_bottles_display': total_bottles_display,
                'products': per_product
            })

        conn.close()
        return jsonify({'success': True, 'data': results})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/finished-goods/send-out', methods=['POST'])
def finished_goods_send_out():
    try:
        data = request.json
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        boxing_id = data.get('boxing_id')
        product_name = data.get('product_name')
        pack_size = data.get('pack_size_kg')
        boxes_sent = int(data.get('boxes_sent', 0) or 0)
        units_sent = int(data.get('units_sent', 0) or 0)
        notes = data.get('notes', '')
        invoice_number = data.get('invoice_number')

        # remark is mandatory
        if not notes or not notes.strip():
            return jsonify({'success': False, 'error': 'Remark/notes are required for shipments'}), 400

        conn = get_db()
        c = conn.cursor()

        # If boxing_id provided but no product_name, treat as boxing-level shipment: insert one shipment row per product in that boxing
        if boxing_id and not product_name:
            # fetch products for boxing
            c.execute("SELECT product_name, pack_size_kg, units_in_box FROM boxing_products WHERE boxing_id = ?", (boxing_id,))
            parts = [dict(r) for r in c.fetchall()]
            if not parts:
                conn.close()
                return jsonify({'success': False, 'error': 'No products found for given boxing id'}), 400
            # fetch boxes_made for this boxing to compute units per box
            c.execute("SELECT boxes_made FROM boxing_operations WHERE id = ?", (boxing_id,))
            row = c.fetchone()
            boxes_made_val = int(row['boxes_made'] or 0) if row else 0
            let_first = True
            for p in parts:
                total_bottles = int(p.get('units_in_box') or 0)
                units_per_box = int(total_bottles // boxes_made_val) if boxes_made_val > 0 else 0
                units = units_per_box * boxes_sent
                # only store boxes_sent for the first product row to avoid double-counting
                bs = boxes_sent if let_first else 0
                c.execute("INSERT INTO finished_goods_shipments (date, boxing_id, product_name, pack_size_kg, boxes_sent, units_sent, notes, invoice_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (date, boxing_id, p['product_name'], p['pack_size_kg'], bs, units, notes, invoice_number))
                let_first = False
        else:
            # single-product shipment (legacy support)
            c.execute("INSERT INTO finished_goods_shipments (date, boxing_id, product_name, pack_size_kg, boxes_sent, units_sent, notes, invoice_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (date, boxing_id, product_name, pack_size, boxes_sent, units_sent, notes, invoice_number))

        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Shipment recorded'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/finished-goods/shipments')
def api_finished_goods_shipments():
    """Return list of finished goods shipments; optional filter by month (YYYY-MM)"""
    try:
        month = request.args.get('month', '')
        conn = get_db()
        c = conn.cursor()
        if month:
            like = f"{month}%"
            c.execute("SELECT id, date, boxing_id, product_name, pack_size_kg, boxes_sent, units_sent, notes, invoice_number FROM finished_goods_shipments WHERE date LIKE ? ORDER BY date DESC", (like,))
        else:
            c.execute("SELECT id, date, boxing_id, product_name, pack_size_kg, boxes_sent, units_sent, notes, invoice_number FROM finished_goods_shipments ORDER BY date DESC")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/finished-goods/shipments/<int:id>', methods=['DELETE'])
def delete_finished_goods_shipment(id):
    try:
        conn = get_db()
        c = conn.cursor()
        # Check existence
        c.execute("SELECT * FROM finished_goods_shipments WHERE id = ?", (id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Shipment not found'}), 404
        # Deleting the shipment will automatically restore counts because inventory is computed from boxing and shipments only
        c.execute("DELETE FROM finished_goods_shipments WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Shipment deleted and counts restored'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/finished-goods/report')
def api_finished_goods_report():
    """Return breakdown of boxes for selected month and product set.
       Query params: month=YYYY-MM, products=comma,separated product names
    """
    try:
        month = request.args.get('month', datetime.now().strftime('%Y-%m'))
        products_q = request.args.get('products', '')
        products = [p.strip() for p in products_q.split(',') if p.strip()]

        # month range
        start_date = f"{month}-01"
        from calendar import monthrange
        year, mon = map(int, month.split('-'))
        last_day = monthrange(year, mon)[1]
        end_date = f"{month}-{last_day:02d}"

        conn = get_db()
        c = conn.cursor()

        # Find boxing_operations in month that exactly match the selected product set
        # If no products provided, return empty
        if not products:
            conn.close()
            return jsonify({'success': True, 'data': []})

        lower_products = [p.lower() for p in products]
        n = len(lower_products)

        # Build placeholders for IN clause
        placeholders = ','.join('?' for _ in lower_products)

        # Select boxing ids where number distinct products = n and all products are in the selected list
        query = f"""
            SELECT bo.id
            FROM boxing_operations bo
            JOIN boxing_products bp ON bp.boxing_id = bo.id
            WHERE bo.date >= ? AND bo.date <= ?
            GROUP BY bo.id
            HAVING COUNT(DISTINCT LOWER(bp.product_name)) = ?
               AND SUM(CASE WHEN LOWER(bp.product_name) IN ({placeholders}) THEN 1 ELSE 0 END) = ?
        """
        params = [start_date, end_date, n] + lower_products + [n]
        c.execute(query, params)
        boxing_ids = [r['id'] for r in c.fetchall()]

        if not boxing_ids:
            conn.close()
            return jsonify({'success': True, 'data': []})

        # Now aggregate breakdowns for matched boxing ids
        boxing_ids_place = ','.join('?' for _ in boxing_ids)

        # For single-product query we want boxes that contain only that product
        if n == 1:
            prod = products[0]
            # Group by units_per_box and pack_size
            q = f"""
                SELECT bp.units_in_box, bp.pack_size_kg, COALESCE(SUM(bo.boxes_made),0) as boxes_count,
                       COALESCE(SUM(bp.units_in_box),0) as total_bottles
                FROM boxing_products bp
                JOIN boxing_operations bo ON bo.id = bp.boxing_id
                WHERE bp.boxing_id IN ({boxing_ids_place}) AND LOWER(bp.product_name) = ?
                GROUP BY bp.units_in_box, bp.pack_size_kg
            """
            params = boxing_ids + [prod.lower()]
            c.execute(q, params)
            rows = [dict(r) for r in c.fetchall()]
            # subtract any boxes already sent for these product/pack_size groups within the matched boxing ids
            for row in rows:
                units = row.get('units_in_box')
                pack = row.get('pack_size_kg')
                # sum boxes_sent for shipments that reference boxing_ids and this product+pack
                q = f"SELECT COALESCE(SUM(fgs.boxes_sent),0) as sent FROM finished_goods_shipments fgs JOIN boxing_products bp ON bp.boxing_id = fgs.boxing_id WHERE LOWER(bp.product_name) = LOWER(?) AND bp.pack_size_kg = ? AND bp.boxing_id IN ({boxing_ids_place})"
                params = [prod.lower(), pack] + boxing_ids
                try:
                    c.execute(q, params)
                    sent = int(c.fetchone()['sent'] or 0)
                except Exception:
                    sent = 0
                # adjust boxes_count reported
                row['boxes_count'] = int(row.get('boxes_count') or 0) - sent
            conn.close()
            return jsonify({'success': True, 'data': rows})

        # For multi-product selection: return boxes_count and per-product breakdown
        # First get boxes_count (number of boxing_operations that match)
        c.execute(f"SELECT COUNT(*) as cnt FROM boxing_operations WHERE id IN ({boxing_ids_place})", boxing_ids)
        boxes_count = c.fetchone()['cnt']

        # Per-product breakdown across matched boxing ops
        q = f"""
            SELECT bp.product_name, bp.pack_size_kg, bp.units_in_box, COALESCE(SUM(bo.boxes_made),0) as boxes_count, COALESCE(SUM(bp.units_in_box),0) as total_bottles
            FROM boxing_products bp
            JOIN boxing_operations bo ON bo.id = bp.boxing_id
            WHERE bp.boxing_id IN ({boxing_ids_place})
            GROUP BY bp.product_name, bp.pack_size_kg, bp.units_in_box
        """
        c.execute(q, boxing_ids)
        breakdown = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'boxes_count': boxes_count, 'data': breakdown})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/boxing/confirm', methods=['POST'])
def confirm_boxing():
    try:
        data = request.json
        date = data['date']
        packaging_usage = data['packaging_usage']  # [{component_name, quantity}]
        notes = data.get('notes', '')
        boxes_made = int(data['boxes_made'])
        units_per_box = int(data.get('units_per_box') or 0)
        products = data['products']  # [{product_name, units}]
        conn = get_db()
        c = conn.cursor()
        # Validate that there are enough filled bottles for each product being boxed
        for prod in products:
            pname = prod['product_name']
            units_needed = int(prod['units'])
            pack_size = float(prod.get('pack_size_kg', 0) or 0)
            # calculate filled bottles for this product and pack size
            if pack_size > 0:
                c.execute("SELECT COALESCE(SUM(actual_bottles_filled), 0) as total FROM filling_operations WHERE product_name = ? AND pack_size_kg = ?", (pname, pack_size))
            else:
                c.execute("SELECT COALESCE(SUM(actual_bottles_filled), 0) as total FROM filling_operations WHERE product_name = ?", (pname,))
            total_filled = c.fetchone()['total']
            # Calculate already boxed units for this product filtered by pack size if provided
            if pack_size > 0:
                c.execute("SELECT COALESCE(SUM(units_in_box), 0) as boxed FROM boxing_products bp JOIN boxing_operations bo ON bo.id = bp.boxing_id WHERE bp.product_name = ? AND bp.pack_size_kg = ?", (pname, pack_size))
            else:
                c.execute("SELECT COALESCE(SUM(units_in_box), 0) as boxed FROM boxing_products bp JOIN boxing_operations bo ON bo.id = bp.boxing_id WHERE bp.product_name = ?", (pname,))
            already_boxed = c.fetchone()['boxed']
            # subtract samples already sent out for this product and pack size
            if pack_size > 0:
                c.execute("SELECT COALESCE(SUM(quantity),0) as samples FROM samples WHERE product_name = ? AND pack_size_kg = ?", (pname, pack_size))
            else:
                c.execute("SELECT COALESCE(SUM(quantity),0) as samples FROM samples WHERE product_name = ?", (pname,))
            total_samples = int(c.fetchone()['samples'] or 0)
            # subtract filling damages for this product and pack size
            if pack_size > 0:
                c.execute("SELECT COALESCE(SUM(quantity),0) as damages FROM filling_damages WHERE product_name = ? AND pack_size_kg = ?", (pname, pack_size))
            else:
                c.execute("SELECT COALESCE(SUM(quantity),0) as damages FROM filling_damages WHERE product_name = ?", (pname,))
            total_damages = int(c.fetchone()['damages'] or 0)
            # Available is filled - already boxed - samples for this pack size
            available = (total_filled or 0) - (already_boxed or 0) - (total_samples or 0) - (total_damages or 0)
            if units_needed > (available or 0):
                return jsonify({'success': False, 'error': f'Insufficient filled bottles for {pname}. Available: {available}'}), 400

        # Compute total units boxed and insert boxing record
        total_units = sum([int(p['units']) for p in products])
        c.execute("""
            INSERT INTO boxing_operations
            (date, boxes_made, units_per_box, total_units_boxed, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (date, boxes_made, units_per_box, total_units, notes))
        boxing_id = c.lastrowid
        # Record products in boxes (store units as entered — these represent total bottles for that boxing)
        for prod in products:
            c.execute("""
                INSERT INTO boxing_products
                (boxing_id, product_name, units_in_box, pack_size_kg)
                VALUES (?, ?, ?, ?)
            """, (boxing_id, prod['product_name'], prod['units'], prod.get('pack_size_kg', 0)))
        # Pre-check packaging stock before consuming for boxing
        for pkg in packaging_usage:
            component_name = pkg['component_name']
            quantity = float(pkg['quantity'])
            c.execute("SELECT current_qty FROM packaging_components WHERE component_name = ? COLLATE NOCASE", (component_name,))
            comp_row = c.fetchone()
            available = float(comp_row['current_qty'] or 0) if comp_row else 0
            if available < quantity:
                conn.close()
                return jsonify({'success': False, 'error': f'Not enough {component_name} for boxing. Available: {available}, needed: {quantity}'}), 400
        # Record packaging usage
        for pkg in packaging_usage:
            component_name = pkg['component_name']
            quantity = float(pkg['quantity'])
            # Consume packaging using FIFO lots; this updates current_qty and inserts packaging_movements
            allocs = consume_packaging_fifo(conn, component_name, quantity, date, 'Boxing', boxing_id, f'{int(quantity)} units of {component_name} used for boxing')
            # Record usage
            c.execute("INSERT INTO boxing_packaging_usage (boxing_id, component_name, quantity_used) VALUES (?, ?, ?)", (boxing_id, component_name, quantity))
            # For boxing packaging allocation tracking we also insert a movement per boxing operation (already done)
        conn.commit()
        conn.close()
        return jsonify({
            'success': True,
            'message': 'Boxing recorded successfully',
            'data': {
                'boxing_id': boxing_id,
                'total_units_boxed': total_units
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/boxing/history')
def get_boxing_history():
    try:
        conn = get_db()
        c = conn.cursor()
        # Optional filters: product (single name) OR products (comma-separated names), month (YYYY-MM)
        product = request.args.get('product', '').strip()
        products_param = request.args.get('products', '').strip()
        products_list = []
        if products_param:
            products_list = [p.strip().lower() for p in products_param.split(',') if p.strip()]
        month = request.args.get('month', '').strip()
        query = "SELECT * FROM boxing_operations WHERE 1=1"
        params = []
        if month:
            query += " AND date LIKE ?"
            params.append(f"{month}-%")
        query += " ORDER BY date DESC, id DESC"
        c.execute(query, params)
        history = []
        for row in c.fetchall():
            record = dict(row)
            # Get products
            c.execute("""
                SELECT product_name, units_in_box, pack_size_kg
                FROM boxing_products
                WHERE boxing_id = ?
            """, (record['id'],))
            prods = [dict(r) for r in c.fetchall()]
            # apply product filter client-side by skipping records that don't include the product(s)
            names = [p['product_name'].lower() for p in prods]
            if product:
                if product.lower() not in names:
                    continue
            if products_list:
                # require all specified products to be present in this boxing record
                if not all(p in names for p in products_list):
                    continue
            record['products'] = prods
            # Get packaging used
            c.execute("""
                SELECT component_name, quantity_used
                FROM boxing_packaging_usage
                WHERE boxing_id = ?
            """, (record['id'],))
            record['packaging_used'] = [dict(r) for r in c.fetchall()]
            history.append(record)
        conn.close()
        return jsonify({'success': True, 'data': history})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================================
# ROUTES - MANPOWER (REVISED)
# ============================================================================

@app.route('/manpower')
def manpower():
    return render_template('manpower.html')

@app.route('/api/manpower/add', methods=['POST'])
def add_manpower_data():
    try:
        data = request.json
        conn = get_db()
        c = conn.cursor()
        c.execute("""
    INSERT INTO manpower_daily 
    (date, production_male, production_female, production_hours,
     filling_male, filling_female, filling_hours,
     boxing_male, boxing_female, boxing_hours, notes)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(date) DO UPDATE SET
        production_male = ?,
        production_female = ?,
        production_hours = ?,
        filling_male = ?,
        filling_female = ?,
        filling_hours = ?,
        boxing_male = ?,
        boxing_female = ?,
        boxing_hours = ?,
        notes = ?
""", (data['date'], data['production_male'], data['production_female'], data.get('production_hours', 0),
      data['filling_male'], data['filling_female'], data.get('filling_hours', 0),
      data['boxing_male'], data['boxing_female'], data.get('boxing_hours', 0), data.get('notes', ''),
      data['production_male'], data['production_female'], data.get('production_hours', 0),
      data['filling_male'], data['filling_female'], data.get('filling_hours', 0),
      data['boxing_male'], data['boxing_female'], data.get('boxing_hours', 0), data.get('notes', '')))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Manpower data saved successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manpower/history')
def get_manpower_history():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT md.*,
                   (SELECT COUNT(*) FROM bulk_production WHERE date = md.date) as batches_made,
                   (SELECT SUM(actual_bottles_filled) FROM filling_operations WHERE date = md.date) as bottles_filled,
                   (SELECT SUM(boxes_made) FROM boxing_operations WHERE date = md.date) as boxes_made
            FROM manpower_daily md
            ORDER BY md.date DESC
        """)
        history = [dict(row) for row in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': history})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================================
# ROUTES - AUDIT (REVISED)
# ============================================================================

@app.route('/audit')
def audit():
    return render_template('audit.html')

@app.route('/api/audit/prepare', methods=['POST'])
def prepare_audit():
    """Prepare audit data with expected weights"""
    try:
        data = request.json
        audit_date = data['audit_date']
        conn = get_db()
        c = conn.cursor()
        # Get last audit date
        c.execute("""
            SELECT audit_date FROM audits
            ORDER BY audit_date DESC
            LIMIT 1
        """)
        last_audit = c.fetchone()
        last_audit_date = last_audit['audit_date'] if last_audit else '1900-01-01'
        # Get all ingredients (case-insensitive ordering)
        c.execute("SELECT name, qty_kg FROM ingredients ORDER BY name COLLATE NOCASE")
        ingredients = []
        for row in c.fetchall():
            ing_name = row['name']
            current_weight = row['qty_kg']
            # Get previous audit weight
            if last_audit:
                c.execute("""
                    SELECT ai.actual_weight_kg
                    FROM audit_ingredients ai
                    JOIN audits a ON a.id = ai.audit_id
                    WHERE a.audit_date = ? AND ai.ingredient_name = ? COLLATE NOCASE
                """, (last_audit_date, ing_name))
                prev = c.fetchone()
                previous_weight = prev['actual_weight_kg'] if prev else 0
            else:
                previous_weight = 0
            # Calculate usage between audits
            c.execute("""
                SELECT COALESCE(SUM(quantity_kg), 0) as total_used
                FROM rm_stock_movements
                WHERE ingredient_name = ? AND movement_type = 'OUT'
                AND date > ? AND date <= ?
            """, (ing_name, last_audit_date, audit_date))
            used = c.fetchone()['total_used']
            expected_weight = current_weight  # System's current weight
            ingredients.append({
                'name': ing_name,
                'previous_weight': round(previous_weight, 3),
                'expected_weight': round(expected_weight, 3),
                'usage_since_last_audit': round(used, 3)
            })
        # Get all packaging components
        packaging_components_list = []
        c.execute("SELECT component_name, current_qty FROM packaging_components ORDER BY component_name COLLATE NOCASE")
        for pkg_row in c.fetchall():
            packaging_components_list.append({
                'name': pkg_row['component_name'],
                'expected_qty': round(float(pkg_row['current_qty'] or 0), 4)
            })
        conn.close()
        return jsonify({
            'success': True,
            'data': {
                'last_audit_date': last_audit_date,
                'ingredients': ingredients,
                'packaging_components': packaging_components_list
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/audit/confirm', methods=['POST'])
def confirm_audit():
    try:
        data = request.json
        audit_date = data['audit_date']
        ingredients = data['ingredients']  # [{name, previous_weight, expected_weight, actual_weight}]
        notes = data.get('notes', '')
        conn = get_db()
        c = conn.cursor()
        # Create audit record (retry on DB lock)
        import time, sqlite3 as _sqlite3
        for attempt in range(5):
            try:
                c.execute("INSERT INTO audits (audit_date, notes) VALUES (?, ?)", (audit_date, notes))
                audit_id = c.lastrowid
                break
            except _sqlite3.OperationalError as oe:
                if 'locked' in str(oe).lower() and attempt < 4:
                    time.sleep(0.5 + attempt * 0.5)
                    continue
                raise

        # Save ingredient audits and apply adjustments if needed
        for ing in ingredients:
            name = ing['name']
            prev_w = float(ing.get('previous_weight', 0) or 0)
            expected_w = float(ing.get('expected_weight', 0) or 0)
            actual_w = float(ing.get('actual_weight', 0) or 0)
            diff = actual_w - expected_w
            c.execute("""
                INSERT INTO audit_ingredients
                (audit_id, ingredient_name, previous_weight_kg, expected_weight_kg,
                 actual_weight_kg, difference_kg)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (audit_id, name, prev_w, expected_w, actual_w, diff))
            # If adjustment needed, update ingredients table, FIFO lots, and log movement
            if abs(diff) > 0.001:
                c.execute("SELECT qty_kg FROM ingredients WHERE name = ? COLLATE NOCASE", (name,))
                old_row = c.fetchone()
                old_qty = float(old_row['qty_kg']) if old_row else 0
                # Update master stock to actual found weight
                c.execute("UPDATE ingredients SET qty_kg = ? WHERE name = ? COLLATE NOCASE", (actual_w, name))
                # Adjust FIFO lots
                preferred_lot_id = ing.get('lot_id')  # optional: user-selected lot for adjustment
                if diff < 0:
                    # Found less than expected — deduct from lots
                    remaining_to_deduct = abs(diff)
                    if preferred_lot_id:
                        # Deduct from selected lot first
                        c.execute("SELECT qty_remaining FROM rm_lots WHERE id = ?", (preferred_lot_id,))
                        lot_row = c.fetchone()
                        if lot_row:
                            deduct = min(float(lot_row['qty_remaining'] or 0), remaining_to_deduct)
                            c.execute("UPDATE rm_lots SET qty_remaining = qty_remaining - ? WHERE id = ?", (deduct, preferred_lot_id))
                            remaining_to_deduct -= deduct
                    # Deduct any remainder from oldest lots (FIFO)
                    if remaining_to_deduct > 0.001:
                        c.execute("SELECT id, qty_remaining FROM rm_lots WHERE ingredient_name = ? COLLATE NOCASE AND qty_remaining > 0.001 ORDER BY received_date ASC, id ASC", (name,))
                        for lot in c.fetchall():
                            if remaining_to_deduct <= 0.001: break
                            if lot['id'] == preferred_lot_id: continue  # already handled
                            deduct = min(float(lot['qty_remaining'] or 0), remaining_to_deduct)
                            c.execute("UPDATE rm_lots SET qty_remaining = qty_remaining - ? WHERE id = ?", (deduct, lot['id']))
                            remaining_to_deduct -= deduct
                else:
                    # Found more than expected — add to a lot
                    if preferred_lot_id:
                        c.execute("UPDATE rm_lots SET qty_remaining = qty_remaining + ? WHERE id = ?", (diff, preferred_lot_id))
                    else:
                        # Add to the most recent lot
                        c.execute("SELECT id FROM rm_lots WHERE ingredient_name = ? COLLATE NOCASE ORDER BY received_date DESC, id DESC LIMIT 1", (name,))
                        lot_row = c.fetchone()
                        if lot_row:
                            c.execute("UPDATE rm_lots SET qty_remaining = qty_remaining + ? WHERE id = ?", (diff, lot_row['id']))
                        else:
                            # No lots — create one with cost_per_kg as unit cost
                            c.execute("SELECT cost_per_kg FROM ingredients WHERE name = ? COLLATE NOCASE", (name,))
                            ing_row = c.fetchone()
                            unit_cost = ing_row['cost_per_kg'] if ing_row else 0
                            c.execute("INSERT INTO rm_lots (ingredient_name, qty_remaining, unit_cost, received_date, reference_type, reference_id, notes) VALUES (?, ?, ?, ?, 'Audit', ?, ?)",
                                      (name, diff, unit_cost, audit_date, audit_id, f'Audit surplus: {actual_w} found vs {expected_w} expected'))
                # Log movement
                c.execute("""
                    INSERT INTO rm_stock_movements
                    (date, ingredient_name, movement_type, quantity_kg, reference_type, reference_id, notes)
                    VALUES (?, ?, 'ADJUSTMENT', ?, 'Audit', ?, ?)
                """, (audit_date, name, abs(diff), audit_id, f'Audit adjustment: expected {expected_w}, found {actual_w}'))
        # Process packaging components
        pkg_components = data.get('packaging_components', [])
        for pkg in pkg_components:
            pkg_name = pkg['name']
            expected_qty = float(pkg.get('expected_qty', 0) or 0)
            actual_qty = float(pkg.get('actual_qty', 0) or 0)
            pkg_diff = actual_qty - expected_qty
            if abs(pkg_diff) > 0.001:
                # Update master packaging qty
                c.execute("UPDATE packaging_components SET current_qty = ? WHERE component_name = ? COLLATE NOCASE", (actual_qty, pkg_name))
                # Determine lot cost for note
                preferred_lot_id = pkg.get('lot_id')
                lot_cost = 0.0
                if preferred_lot_id:
                    c.execute("SELECT unit_cost FROM packaging_lots WHERE id = ?", (preferred_lot_id,))
                    l_row = c.fetchone()
                    if l_row:
                        lot_cost = float(l_row['unit_cost'] or 0)
                # Adjust FIFO lots
                if pkg_diff < 0:
                    remaining_to_deduct = abs(pkg_diff)
                    if preferred_lot_id:
                        c.execute("SELECT qty_remaining FROM packaging_lots WHERE id = ?", (preferred_lot_id,))
                        l_row = c.fetchone()
                        if l_row:
                            deduct = min(float(l_row['qty_remaining'] or 0), remaining_to_deduct)
                            c.execute("UPDATE packaging_lots SET qty_remaining = qty_remaining - ? WHERE id = ?", (deduct, preferred_lot_id))
                            remaining_to_deduct -= deduct
                    if remaining_to_deduct > 0.001:
                        c.execute("SELECT id, qty_remaining FROM packaging_lots WHERE component_name = ? COLLATE NOCASE AND qty_remaining > 0.001 ORDER BY received_date ASC, id ASC", (pkg_name,))
                        for lot in c.fetchall():
                            if remaining_to_deduct <= 0.001: break
                            if lot['id'] == preferred_lot_id: continue
                            deduct = min(float(lot['qty_remaining'] or 0), remaining_to_deduct)
                            c.execute("UPDATE packaging_lots SET qty_remaining = qty_remaining - ? WHERE id = ?", (deduct, lot['id']))
                            remaining_to_deduct -= deduct
                else:
                    if preferred_lot_id:
                        c.execute("UPDATE packaging_lots SET qty_remaining = qty_remaining + ? WHERE id = ?", (pkg_diff, preferred_lot_id))
                    else:
                        c.execute("SELECT id FROM packaging_lots WHERE component_name = ? COLLATE NOCASE ORDER BY received_date DESC, id DESC LIMIT 1", (pkg_name,))
                        l_row = c.fetchone()
                        if l_row:
                            c.execute("UPDATE packaging_lots SET qty_remaining = qty_remaining + ? WHERE id = ?", (pkg_diff, l_row['id']))
                        else:
                            c.execute("SELECT cost_per_unit FROM packaging_components WHERE component_name = ? COLLATE NOCASE", (pkg_name,))
                            comp_row = c.fetchone()
                            unit_cost_val = comp_row['cost_per_unit'] if comp_row else 0
                            c.execute("INSERT INTO packaging_lots (component_name, qty_remaining, unit_cost, received_date, reference_type, reference_id, notes) VALUES (?, ?, ?, ?, 'Audit', ?, ?)",
                                      (pkg_name, pkg_diff, unit_cost_val, audit_date, audit_id, f'Audit surplus: {actual_qty} found vs {expected_qty} expected'))
                # Log packaging movement with required note format
                pkg_movement_type = 'IN' if pkg_diff > 0 else 'OUT'
                pkg_note = f"quantity of this cost changed from {round(expected_qty, 4)} to {round(actual_qty, 4)} during audit on {audit_date}"
                c.execute("""
                    INSERT INTO packaging_movements
                    (date, component_name, movement_type, quantity, unit_cost, reference_type, reference_id, notes)
                    VALUES (?, ?, ?, ?, ?, 'Audit', ?, ?)
                """, (audit_date, pkg_name, pkg_movement_type, abs(pkg_diff), lot_cost, audit_id, pkg_note))
        conn.commit()
        conn.close()
        return jsonify({
            'success': True,
            'message': 'Audit completed successfully',
            'data': {'audit_id': audit_id}
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/audit/history')
def get_audit_history():
    try:
        conn = get_db()
        c = conn.cursor()
        month = request.args.get('month', '').strip()
        query = "SELECT * FROM audits WHERE 1=1"
        params = []
        if month:
            query += " AND audit_date LIKE ?"
            params.append(f"{month}-%")
        query += " ORDER BY audit_date DESC"
        c.execute(query, params)
        audits = []
        for row in c.fetchall():
            audit = dict(row)
            c.execute("""
                SELECT * FROM audit_ingredients
                WHERE audit_id = ?
                ORDER BY ingredient_name
            """, (audit['id'],))
            audit['ingredients'] = [dict(r) for r in c.fetchall()]
            # Packaging adjustments recorded during this audit
            c.execute("""
                SELECT component_name, movement_type, quantity, unit_cost, notes
                FROM packaging_movements
                WHERE reference_type = 'Audit' AND reference_id = ?
                ORDER BY component_name
            """, (audit['id'],))
            audit['packaging_adjustments'] = [dict(r) for r in c.fetchall()]
            audits.append(audit)
        conn.close()
        return jsonify({'success': True, 'data': audits})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/audit/monthly-summary')
def get_audit_monthly_summary():
    """Get monthly ingredient usage summary"""
    try:
        month = request.args.get('month', datetime.now().strftime('%Y-%m'))
        conn = get_db()
        c = conn.cursor()
        # Get start and end of month
        start_date = f"{month}-01"
        if month == datetime.now().strftime('%Y-%m'):
            end_date = datetime.now().strftime('%Y-%m-%d')
        else:
            # Last day of month
            from calendar import monthrange
            year, mon = map(int, month.split('-'))
            last_day = monthrange(year, mon)[1]
            end_date = f"{month}-{last_day:02d}"
        # Get all ingredients
        c.execute("SELECT name FROM ingredients ORDER BY name")
        summary = []
        for row in c.fetchall():
            ing_name = row['name']
            # Weight at start of month
            c.execute("""
                SELECT qty_kg FROM ingredients WHERE name = ?
            """, (ing_name,))
            current = c.fetchone()['qty_kg']
            # Total used in month
            c.execute("""
                SELECT COALESCE(SUM(quantity_kg), 0) as used
                FROM rm_stock_movements
                WHERE ingredient_name = ? AND movement_type = 'OUT'
                AND date >= ? AND date <= ?
            """, (ing_name, start_date, end_date))
            used = c.fetchone()['used']
            # Total added in month
            c.execute("""
                SELECT COALESCE(SUM(quantity_kg), 0) as added
                FROM rm_stock_movements
                WHERE ingredient_name = ? AND movement_type = 'IN'
                AND date >= ? AND date <= ?
            """, (ing_name, start_date, end_date))
            added = c.fetchone()['added']
            start_weight = current + used - added
            summary.append({
                'ingredient': ing_name,
                'start_weight': round(start_weight, 3),
                'end_weight': round(current, 3),
                'used_in_month': round(used, 3),
                'added_in_month': round(added, 3)
            })
        # Packaging summary: used (OUT) and damaged (DAMAGE) per component
        c.execute("SELECT component_name FROM packaging_components ORDER BY component_name COLLATE NOCASE")
        pkg_summary = []
        for pkg_row in c.fetchall():
            comp_name = pkg_row['component_name']
            c.execute("""
                SELECT COALESCE(SUM(quantity), 0) as used
                FROM packaging_movements
                WHERE component_name = ? AND movement_type = 'OUT'
                AND date >= ? AND date <= ?
            """, (comp_name, start_date, end_date))
            pkg_used = float(c.fetchone()['used'] or 0)
            c.execute("""
                SELECT COALESCE(SUM(quantity), 0) as damaged
                FROM packaging_movements
                WHERE component_name = ? AND movement_type = 'DAMAGE'
                AND date >= ? AND date <= ?
            """, (comp_name, start_date, end_date))
            pkg_damaged = float(c.fetchone()['damaged'] or 0)
            if pkg_used > 0 or pkg_damaged > 0:
                pkg_summary.append({
                    'component': comp_name,
                    'used_in_month': round(pkg_used, 2),
                    'damaged_in_month': round(pkg_damaged, 2)
                })
        conn.close()
        return jsonify({'success': True, 'data': summary, 'packaging_data': pkg_summary})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/audit/monthly-consumption-cost')
def get_monthly_consumption_cost():
    """Calculate consumption costs per product for a month, with FIFO tier breakdown."""
    try:
        month = request.args.get('month', datetime.now().strftime('%Y-%m'))
        product_filter = request.args.get('products', '')

        conn = get_db()
        c = conn.cursor()

        from calendar import monthrange
        year, mon = map(int, month.split('-'))
        last_day = monthrange(year, mon)[1]
        start_date = f"{month}-01"
        end_date   = f"{month}-{last_day:02d}"

        if product_filter:
            products = [p.strip() for p in product_filter.split(',') if p.strip()]
        else:
            c.execute("SELECT DISTINCT product_name FROM bulk_production WHERE date >= ? AND date <= ?",
                      (start_date, end_date))
            products = [row[0] for row in c.fetchall()]

        def _add_rm_tier(d, ing_name, kg_used, cost_per_kg):
            if ing_name not in d:
                d[ing_name] = {'tiers': [], 'total_kg': 0.0, 'total_cost': 0.0}
            total = kg_used * cost_per_kg
            existing = next((t for t in d[ing_name]['tiers'] if abs(t['cost_per_kg'] - cost_per_kg) < 0.00001), None)
            if existing:
                existing['kg_used']    = round(existing['kg_used'] + kg_used, 4)
                existing['total_cost'] = round(existing['total_cost'] + total, 2)
            else:
                d[ing_name]['tiers'].append({'kg_used': round(kg_used, 4), 'cost_per_kg': cost_per_kg, 'total_cost': round(total, 2)})
            d[ing_name]['total_kg']   = round(d[ing_name]['total_kg']   + kg_used, 4)
            d[ing_name]['total_cost'] = round(d[ing_name]['total_cost'] + total,   2)
            return total

        def _add_pkg_tier(d, comp_name, qty_used, cost_per_unit):
            if comp_name not in d:
                d[comp_name] = {'tiers': [], 'total_qty': 0.0, 'total_cost': 0.0}
            total = qty_used * cost_per_unit
            existing = next((t for t in d[comp_name]['tiers'] if abs(t['cost_per_unit'] - cost_per_unit) < 0.00001), None)
            if existing:
                existing['qty_used']   = round(existing['qty_used'] + qty_used, 4)
                existing['total_cost'] = round(existing['total_cost'] + total, 2)
            else:
                d[comp_name]['tiers'].append({'qty_used': round(qty_used, 4), 'cost_per_unit': cost_per_unit, 'total_cost': round(total, 2)})
            d[comp_name]['total_qty']  = round(d[comp_name]['total_qty']  + qty_used, 4)
            d[comp_name]['total_cost'] = round(d[comp_name]['total_cost'] + total,   2)
            return total

        results = []
        for product_name in products:
            rm_d  = {}
            pkg_d = {}
            total_rm_cost  = 0.0
            total_pkg_cost = 0.0

            # ── 1. Raw material costs filtered to THIS product only ──────────
            # Join rm_stock_movements with bulk_production via reference_id so each
            # product only gets the RM consumed for its own production batches.
            c.execute("""
                SELECT sm.ingredient_name, sm.unit_cost, SUM(sm.quantity_kg) as qty_used
                FROM rm_stock_movements sm
                JOIN bulk_production bp
                  ON sm.reference_type = 'Production' AND sm.reference_id = bp.id
                WHERE sm.movement_type = 'OUT'
                  AND LOWER(bp.product_name) = LOWER(?)
                  AND sm.date >= ? AND sm.date <= ?
                GROUP BY sm.ingredient_name, sm.unit_cost
            """, (product_name, start_date, end_date))
            for row in c.fetchall():
                total_rm_cost += _add_rm_tier(rm_d, row['ingredient_name'], row['qty_used'] or 0, row['unit_cost'] or 0)

            # ── 2. Packaging costs for FILLING (FIFO via packaging_movements) ─
            c.execute("""
                SELECT pm.component_name, pm.unit_cost, SUM(pm.quantity) as qty_used
                FROM packaging_movements pm
                JOIN filling_operations fo
                  ON pm.reference_type = 'Filling' AND pm.reference_id = fo.id
                WHERE LOWER(fo.product_name) = LOWER(?)
                  AND fo.date >= ? AND fo.date <= ?
                  AND pm.movement_type = 'OUT'
                GROUP BY pm.component_name, pm.unit_cost
            """, (product_name, start_date, end_date))
            for row in c.fetchall():
                total_pkg_cost += _add_pkg_tier(pkg_d, row['component_name'], row['qty_used'] or 0, row['unit_cost'] or 0)

            # ── 3. Packaging costs for BOXING (FIFO via packaging_movements) ──
            c.execute("""
                SELECT DISTINCT bo.id
                FROM boxing_operations bo
                JOIN boxing_products bp ON bp.boxing_id = bo.id
                WHERE bo.date >= ? AND bo.date <= ?
                  AND LOWER(bp.product_name) = LOWER(?)
            """, (start_date, end_date, product_name))
            boxing_ids = [r['id'] for r in c.fetchall()]

            for bo_id in boxing_ids:
                c.execute("SELECT product_name, units_in_box FROM boxing_products WHERE boxing_id = ?", (bo_id,))
                prods = [dict(r) for r in c.fetchall()]
                total_units = sum(p['units_in_box'] for p in prods) or 0
                if total_units == 0:
                    continue
                this_units = sum(p['units_in_box'] for p in prods if p['product_name'].lower() == product_name.lower())
                if this_units == 0:
                    continue
                share = this_units / total_units
                # Use actual FIFO lot costs from packaging_movements for this boxing op
                c.execute("""
                    SELECT component_name, unit_cost, quantity
                    FROM packaging_movements
                    WHERE reference_type = 'Boxing' AND reference_id = ? AND movement_type = 'OUT'
                """, (bo_id,))
                for row in c.fetchall():
                    alloc_qty = (row['quantity'] or 0) * share
                    total_pkg_cost += _add_pkg_tier(pkg_d, row['component_name'], alloc_qty, row['unit_cost'] or 0)

            results.append({
                'product':              product_name,
                'raw_materials':        rm_d,
                'packaging':            pkg_d,
                'total_rm_cost':        round(total_rm_cost,  2),
                'total_packaging_cost': round(total_pkg_cost, 2),
                'total_cost':           round(total_rm_cost + total_pkg_cost, 2),
            })

        conn.close()
        return jsonify({'success': True, 'data': results, 'month': month})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# ROUTES - FIFO LOT SUMMARIES
# ============================================================================

@app.route('/api/rm/lot-summary')
def rm_lot_summary():
    """Current FIFO lot breakdown per ingredient — qty remaining per price tier"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT ingredient_name, unit_cost, SUM(qty_remaining) as qty_remaining
            FROM rm_lots WHERE qty_remaining > 0.001
            GROUP BY ingredient_name, unit_cost
            ORDER BY ingredient_name COLLATE NOCASE, received_date ASC, id ASC
        """)
        result = {}
        for r in c.fetchall():
            name = r['ingredient_name']
            if name not in result:
                result[name] = []
            result[name].append({'unit_cost': r['unit_cost'] or 0, 'qty_remaining': round(r['qty_remaining'], 4)})
        conn.close()
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/rm/lots/<path:ingredient_name>')
def get_rm_lots_for_ingredient(ingredient_name):
    """Get individual FIFO lots for an ingredient (for audit lot selection)"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT id, qty_remaining, unit_cost, received_date, notes
            FROM rm_lots WHERE ingredient_name = ? COLLATE NOCASE AND qty_remaining > 0.001
            ORDER BY received_date ASC, id ASC
        """, (ingredient_name,))
        lots = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': lots})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ingredients/delete/<int:id>', methods=['DELETE'])
def delete_ingredient(id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT name FROM ingredients WHERE id = ?", (id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Ingredient not found'}), 404
        name = row['name']
        c.execute("DELETE FROM rm_stock_movements WHERE ingredient_name = ? COLLATE NOCASE", (name,))
        c.execute("DELETE FROM rm_lots WHERE ingredient_name = ? COLLATE NOCASE", (name,))
        c.execute("DELETE FROM ingredients WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': f'"{name}" deleted successfully'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/packaging/components/delete/<int:id>', methods=['DELETE'])
def delete_packaging_component(id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT component_name FROM packaging_components WHERE id = ?", (id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Component not found'}), 404
        name = row['component_name']
        c.execute("DELETE FROM packaging_movements WHERE component_name = ? COLLATE NOCASE", (name,))
        c.execute("DELETE FROM packaging_lots WHERE component_name = ? COLLATE NOCASE", (name,))
        c.execute("DELETE FROM packaging_components WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': f'"{name}" deleted successfully'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/audit/delete/<int:audit_id>', methods=['DELETE'])
def delete_audit(audit_id):
    try:
        conn = get_db()
        c = conn.cursor()
        # Only allow deleting the most recent audit
        c.execute("SELECT id FROM audits ORDER BY audit_date DESC, id DESC LIMIT 1")
        latest = c.fetchone()
        if not latest or latest['id'] != audit_id:
            conn.close()
            return jsonify({'success': False, 'error': 'Only the most recent audit can be deleted'}), 400
        # Fetch ingredients from this audit to reverse adjustments
        c.execute("SELECT * FROM audit_ingredients WHERE audit_id = ?", (audit_id,))
        ingredients = c.fetchall()
        for ing in ingredients:
            name = ing['ingredient_name']
            expected_w = float(ing['expected_weight_kg'])
            actual_w = float(ing['actual_weight_kg'])
            diff = actual_w - expected_w
            if abs(diff) > 0.001:
                # Restore ingredient qty to expected (pre-audit) value
                c.execute("UPDATE ingredients SET qty_kg = ? WHERE name = ? COLLATE NOCASE", (expected_w, name))
                if diff < 0:
                    # Shortage recorded: lots were reduced. Add back to most recent lot.
                    restore_qty = abs(diff)
                    c.execute("SELECT id FROM rm_lots WHERE ingredient_name = ? COLLATE NOCASE ORDER BY received_date DESC, id DESC LIMIT 1", (name,))
                    lot = c.fetchone()
                    if lot:
                        c.execute("UPDATE rm_lots SET qty_remaining = qty_remaining + ? WHERE id = ?", (restore_qty, lot['id']))
                    else:
                        c.execute("SELECT cost_per_kg FROM ingredients WHERE name = ? COLLATE NOCASE", (name,))
                        ing_row = c.fetchone()
                        unit_cost = ing_row['cost_per_kg'] if ing_row else 0
                        c.execute("INSERT INTO rm_lots (ingredient_name, qty_remaining, unit_cost, received_date, reference_type, reference_id, notes) VALUES (?, ?, ?, ?, 'Audit Restore', ?, ?)",
                                  (name, restore_qty, unit_cost, datetime.now().strftime('%Y-%m-%d'), audit_id, 'Restored from deleted audit'))
                else:
                    # Surplus recorded: lots were increased. Deduct diff from most recent lots.
                    remaining_to_deduct = diff
                    c.execute("SELECT id, qty_remaining FROM rm_lots WHERE ingredient_name = ? COLLATE NOCASE AND qty_remaining > 0.001 ORDER BY received_date DESC, id DESC", (name,))
                    for lot in c.fetchall():
                        if remaining_to_deduct <= 0.001:
                            break
                        deduct = min(float(lot['qty_remaining'] or 0), remaining_to_deduct)
                        c.execute("UPDATE rm_lots SET qty_remaining = qty_remaining - ? WHERE id = ?", (deduct, lot['id']))
                        remaining_to_deduct -= deduct
                # Remove the audit adjustment movement log
                c.execute("DELETE FROM rm_stock_movements WHERE reference_type = 'Audit' AND reference_id = ? AND ingredient_name = ? COLLATE NOCASE AND movement_type = 'ADJUSTMENT'", (audit_id, name))
        c.execute("DELETE FROM audit_ingredients WHERE audit_id = ?", (audit_id,))
        c.execute("DELETE FROM audits WHERE id = ?", (audit_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Audit deleted and ingredient quantities restored to pre-audit values'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/packaging/lots/<path:component_name>')
def get_packaging_lots_for_component(component_name):
    """Get individual FIFO lots for a packaging component (for damage lot selection)"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT id, qty_remaining, unit_cost, received_date, notes
            FROM packaging_lots WHERE component_name = ? COLLATE NOCASE AND qty_remaining > 0.001
            ORDER BY received_date ASC, id ASC
        """, (component_name,))
        lots = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({'success': True, 'data': lots})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/packaging/lot-summary')
def packaging_lot_summary():
    """Current FIFO lot breakdown per packaging component — qty remaining per price tier"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT component_name, unit_cost, SUM(qty_remaining) as qty_remaining
            FROM packaging_lots WHERE qty_remaining > 0.001
            GROUP BY component_name, unit_cost
            ORDER BY component_name COLLATE NOCASE, received_date ASC, id ASC
        """)
        result = {}
        for r in c.fetchall():
            name = r['component_name']
            if name not in result:
                result[name] = []
            result[name].append({'unit_cost': r['unit_cost'] or 0, 'qty_remaining': round(r['qty_remaining'], 4)})
        conn.close()
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# ROUTES - GOOGLE SHEETS SYNC
# ============================================================================

@app.route('/api/sync-to-sheets', methods=['POST'])
def sync_to_sheets():
    """Sync all data to Google Sheets"""
    try:
        try:
            from google_sheets_sync import sync_all_data_to_sheets
        except ImportError:
            return jsonify({
                'success': False,
                'error': 'Google Sheets sync module not found'
            }), 500
        conn = get_db()
        result = sync_all_data_to_sheets(conn)
        conn.close()
        # Log sync
        log_conn = get_db()
        log_c = log_conn.cursor()
        log_c.execute("""
            INSERT INTO sync_log (sync_type, status, message)
            VALUES (?, ?, ?)
        """, ('manual', 'success' if result['success'] else 'failed', result['message']))
        log_conn.commit()
        log_conn.close()
        if result['success']:
            return jsonify(result)
        else:
            return jsonify(result), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sync-to-sheets/audit', methods=['POST'])
def sync_audit_only():
    """Sync only the Audit tab to Google Sheets"""
    try:
        try:
            from google_sheets_sync import sync_audit
        except ImportError:
            return jsonify({'success': False, 'error': 'Google Sheets sync module not found'}), 500
        conn = get_db()
        try:
            client = None
            # reuse the helper in google_sheets_sync
            from google_sheets_sync import get_sheets_client, get_or_create_worksheet
            client = get_sheets_client()
            sheet = client.open_by_key(__import__('config').GOOGLE_SHEET_ID)
            result_msg = sync_audit(conn, sheet)
            conn.close()
            return jsonify({'success': True, 'message': result_msg})
        except Exception as e:
            conn.close()
            traceback.print_exc()
            return jsonify({'success': False, 'error': str(e)}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================================
# REQUISITION FORM GENERATION
# ============================================================================

@app.route('/api/generate-requisition', methods=['POST'])
def generate_requisition():
    """Generate a filled requisition form xlsx for a raw material or packaging component."""
    try:
        try:
            from openpyxl import load_workbook
        except ImportError:
            import subprocess, sys
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'openpyxl', '--quiet'])
            from openpyxl import load_workbook
        from io import BytesIO
        from flask import send_file
        import copy

        data = request.get_json()
        item_name   = data.get('item_name', '')
        item_type   = data.get('item_type', 'ingredient')  # 'ingredient' or 'packaging'
        number      = data.get('number', '')
        department  = data.get('department', '')
        count       = data.get('count', '')
        purpose     = data.get('purpose', '')
        status      = data.get('status', '')
        prepared_by = data.get('prepared_by', 'Akshay')

        # Allow caller to override cost (editable field from UI)
        cost_override = data.get('cost', '')

        conn = get_db()
        c = conn.cursor()

        # Fetch current stock, vendor, and last restock cost from DB
        vendor_name    = ''
        current_stock  = ''
        last_cost      = ''
        if item_type == 'ingredient':
            c.execute("SELECT qty_kg, supplier, cost_per_kg FROM ingredients WHERE name = ? COLLATE NOCASE", (item_name,))
            row = c.fetchone()
            if row:
                current_stock = f"{row['qty_kg']} kg"
                vendor_name   = row['supplier'] or ''
            # Last restock cost from most recent IN movement
            c.execute("""
                SELECT unit_cost FROM rm_stock_movements
                WHERE ingredient_name = ? COLLATE NOCASE AND movement_type = 'IN'
                  AND reference_type = 'Restock' AND unit_cost > 0
                ORDER BY date DESC, id DESC LIMIT 1
            """, (item_name,))
            cost_row = c.fetchone()
            last_cost = str(cost_row['unit_cost']) if cost_row else (str(row['cost_per_kg']) if row and row['cost_per_kg'] else '')
        else:
            c.execute("SELECT current_qty, supplier, cost_per_unit FROM packaging_components WHERE component_name = ? COLLATE NOCASE", (item_name,))
            row = c.fetchone()
            if row:
                current_stock = str(row['current_qty'])
                vendor_name   = row['supplier'] or ''
            # Last restock cost from most recent IN packaging movement
            c.execute("""
                SELECT unit_cost FROM packaging_movements
                WHERE component_name = ? COLLATE NOCASE AND movement_type = 'IN'
                  AND reference_type = 'Restock' AND unit_cost > 0
                ORDER BY date DESC, id DESC LIMIT 1
            """, (item_name,))
            cost_row = c.fetchone()
            last_cost = str(cost_row['unit_cost']) if cost_row else (str(row['cost_per_unit']) if row and row['cost_per_unit'] else '')
        conn.close()

        # Use caller-supplied cost if provided, otherwise fall back to last restock cost
        resolved_cost = cost_override if cost_override else last_cost

        # Load the template from the same directory as app.py
        template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Requisition_Form.xlsx')
        wb = load_workbook(template_path)
        ws = wb.active

        today = datetime.now().strftime('%d-%m-%Y')

        replacements = {
            '{{DATE}}':          today,
            '{{NUMBER}}':        str(number),
            '{{DEPARTMENT}}':    department,
            '{{ITEM_NAME}}':     item_name,
            '{{COUNT}}':         str(count),
            '{{CURRENT_STOCK}}': current_stock,
            '{{PURPOSE}}':       purpose,
            '{{VENDOR_NAME}}':   vendor_name,
            '{{STATUS}}':        status,
            '{{PREPARED_BY}}':   prepared_by,
            '{{COST}}':          resolved_cost,
        }

        for row in ws.iter_rows():
            for cell in row:
                if cell.value and isinstance(cell.value, str):
                    new_val = cell.value
                    for placeholder, replacement in replacements.items():
                        new_val = new_val.replace(placeholder, replacement)
                    if new_val != cell.value:
                        cell.value = new_val

        output = BytesIO()
        wb.save(output)
        output.seek(0)

        safe_name = item_name.replace(' ', '_').replace('/', '-')
        filename  = f"Requisition_{safe_name}_{today.replace('-', '')}.xlsx"

        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# RUN APPLICATION
# ============================================================================

if __name__ == '__main__':
    # Always ensure database schema is initialized/migrated on startup.
    # This is idempotent and will create missing tables/columns when upgrading an existing DB.
    print("Ensuring database schema is initialized/migrated...")
    init_database()
    import socket
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    print("\n" + "="*60)
    print("🏭 MANUFACTURING MANAGEMENT SYSTEM V2")
    print("="*60)
    print(f"\n✅ Server starting...")
    print(f"📍 Access from this computer: http://localhost:5000")
    print(f"📍 Access from other computers: http://{local_ip}:5000")
    print(f"📁 Database: {DB_NAME}")
    print("\n💡 Press Ctrl+C to stop the server")
    print("="*60 + "\n")
    app.run(host='0.0.0.0', port=8080, debug=True)
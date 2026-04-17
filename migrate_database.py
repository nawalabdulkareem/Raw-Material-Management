#!/usr/bin/env python3
"""
Database Migration Script - V1 to V2
Run this ONCE to upgrade your existing database
"""

import sqlite3
import os
from datetime import datetime

DB_NAME = "manufacturing.db"

def backup_database():
    """Create backup of existing database"""
    if os.path.exists(DB_NAME):
        backup_name = f"manufacturing_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        import shutil
        shutil.copy(DB_NAME, backup_name)
        print(f"✅ Backup created: {backup_name}")
        return True
    return False

def migrate_database():
    """Migrate database from V1 to V2 structure"""
    print("\n" + "="*60)
    print("DATABASE MIGRATION - V1 to V2")
    print("="*60)
    
    # Create backup first
    if backup_database():
        print("✅ Database backed up successfully")
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    print("\nMigrating database structure...")
    
    # 1. Update bulk_production table - add batch_number if missing
    try:
        c.execute("SELECT batch_number FROM bulk_production LIMIT 1")
        print("✅ bulk_production.batch_number already exists")
    except sqlite3.OperationalError:
        print("⚙️  Adding batch_number to bulk_production...")
        c.execute("ALTER TABLE bulk_production ADD COLUMN batch_number TEXT")
        # Update existing records with auto-generated batch numbers
        c.execute("SELECT id, date, product_name FROM bulk_production WHERE batch_number IS NULL")
        for row in c.fetchall():
            batch_num = f"BATCH_{row[1].replace('-','')}_{row[0]}"
            c.execute("UPDATE bulk_production SET batch_number = ? WHERE id = ?", (batch_num, row[0]))
        print("✅ batch_number column added and populated")
    
    # 2. Create packaging_components table if not exists
    c.execute("""
    CREATE TABLE IF NOT EXISTS packaging_components (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        component_name TEXT UNIQUE NOT NULL,
        current_qty REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    print("✅ packaging_components table ready")
    
    # 3. Create packaging_movements table
    c.execute("""
    CREATE TABLE IF NOT EXISTS packaging_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        component_name TEXT NOT NULL,
        movement_type TEXT NOT NULL,
        quantity REAL NOT NULL,
        reference_type TEXT,
        reference_id INTEGER,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    print("✅ packaging_movements table ready")
    
    # 4. Update filling_operations table
    try:
        c.execute("SELECT batch_number FROM filling_operations LIMIT 1")
        print("✅ filling_operations.batch_number already exists")
    except sqlite3.OperationalError:
        print("⚙️  Recreating filling_operations table with new structure...")
        # Backup old data
        try:
            c.execute("SELECT * FROM filling_operations")
            old_data = c.fetchall()
            c.execute("DROP TABLE filling_operations")
        except:
            old_data = []
        
        # Create new structure - CHANGED TO pack_size_kg
        c.execute("""
        CREATE TABLE filling_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            product_name TEXT NOT NULL,
            batch_number TEXT NOT NULL,
            pack_size_kg REAL NOT NULL,
            bulk_used_kg REAL NOT NULL,
            actual_bottles_filled INTEGER NOT NULL,
            theoretical_bottles INTEGER NOT NULL,
            bottles_diff INTEGER NOT NULL,
            bottles_diff_percentage REAL NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        print("✅ filling_operations table recreated with pack_size_kg")
    
    # 5. Create filling_packaging_usage table
    c.execute("""
    CREATE TABLE IF NOT EXISTS filling_packaging_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filling_id INTEGER NOT NULL,
        component_name TEXT NOT NULL,
        quantity_used REAL NOT NULL,
        FOREIGN KEY(filling_id) REFERENCES filling_operations(id)
    )
    """)
    print("✅ filling_packaging_usage table ready")
    
    # 6. Update boxing_operations table
    try:
        c.execute("SELECT units_per_box FROM boxing_operations LIMIT 1")
        print("✅ boxing_operations.units_per_box already exists")
    except sqlite3.OperationalError:
        print("⚙️  Recreating boxing_operations table...")
        try:
            c.execute("DROP TABLE boxing_operations")
        except:
            pass
        
        c.execute("""
        CREATE TABLE boxing_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            boxes_made INTEGER NOT NULL,
            units_per_box INTEGER NOT NULL,
            total_units_boxed INTEGER NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        print("✅ boxing_operations table recreated")
    
    # 7. Create boxing_products table
    c.execute("""
    CREATE TABLE IF NOT EXISTS boxing_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        boxing_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        units_in_box INTEGER NOT NULL,
        FOREIGN KEY(boxing_id) REFERENCES boxing_operations(id)
    )
    """)
    print("✅ boxing_products table ready")
    
    # 8. Create boxing_packaging_usage table
    c.execute("""
    CREATE TABLE IF NOT EXISTS boxing_packaging_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        boxing_id INTEGER NOT NULL,
        component_name TEXT NOT NULL,
        quantity_used REAL NOT NULL,
        FOREIGN KEY(boxing_id) REFERENCES boxing_operations(id)
    )
    """)
    print("✅ boxing_packaging_usage table ready")
    
    # 9. Update manpower_daily table
    try:
        c.execute("SELECT production_male FROM manpower_daily LIMIT 1")
        print("✅ manpower_daily columns already exist")
    except sqlite3.OperationalError:
        print("⚙️  Recreating manpower_daily table...")
        try:
            c.execute("DROP TABLE manpower_daily")
        except:
            pass
        
        c.execute("""
        CREATE TABLE manpower_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            production_male INTEGER DEFAULT 0,
            production_female INTEGER DEFAULT 0,
            filling_male INTEGER DEFAULT 0,
            filling_female INTEGER DEFAULT 0,
            boxing_male INTEGER DEFAULT 0,
            boxing_female INTEGER DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        print("✅ manpower_daily table recreated")
    
    # 10. Create audits table
    c.execute("""
    CREATE TABLE IF NOT EXISTS audits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        audit_date TEXT NOT NULL UNIQUE,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    print("✅ audits table ready")
    
    # 11. Create audit_ingredients table
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
    print("✅ audit_ingredients table ready")
    
    conn.commit()
    conn.close()
    
    print("\n" + "="*60)
    print("✅ MIGRATION COMPLETED SUCCESSFULLY!")
    print("="*60)
    print("\nYour database has been upgraded to V2 structure.")
    print("You can now use all the new features.")
    print("\nNOTE: A backup was created in case you need to revert.")
    print("="*60 + "\n")

if __name__ == "__main__":
    if os.path.exists(DB_NAME):
        print(f"\nFound database: {DB_NAME}")
        response = input("Do you want to migrate to V2 structure? (yes/no): ")
        if response.lower() in ['yes', 'y']:
            migrate_database()
        else:
            print("Migration cancelled.")
    else:
        print(f"\nNo database found at {DB_NAME}")
        print("The database will be created automatically when you run app.py")

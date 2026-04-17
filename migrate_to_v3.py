#!/usr/bin/env python3
"""
Database Migration V3 - Add Cost Tracking, Litre Conversions, Hours, and History Features
Run this after V2 migration to add the new features
"""

import sqlite3
import os
from datetime import datetime

DB_NAME = "manufacturing.db"

def backup_database():
    """Create backup of existing database"""
    if os.path.exists(DB_NAME):
        backup_name = f"manufacturing_backup_v3_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        import shutil
        shutil.copy(DB_NAME, backup_name)
        print(f"✅ Backup created: {backup_name}")
        return True
    return False

def migrate_to_v3():
    """Add new columns and features for V3"""
    print("\n" + "="*60)
    print("DATABASE MIGRATION - V3 (Costs, Litres, Hours)")
    print("="*60)
    
    backup_database()
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    print("\nAdding new features...")
    
    # 1. Add cost to packaging components
    try:
        c.execute("SELECT cost_per_unit FROM packaging_components LIMIT 1")
        print("✅ packaging_components.cost_per_unit already exists")
    except sqlite3.OperationalError:
        print("⚙️  Adding cost_per_unit to packaging_components...")
        c.execute("ALTER TABLE packaging_components ADD COLUMN cost_per_unit REAL DEFAULT 0")
        print("✅ cost_per_unit column added")
    
    # 2. Add kg-to-litre factor to products
    try:
        c.execute("SELECT kg_to_litre_factor FROM products LIMIT 1")
        print("✅ products.kg_to_litre_factor already exists")
    except sqlite3.OperationalError:
        print("⚙️  Adding kg_to_litre_factor to products...")
        c.execute("ALTER TABLE products ADD COLUMN kg_to_litre_factor REAL DEFAULT 1.0")
        print("✅ kg_to_litre_factor column added (default 1.0)")
    
    # 3. Add bulk poured to filling operations
    try:
        c.execute("SELECT bulk_poured_kg FROM filling_operations LIMIT 1")
        print("✅ filling_operations.bulk_poured_kg already exists")
    except sqlite3.OperationalError:
        print("⚙️  Adding bulk_poured_kg to filling_operations...")
        c.execute("ALTER TABLE filling_operations ADD COLUMN bulk_poured_kg REAL DEFAULT 0")
        # Update existing records to match bulk_used_kg
        c.execute("UPDATE filling_operations SET bulk_poured_kg = bulk_used_kg WHERE bulk_poured_kg = 0")
        print("✅ bulk_poured_kg column added")
    
    # 4. Add hours to manpower_daily
    for dept in ['production', 'filling', 'boxing']:
        col_name = f"{dept}_hours"
        try:
            c.execute(f"SELECT {col_name} FROM manpower_daily LIMIT 1")
            print(f"✅ manpower_daily.{col_name} already exists")
        except sqlite3.OperationalError:
            print(f"⚙️  Adding {col_name} to manpower_daily...")
            c.execute(f"ALTER TABLE manpower_daily ADD COLUMN {col_name} REAL DEFAULT 0")
            print(f"✅ {col_name} column added")
    
    conn.commit()
    conn.close()
    
    print("\n" + "="*60)
    print("✅ V3 MIGRATION COMPLETED!")
    print("="*60)
    print("\nNew features added:")
    print("✓ Packaging component costs")
    print("✓ Product kg-to-litre conversion factors")
    print("✓ Filling bulk poured tracking")
    print("✓ Manpower hours tracking")
    print("\nYou can now:")
    print("• Track costs for packaging components")
    print("• View consumption costs per product per month")
    print("• See all calculations in litres")
    print("• Track wastage at filling machine")
    print("• Log hours worked per department")
    print("="*60 + "\n")

if __name__ == "__main__":
    if os.path.exists(DB_NAME):
        print(f"\nFound database: {DB_NAME}")
        response = input("Do you want to add V3 features (costs, litres, hours)? (yes/no): ")
        if response.lower() in ['yes', 'y']:
            migrate_to_v3()
        else:
            print("Migration cancelled.")
    else:
        print(f"\nNo database found at {DB_NAME}")
        print("Please run the app first to create the database.")

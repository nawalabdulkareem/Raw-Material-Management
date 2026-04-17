"""
google_sheets_sync.py - REVISED V3
Syncs ALL data from manufacturing system to Google Sheets
Uses batch writes for speed; no resize() calls to avoid API errors.
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

try:
    from config import GOOGLE_SHEETS_ENABLED, GOOGLE_CREDENTIALS_FILE, GOOGLE_SHEET_ID
except ImportError:
    GOOGLE_SHEETS_ENABLED = False
    GOOGLE_CREDENTIALS_FILE = 'google_credentials.json'
    GOOGLE_SHEET_ID = None


def get_sheets_client():
    """Connect to Google Sheets"""
    if not GOOGLE_SHEETS_ENABLED:
        raise Exception("Google Sheets sync is not enabled in config.py")
    if not GOOGLE_SHEET_ID:
        raise Exception("GOOGLE_SHEET_ID not set in config.py")
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    return client


def get_or_create_worksheet(sheet, title, rows=1000, cols=20):
    """Get existing worksheet or create it. Never resize."""
    try:
        return sheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols)


def write_tab(worksheet, header, rows):
    """
    Clear the worksheet and write header + all data rows in one batch call.
    Uses clear() instead of resize() to avoid the 'cannot delete all rows' error.
    """
    worksheet.clear()
    all_rows = [header] + rows
    if all_rows:
        # update from A1 covering the full data block
        worksheet.update(all_rows, 'A1')


# ─────────────────────────────────────────────
# Individual tab sync functions
# ─────────────────────────────────────────────

def sync_raw_materials(conn, sheet):
    c = conn.cursor()
    c.execute("SELECT name, qty_kg, supplier, cost_per_kg FROM ingredients ORDER BY name")
    data = c.fetchall()

    header = ['Ingredient Name', 'Quantity (kg)', 'Supplier', 'Cost per kg (₹)', 'Total Value (₹)']
    rows = []
    for name, qty, supplier, cost in data:
        total = round((qty or 0) * (cost or 0), 4)
        rows.append([name, qty or 0, supplier or '', cost or 0, total])

    ws = get_or_create_worksheet(sheet, "Raw Materials")
    write_tab(ws, header, rows)
    return f"Raw Materials: {len(rows)} ingredients"


def sync_production(conn, sheet):
    c = conn.cursor()
    c.execute("""
        SELECT date, product_name, batch_number, batch_size_kg,
               actual_yield_kg, yield_loss_kg, yield_percentage, notes
        FROM bulk_production
        ORDER BY date DESC, id DESC
    """)
    data = c.fetchall()

    header = ['Date', 'Product', 'Batch Number', 'Batch Size (kg)',
              'Actual Yield (kg)', 'Yield Loss (kg)', 'Yield %', 'Notes']
    rows = []
    for row in data:
        rows.append([row[0], row[1], row[2] or '', row[3], row[4],
                     row[5], round(row[6], 4), row[7] or ''])

    ws = get_or_create_worksheet(sheet, "Production")
    write_tab(ws, header, rows)
    return f"Production: {len(rows)} batches"


def sync_filling(conn, sheet):
    c = conn.cursor()
    c.execute("""
        SELECT fo.id, fo.date, fo.product_name, fo.batch_number,
               fo.pack_size_kg, fo.bulk_used_kg, fo.actual_bottles_filled,
               fo.theoretical_bottles, fo.bottles_diff,
               fo.bottles_diff_percentage, fo.notes
        FROM filling_operations fo
        ORDER BY fo.date DESC, fo.id DESC
    """)
    filling_data = c.fetchall()

    header = ['Date', 'Product', 'Batch Number', 'Pack Size (kg)',
              'Bulk Used (kg)', 'Bottles Filled', 'Theoretical Bottles',
              'Difference', 'Diff %', 'Notes', 'Packaging Used']
    rows = []
    for row in filling_data:
        filling_id = row[0]
        # Get packaging used for this filling record
        c.execute("""
            SELECT component_name, quantity_used
            FROM filling_packaging_usage
            WHERE filling_id = ?
        """, (filling_id,))
        pkg = c.fetchall()
        pkg_str = ', '.join([f"{p[0]}: {p[1]}" for p in pkg]) if pkg else ''

        rows.append([
            row[1], row[2], row[3] or '', row[4], row[5],
            row[6], row[7], row[8], round(row[9], 4),
            row[10] or '', pkg_str
        ])

    ws = get_or_create_worksheet(sheet, "Filling")
    write_tab(ws, header, rows)
    return f"Filling: {len(rows)} records"


def sync_boxing(conn, sheet):
    c = conn.cursor()
    c.execute("""
        SELECT id, date, boxes_made, units_per_box, total_units_boxed, notes
        FROM boxing_operations
        ORDER BY date DESC, id DESC
    """)
    boxing_data = c.fetchall()

    header = ['Date', 'Boxes Made', 'Units per Box', 'Total Units Boxed',
              'Products', 'Packaging Used', 'Notes']
    rows = []
    for row in boxing_data:
        boxing_id = row[0]

        c.execute("""
            SELECT product_name, units_in_box
            FROM boxing_products WHERE boxing_id = ?
        """, (boxing_id,))
        prods = c.fetchall()
        prod_str = ', '.join([f"{p[0]}: {p[1]} units" for p in prods]) if prods else ''

        c.execute("""
            SELECT component_name, quantity_used
            FROM boxing_packaging_usage WHERE boxing_id = ?
        """, (boxing_id,))
        pkg = c.fetchall()
        pkg_str = ', '.join([f"{p[0]}: {p[1]}" for p in pkg]) if pkg else ''

        rows.append([row[1], row[2], row[3], row[4], prod_str, pkg_str, row[5] or ''])

    ws = get_or_create_worksheet(sheet, "Boxing")
    write_tab(ws, header, rows)
    return f"Boxing: {len(rows)} records"


def sync_packaging_inventory(conn, sheet):
    c = conn.cursor()
    c.execute("SELECT component_name, current_qty, cost_per_unit, supplier FROM packaging_components ORDER BY component_name")
    components = c.fetchall()

    header = ['Component Name', 'Current Quantity', 'Cost per Unit', 'Supplier', 'Total Value']
    rows = [[name, qty or 0, cost or 0, supplier or '', round((qty or 0) * (cost or 0), 4)] for name, qty, cost, supplier in components]

    ws = get_or_create_worksheet(sheet, "Packaging Inventory")
    write_tab(ws, header, rows)
    return f"Packaging Inventory: {len(rows)} components"


def sync_packaging_history(conn, sheet):
    c = conn.cursor()
    c.execute("""
        SELECT date, component_name, movement_type, quantity, reference_type, notes
        FROM packaging_movements
        ORDER BY date DESC, id DESC
        LIMIT 500
    """)
    movements = c.fetchall()

    header = ['Date', 'Component', 'In / Out', 'Quantity', 'Source', 'Description']
    rows = []
    for row in movements:
        date, component, mtype, qty, ref_type, notes = row
        # Build a human-readable description
        if ref_type == 'Restock' or ref_type == 'Initial':
            desc = f"{qty} {component} restocked on {date}"
        elif ref_type == 'Filling':
            desc = f"{qty} {component} used for filling on {date}"
        elif ref_type == 'Boxing':
            desc = f"{qty} {component} used for boxing on {date}"
        else:
            desc = notes or ''
        rows.append([date, component, mtype, qty, ref_type or '', desc])

    ws = get_or_create_worksheet(sheet, "Packaging History")
    write_tab(ws, header, rows)
    return f"Packaging History: {len(rows)} movements"


def sync_manpower(conn, sheet):
    c = conn.cursor()
    c.execute("""
        SELECT md.date,
               md.production_male, md.production_female,
               md.filling_male,    md.filling_female,
               md.boxing_male,     md.boxing_female,
               (SELECT COUNT(*)         FROM bulk_production     WHERE date = md.date) AS batches,
               (SELECT COALESCE(SUM(actual_bottles_filled), 0)
                FROM filling_operations WHERE date = md.date)                          AS bottles,
               (SELECT COALESCE(SUM(boxes_made), 0)
                FROM boxing_operations  WHERE date = md.date)                          AS boxes,
               md.notes
        FROM manpower_daily md
        ORDER BY md.date DESC
    """)
    data = c.fetchall()

    header = ['Date',
              'Production Male', 'Production Female',
              'Filling Male',    'Filling Female',
              'Boxing Male',     'Boxing Female',
              'Batches Made', 'Bottles Filled', 'Boxes Made', 'Notes']
    rows = []
    for row in data:
        rows.append([
            row[0],
            row[1] or 0, row[2] or 0,
            row[3] or 0, row[4] or 0,
            row[5] or 0, row[6] or 0,
            row[7] or 0, row[8] or 0, row[9] or 0,
            row[10] or ''
        ])

    ws = get_or_create_worksheet(sheet, "Manpower")
    write_tab(ws, header, rows)
    return f"Manpower: {len(rows)} records"


def sync_audit(conn, sheet):
    c = conn.cursor()
    c.execute("""
        SELECT a.audit_date, ai.ingredient_name,
               ai.previous_weight_kg, ai.expected_weight_kg,
               ai.actual_weight_kg,  ai.difference_kg,
               a.notes
        FROM audits a
        JOIN audit_ingredients ai ON ai.audit_id = a.id
        ORDER BY a.audit_date DESC, ai.ingredient_name
    """)
    data = c.fetchall()

    header = ['Audit Date', 'Ingredient',
              'Previous Weight (kg)', 'Expected Weight (kg)',
              'Actual Weight (kg)',   'Difference (kg)', 'Notes']
    rows = []
    for row in data:
        rows.append([row[0], row[1], row[2], row[3], row[4],
                     round(row[5], 4), row[6] or ''])

    ws = get_or_create_worksheet(sheet, "Audit")
    write_tab(ws, header, rows)
    return f"Audit: {len(rows)} records"


# ─────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────

def sync_all_data_to_sheets(conn):
    """Sync every tab. Returns a result dict for the API response."""
    if not GOOGLE_SHEETS_ENABLED:
        return {
            'success': False,
            'message': 'Google Sheets sync is disabled. Set GOOGLE_SHEETS_ENABLED = True in config.py'
        }

    try:
        client = get_sheets_client()
        sheet  = client.open_by_key(GOOGLE_SHEET_ID)

        results = [
            sync_raw_materials(conn, sheet),
            sync_production(conn, sheet),
            sync_filling(conn, sheet),
            sync_boxing(conn, sheet),
            sync_packaging_inventory(conn, sheet),
            sync_packaging_history(conn, sheet),
            sync_manpower(conn, sheet),
            sync_audit(conn, sheet),
        ]

        return {
            'success': True,
            'message': f'✅ All 8 tabs synced successfully! ({datetime.now().strftime("%H:%M:%S")})',
            'details': results
        }

    except FileNotFoundError:
        return {
            'success': False,
            'message': f'Credentials file not found: {GOOGLE_CREDENTIALS_FILE}. '
                       'Download it from Google Cloud Console and place it in your manufacturing_system folder.'
        }
    except gspread.exceptions.SpreadsheetNotFound:
        return {
            'success': False,
            'message': 'Google Sheet not found. Check GOOGLE_SHEET_ID in config.py.'
        }
    except PermissionError:
        return {
            'success': False,
            'message': 'Permission denied. Share the Google Sheet with your service account email '
                       '(found in google_credentials.json under "client_email") and give it Editor access.'
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'message': f'Sync error: {str(e)}'
        }

"""
NEW API ENDPOINTS FOR V3 - Add these to app.py

Copy and paste these functions into your app.py file before the line:
    if __name__ == '__main__':

These endpoints provide:
- Monthly consumption cost tracking
- Raw materials history with filtering
- Dashboard consumption costs
- Wastage reports in litres
"""

# ============================================================================
# RAW MATERIALS MOVEMENT HISTORY WITH FILTERING
# ============================================================================

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
            SELECT date, ingredient_name, movement_type, quantity_kg, 
                   reference_type, notes, created_at
            FROM rm_stock_movements
            WHERE 1=1
        """
        params = []
        
        if ingredient_filter:
            ingredients = [i.strip() for i in ingredient_filter.split(',')]
            placeholders = ','.join(['?' for _ in ingredients])
            query += f" AND ingredient_name IN ({placeholders})"
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
# MONTHLY CONSUMPTION COST CALCULATION
# ============================================================================

@app.route('/api/audit/monthly-consumption-cost')
def get_monthly_consumption_cost():
    """Calculate consumption costs per product for a month"""
    try:
        month = request.args.get('month', datetime.now().strftime('%Y-%m'))
        product_filter = request.args.get('products', '')  # Comma-separated product names
        
        conn = get_db()
        c = conn.cursor()
        
        # Date range for the month
        start_date = f"{month}-01"
        from calendar import monthrange
        year, mon = map(int, month.split('-'))
        last_day = monthrange(year, mon)[1]
        end_date = f"{month}-{last_day:02d}"
        
        results = []
        
        # Get list of products to analyze
        if product_filter:
            products = [p.strip() for p in product_filter.split(',')]
        else:
            c.execute("SELECT DISTINCT product_name FROM bulk_production WHERE date >= ? AND date <= ?", 
                     (start_date, end_date))
            products = [row[0] for row in c.fetchall()]
        
        for product_name in products:
            product_cost = {
                'product': product_name,
                'raw_materials': {},
                'packaging': {},
                'total_rm_cost': 0,
                'total_packaging_cost': 0,
                'total_cost': 0
            }
            
            # 1. Calculate RM costs for this product
            c.execute("SELECT id FROM products WHERE name = ?", (product_name,))
            product_row = c.fetchone()
            if product_row:
                product_id = product_row['id']
                
                # Get total batch size produced this month
                c.execute("""
                    SELECT SUM(batch_size_kg) as total_kg
                    FROM bulk_production
                    WHERE product_name = ? AND date >= ? AND date <= ?
                """, (product_name, start_date, end_date))
                total_production = c.fetchone()['total_kg'] or 0
                
                if total_production > 0:
                    # Get formula ingredients and their costs
                    c.execute("""
                        SELECT pi.ingredient_name, pi.percentage, i.cost_per_kg
                        FROM product_ingredients pi
                        JOIN ingredients i ON i.name = pi.ingredient_name
                        WHERE pi.product_id = ?
                    """, (product_id,))
                    
                    for row in c.fetchall():
                        ing_name = row['ingredient_name']
                        percentage = row['percentage']
                        cost_per_kg = row['cost_per_kg'] or 0
                        
                        kg_used = total_production * (percentage / 100.0)
                        cost = kg_used * cost_per_kg
                        
                        product_cost['raw_materials'][ing_name] = {
                            'kg_used': round(kg_used, 4),
                            'cost_per_kg': cost_per_kg,
                            'total_cost': round(cost, 2)
                        }
                        product_cost['total_rm_cost'] += cost
            
            # 2. Calculate packaging costs for this product
            c.execute("""
                SELECT fpu.component_name, SUM(fpu.quantity_used) as total_qty
                FROM filling_packaging_usage fpu
                JOIN filling_operations fo ON fo.id = fpu.filling_id
                WHERE fo.product_name = ? AND fo.date >= ? AND fo.date <= ?
                GROUP BY fpu.component_name
            """, (product_name, start_date, end_date))
            
            for row in c.fetchall():
                comp_name = row['component_name']
                qty_used = row['total_qty']
                
                c.execute("SELECT cost_per_unit FROM packaging_components WHERE component_name = ?", 
                         (comp_name,))
                cost_row = c.fetchone()
                cost_per_unit = cost_row['cost_per_unit'] if cost_row else 0
                
                cost = qty_used * cost_per_unit
                product_cost['packaging'][comp_name] = {
                    'qty_used': qty_used,
                    'cost_per_unit': cost_per_unit,
                    'total_cost': round(cost, 2)
                }
                product_cost['total_packaging_cost'] += cost
            
            # Also get boxing packaging
            c.execute("""
                SELECT bpu.component_name, SUM(bpu.quantity_used) as total_qty
                FROM boxing_packaging_usage bpu
                JOIN boxing_operations bo ON bo.id = bpu.boxing_id
                JOIN boxing_products bp ON bp.boxing_id = bo.id
                WHERE bp.product_name = ? AND bo.date >= ? AND bo.date <= ?
                GROUP BY bpu.component_name
            """, (product_name, start_date, end_date))
            
            for row in c.fetchall():
                comp_name = row['component_name']
                qty_used = row['total_qty']
                
                c.execute("SELECT cost_per_unit FROM packaging_components WHERE component_name = ?", 
                         (comp_name,))
                cost_row = c.fetchone()
                cost_per_unit = cost_row['cost_per_unit'] if cost_row else 0
                
                cost = qty_used * cost_per_unit
                
                if comp_name in product_cost['packaging']:
                    product_cost['packaging'][comp_name]['qty_used'] += qty_used
                    product_cost['packaging'][comp_name]['total_cost'] += round(cost, 2)
                else:
                    product_cost['packaging'][comp_name] = {
                        'qty_used': qty_used,
                        'cost_per_unit': cost_per_unit,
                        'total_cost': round(cost, 2)
                    }
                product_cost['total_packaging_cost'] += cost
            
            product_cost['total_cost'] = product_cost['total_rm_cost'] + product_cost['total_packaging_cost']
            product_cost['total_rm_cost'] = round(product_cost['total_rm_cost'], 2)
            product_cost['total_packaging_cost'] = round(product_cost['total_packaging_cost'], 2)
            product_cost['total_cost'] = round(product_cost['total_cost'], 2)
            
            results.append(product_cost)
        
        conn.close()
        return jsonify({'success': True, 'data': results, 'month': month})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# DASHBOARD - CONSUMPTION COSTS
# ============================================================================

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


# ============================================================================
# DASHBOARD - WASTAGE REPORT IN LITRES
# ============================================================================

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
                
                # Also track bottle difference
                bottle_diff = actual - theoretical
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
                wastage_data['total_wastage_litres'] += waste_litres
            
            wastage_data['filling_wastage'] = filling_losses
            wastage_data['total_wastage_litres'] = round(wastage_data['total_wastage_litres'], 4)
            
            results.append(wastage_data)
        
        conn.close()
        return jsonify({'success': True, 'data': results, 'month': month})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# UPDATE EXISTING ENDPOINTS TO SUPPORT NEW FIELDS
# ============================================================================

# NOTE: These are updates to existing endpoints. 
# Find the corresponding functions in your app.py and modify them.

"""
UPDATES NEEDED FOR EXISTING ENDPOINTS:

1. /api/packaging/components/add - Add cost_per_unit to insert:
   c.execute('''
       INSERT INTO packaging_components (component_name, current_qty, cost_per_unit)
       VALUES (?, ?, ?)
   ''', (data['component_name'], data['quantity'], data.get('cost_per_unit', 0)))

2. /api/products/add - Add kg_to_litre_factor to insert:
   c.execute("INSERT INTO products (name, kg_to_litre_factor) VALUES (?, ?)", 
             (data['name'], data.get('kg_to_litre_factor', 1.0)))

3. /api/filling/confirm - Add bulk_poured_kg to insert:
   c.execute('''
       INSERT INTO filling_operations
       (date, product_name, batch_number, pack_size_kg, bulk_used_kg, bulk_poured_kg,
        actual_bottles_filled, theoretical_bottles, bottles_diff, bottles_diff_percentage, notes)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
   ''', (date, product_name, batch_number, pack_size, bulk_used, bulk_poured,
         actual_bottles, theoretical, diff, diff_pct, notes))

4. /api/manpower/add - Add hours fields to insert:
   c.execute('''
       INSERT INTO manpower_daily 
       (date, production_male, production_female, production_hours,
        filling_male, filling_female, filling_hours,
        boxing_male, boxing_female, boxing_hours, notes)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ...
   ''')
"""

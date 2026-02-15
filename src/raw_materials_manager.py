#!/usr/bin/env python3
"""
raw_materials_manager.py

Single-file desktop app (Tkinter + sqlite3) for:
- Stock (ingredients with supplier, serial numbers, alphabetical, alternating row colors)
- Products (save up to 30 ingredient rows; autocomplete ingredient selection from stock)
- Production (plan, confirm production, record manual date/time and batch number,
  history with batch shown, delete a production and restore stock using the production's unique id)

Data stored in raw_materials.db in the same folder as this script.
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import sqlite3
import os
from datetime import datetime

DB_NAME = "raw_materials.db"
MAX_ROWS = 30  # number of ingredient rows allowed per product

# --- Database utilities ---
def init_db():
    """Create DB and tables (add missing columns if older DB exists)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # Ingredients table
    c.execute("""
    CREATE TABLE IF NOT EXISTS ingredients (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        qty_kg REAL NOT NULL DEFAULT 0,
        supplier TEXT DEFAULT ''
    )
    """)

    # Ensure supplier column exists (safe attempt)
    try:
        c.execute("ALTER TABLE ingredients ADD COLUMN supplier TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    # Products table
    c.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL
    )
    """)

    # Product ingredients / formula
    c.execute("""
    CREATE TABLE IF NOT EXISTS product_ingredients (
        id INTEGER PRIMARY KEY,
        product_id INTEGER NOT NULL,
        ingredient_name TEXT NOT NULL,
        percentage REAL NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    """)

    # Productions table (includes batch_number).
    # If DB already exists without batch_number, we attempt to add it after creating the table.
    c.execute("""
    CREATE TABLE IF NOT EXISTS productions (
        id INTEGER PRIMARY KEY,
        product_id INTEGER,
        product_name TEXT,
        kilos_produced REAL,
        produced_at TEXT,
        batch_number TEXT
    )
    """)
    # ensure batch_number exists if older DB
    try:
        c.execute("ALTER TABLE productions ADD COLUMN batch_number TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

def get_connection():
    return sqlite3.connect(DB_NAME)

# --- UI helpers ---
class AutocompleteCombobox(ttk.Combobox):
    """
    A simple combobox that filters its values as the user types (case-insensitive substring match).
    Set values with combobox.set_values(list_of_strings)
    """
    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self._orig_values = []
        self.bind("<KeyRelease>", self._on_keyrelease)
        self.bind("<<ComboboxSelected>>", self._on_select)

    def set_values(self, values):
        self._orig_values = list(values)
        self['values'] = self._orig_values

    def _on_keyrelease(self, event):
        typed = self.get()
        if typed == "":
            self['values'] = self._orig_values
        else:
            low = typed.lower()
            filtered = [v for v in self._orig_values if low in v.lower()]
            self['values'] = filtered
        try:
            if self['values']:
                self.event_generate('<Down>')
        except Exception:
            pass

    def _on_select(self, event):
        pass

# --- App GUI ---
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Raw Materials Manager")
        self.geometry("1000x650")
        self.minsize(900, 600)

        style = ttk.Style(self)
        try:
            style.theme_use(style.theme_use())
        except Exception:
            pass
        style.configure("Treeview", rowheight=24)
        style.configure("Treeview.Heading", font=(None, 10, "bold"))
        style.configure("TNotebook.Tab", font=(None, 10, "bold"))

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill='both', expand=True)

        self.stock_frame = StockFrame(self.nb)
        self.products_frame = ProductsFrame(self.nb)
        self.production_frame = ProductionFrame(self.nb)

        self.nb.add(self.stock_frame, text="Stock")
        self.nb.add(self.products_frame, text="Products")
        self.nb.add(self.production_frame, text="Production")

        self.nb.bind("<<NotebookTabChanged>>", self.on_tab_change)

    def on_tab_change(self, event):
        self.stock_frame.refresh()
        self.products_frame.refresh()
        self.production_frame.refresh()

# --- Stock Page ---
class StockFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.create_ui()
        self.refresh()

    def create_ui(self):
        top = ttk.Frame(self)
        top.pack(fill='x', pady=8, padx=8)

        add_btn = ttk.Button(top, text="Add Ingredient", command=self.add_ingredient)
        add_btn.pack(side='left', padx=4)
        edit_btn = ttk.Button(top, text="Edit Selected", command=self.edit_selected)
        edit_btn.pack(side='left', padx=4)
        restock_btn = ttk.Button(top, text="Restock Selected (+kg)", command=self.restock_selected)
        restock_btn.pack(side='left', padx=4)
        delete_btn = ttk.Button(top, text="Delete Selected", command=self.delete_selected)
        delete_btn.pack(side='left', padx=4)

        export_btn = ttk.Button(top, text="Backup DB", command=self.backup_db)
        export_btn.pack(side='right', padx=4)

        cols = ("sno", "name", "qty_kg", "supplier")
        self.tree = ttk.Treeview(self, columns=cols, show='headings', height=20)
        self.tree.heading("sno", text="SNo")
        self.tree.heading("name", text="Ingredient Name")
        self.tree.heading("qty_kg", text="Quantity (kg)")
        self.tree.heading("supplier", text="Supplier")
        self.tree.column("sno", width=60, anchor='center')
        self.tree.column("name", width=420)
        self.tree.column("qty_kg", width=140, anchor='e')
        self.tree.column("supplier", width=320)
        self.tree.pack(fill='both', expand=True, padx=8, pady=8)

        # alternating row backgrounds
        self.tree.tag_configure('odd', background='#ffffff')
        self.tree.tag_configure('even', background='#f3f3f3')

    def refresh(self):
        # Clear existing
        for r in self.tree.get_children():
            self.tree.delete(r)
        conn = get_connection()
        c = conn.cursor()
        # ORDER BY name (case-insensitive) for alphabetical arrangement
        c.execute("SELECT name, qty_kg, supplier FROM ingredients ORDER BY name COLLATE NOCASE")
        rows = c.fetchall()
        conn.close()
        for i, (name, qty, supplier) in enumerate(rows, start=1):
            tag = 'odd' if i % 2 else 'even'
            self.tree.insert('', 'end', values=(i, name, round(qty, 6), supplier or ""), tags=(tag,))

    def add_ingredient(self):
        dialog = IngredientDialog(self, title="Add Ingredient")
        self.wait_window(dialog)
        if dialog.result:
            name, qty, supplier = dialog.result
            conn = get_connection()
            c = conn.cursor()
            try:
                c.execute("INSERT INTO ingredients(name, qty_kg, supplier) VALUES (?,?,?)", (name, qty, supplier))
                conn.commit()
            except sqlite3.IntegrityError:
                messagebox.showerror("Error", "Ingredient name already exists.")
            conn.close()
            self.refresh()

    def edit_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select", "Please select an ingredient.")
            return
        item = self.tree.item(sel[0])
        name = item['values'][1]
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT qty_kg, supplier FROM ingredients WHERE name=?", (name,))
        row = c.fetchone()
        conn.close()
        qty = row[0] if row else 0
        supplier = row[1] if row else ""
        dialog = IngredientDialog(self, title="Edit Ingredient", name=name, qty=qty, supplier=supplier, disable_name=True)
        self.wait_window(dialog)
        if dialog.result:
            _, newqty, newsupplier = dialog.result
            conn = get_connection()
            c = conn.cursor()
            c.execute("UPDATE ingredients SET qty_kg=?, supplier=? WHERE name=?", (newqty, newsupplier, name))
            conn.commit()
            conn.close()
            self.refresh()

    def restock_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select", "Please select an ingredient.")
            return
        item = self.tree.item(sel[0])
        name = item['values'][1]
        ans = simpledialog.askfloat("Restock", f"Add how many kg to '{name}'?", minvalue=0.0)
        if ans is None:
            return
        conn = get_connection()
        c = conn.cursor()
        c.execute("UPDATE ingredients SET qty_kg = qty_kg + ? WHERE name = ?", (ans, name))
        conn.commit()
        conn.close()
        self.refresh()

    def delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select", "Please select an ingredient.")
            return
        item = self.tree.item(sel[0])
        name = item['values'][1]
        if messagebox.askyesno("Delete", f"Delete ingredient '{name}'? This cannot be undone."):
            conn = get_connection()
            c = conn.cursor()
            c.execute("DELETE FROM ingredients WHERE name=?", (name,))
            conn.commit()
            conn.close()
            self.refresh()

    def backup_db(self):
        path = simpledialog.askstring("Backup DB", "Enter filename to save backup (e.g., backup.db):", initialvalue="backup.db")
        if not path:
            return
        try:
            import shutil
            shutil.copyfile(DB_NAME, path)
            messagebox.showinfo("Backup", f"Database backed up to {path}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not backup: {e}")

class IngredientDialog(tk.Toplevel):
    def __init__(self, parent, title="Ingredient", name="", qty=0.0, supplier="", disable_name=False):
        super().__init__(parent)
        self.result = None
        self.title(title)
        self.transient(parent)
        self.grab_set()

        ttk.Label(self, text="Ingredient name:").grid(row=0, column=0, sticky='w', padx=8, pady=6)
        self.name_var = tk.StringVar(value=name)
        self.name_entry = ttk.Entry(self, textvariable=self.name_var, width=40)
        self.name_entry.grid(row=0, column=1, padx=8, pady=6)

        ttk.Label(self, text="Quantity (kg):").grid(row=1, column=0, sticky='w', padx=8, pady=6)
        self.qty_var = tk.DoubleVar(value=round(qty, 6))
        self.qty_entry = ttk.Entry(self, textvariable=self.qty_var, width=20)
        self.qty_entry.grid(row=1, column=1, padx=8, pady=6, sticky='w')

        ttk.Label(self, text="Supplier:").grid(row=2, column=0, sticky='w', padx=8, pady=6)
        self.supplier_var = tk.StringVar(value=supplier)
        self.supplier_entry = ttk.Entry(self, textvariable=self.supplier_var, width=40)
        self.supplier_entry.grid(row=2, column=1, padx=8, pady=6, sticky='w')

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(btn_frame, text="OK", command=self.on_ok).pack(side='left', padx=6)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side='left')

        if disable_name:
            self.name_entry.config(state='disabled')

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.name_entry.focus_set()

    def on_ok(self):
        name = self.name_var.get().strip()
        try:
            qty = float(self.qty_var.get())
        except Exception:
            messagebox.showerror("Error", "Quantity must be a number.")
            return
        supplier = self.supplier_var.get().strip()
        if not name:
            messagebox.showerror("Error", "Name is required.")
            return
        self.result = (name, qty, supplier)
        self.destroy()

# --- Products Page ---
class ProductsFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.create_ui()
        self.refresh()

    def create_ui(self):
        top = ttk.Frame(self)
        top.pack(fill='x', padx=8, pady=6)
        add_btn = ttk.Button(top, text="Add New Product", command=self.add_product)
        add_btn.pack(side='left', padx=4)
        edit_btn = ttk.Button(top, text="Edit Selected", command=self.edit_product)
        edit_btn.pack(side='left', padx=4)
        delete_btn = ttk.Button(top, text="Delete Selected", command=self.delete_product)
        delete_btn.pack(side='left', padx=4)

        # Product list
        cols = ("name",)
        self.tree = ttk.Treeview(self, columns=cols, show='headings', height=12)
        self.tree.heading("name", text="Product Name")
        self.tree.column("name", width=500)
        self.tree.pack(fill='x', padx=8, pady=6)

        # Product detail: show ingredients
        detail_frame = ttk.LabelFrame(self, text="Product Ingredients (percentage)")
        detail_frame.pack(fill='both', expand=True, padx=8, pady=6)
        self.detail_tree = ttk.Treeview(detail_frame, columns=("ingredient", "perc"), show='headings', height=10)
        self.detail_tree.heading("ingredient", text="Ingredient")
        self.detail_tree.heading("perc", text="%")
        self.detail_tree.column("ingredient", width=400)
        self.detail_tree.column("perc", width=120, anchor='e')
        self.detail_tree.pack(fill='both', expand=True, padx=4, pady=4)

        self.tree.bind("<<TreeviewSelect>>", self.on_select)

    def refresh(self):
        for r in self.tree.get_children():
            self.tree.delete(r)
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT id, name FROM products ORDER BY id")
        self.products = c.fetchall()
        for pid, name in self.products:
            self.tree.insert('', 'end', iid=str(pid), values=(name,))
        conn.close()
        self.detail_tree.delete(*self.detail_tree.get_children())

    def on_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        pid = int(sel[0])
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT ingredient_name, percentage FROM product_ingredients WHERE product_id=? ORDER BY id", (pid,))
        rows = c.fetchall()
        conn.close()
        self.detail_tree.delete(*self.detail_tree.get_children())
        for ing, perc in rows:
            self.detail_tree.insert('', 'end', values=(ing, round(perc, 6)))

    def add_product(self):
        dialog = ProductEditor(self, title="Add New Product")
        self.wait_window(dialog)
        if dialog.saved:
            self.refresh()

    def edit_product(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select", "Please select a product to edit.")
            return
        pid = int(sel[0])
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM products WHERE id=?", (pid,))
        name = c.fetchone()[0]
        c.execute("SELECT ingredient_name, percentage FROM product_ingredients WHERE product_id=? ORDER BY id", (pid,))
        items = c.fetchall()
        conn.close()
        dialog = ProductEditor(self, title="Edit Product", product_id=pid, product_name=name, items=items)
        self.wait_window(dialog)
        if dialog.saved:
            self.refresh()

    def delete_product(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select", "Please select a product to delete.")
            return
        pid = int(sel[0])
        name = self.tree.item(sel[0])['values'][0]
        if messagebox.askyesno("Delete", f"Delete product '{name}' and its formula?"):
            conn = get_connection()
            c = conn.cursor()
            c.execute("DELETE FROM product_ingredients WHERE product_id=?", (pid,))
            c.execute("DELETE FROM products WHERE id=?", (pid,))
            conn.commit()
            conn.close()
            self.refresh()

class ProductEditor(tk.Toplevel):
    def __init__(self, parent, title="Product Editor", product_id=None, product_name="", items=None):
        super().__init__(parent)
        self.saved = False
        self.product_id = product_id
        self.title(title)
        self.transient(parent)
        self.grab_set()
        self.geometry("820x650")

        ttk.Label(self, text="Product name:").pack(anchor='w', padx=8, pady=4)
        self.name_var = tk.StringVar(value=product_name)
        ttk.Entry(self, textvariable=self.name_var, width=60).pack(anchor='w', padx=8)

        # Frame for grid (ingredient name + percentage) with scrollbar
        frame = ttk.Frame(self)
        frame.pack(fill='both', expand=True, padx=8, pady=8)
        canvas = tk.Canvas(frame)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        self.inner = ttk.Frame(canvas)
        self.inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.inner, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        header = ttk.Frame(self.inner)
        header.grid(row=0, column=0, sticky='ew', pady=4)
        ttk.Label(header, text="Row").grid(row=0, column=0, padx=4)
        ttk.Label(header, text="Ingredient").grid(row=0, column=1, padx=4)
        ttk.Label(header, text="%").grid(row=0, column=2, padx=4)

        # get ingredient list for autocomplete
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM ingredients ORDER BY name COLLATE NOCASE")
        ing_list = [r[0] for r in c.fetchall()]
        conn.close()

        self.rows = []
        for i in range(MAX_ROWS):
            rowf = ttk.Frame(self.inner)
            rowf.grid(row=i+1, column=0, sticky='w', pady=2)
            ttk.Label(rowf, text=str(i+1)).grid(row=0, column=0, padx=4)
            ing_var = tk.StringVar()
            ing_cb = AutocompleteCombobox(rowf, textvariable=ing_var, width=40)
            ing_cb.set_values(ing_list)
            ing_cb.grid(row=0, column=1, padx=4)
            perc_var = tk.DoubleVar(value=0.0)
            ttk.Entry(rowf, textvariable=perc_var, width=10).grid(row=0, column=2, padx=4)
            self.rows.append((ing_cb, ing_var, perc_var))

        if items:
            for i, (ing, perc) in enumerate(items):
                if i < MAX_ROWS:
                    self.rows[i][0].set_values(ing_list)
                    self.rows[i][1].set(ing)
                    self.rows[i][2].set(perc)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill='x', padx=8, pady=8)
        ttk.Button(btn_frame, text="Save", command=self.on_save).pack(side='left', padx=6)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side='left', padx=6)
        ttk.Label(btn_frame, text="Tip: leave unused rows empty. Percentages can be decimals.").pack(side='right')

    def on_save(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Error", "Product name required.")
            return
        items = []
        total_perc = 0.0
        for ing_cb, ing_var, perc_var in self.rows:
            ing = ing_var.get().strip()
            if ing:
                try:
                    perc = float(perc_var.get())
                except Exception:
                    messagebox.showerror("Error", f"Invalid percentage for ingredient '{ing}'.")
                    return
                if perc <= 0:
                    messagebox.showerror("Error", f"Percentage for '{ing}' must be > 0.")
                    return
                items.append((ing, perc))
                total_perc += perc

        if len(items) == 0:
            messagebox.showerror("Error", "At least one ingredient required.")
            return

        conn = get_connection()
        c = conn.cursor()
        if self.product_id is None:
            try:
                c.execute("INSERT INTO products(name) VALUES (?)", (name,))
                pid = c.lastrowid
            except sqlite3.IntegrityError:
                messagebox.showerror("Error", "Product name already exists.")
                conn.close()
                return
        else:
            pid = self.product_id
            c.execute("UPDATE products SET name=? WHERE id=?", (name, pid))
            c.execute("DELETE FROM product_ingredients WHERE product_id=?", (pid,))
        for ing, perc in items:
            c.execute("INSERT INTO product_ingredients(product_id, ingredient_name, percentage) VALUES (?,?,?)",
                      (pid, ing, perc))
        conn.commit()
        conn.close()
        self.saved = True
        self.destroy()

# --- Production Page ---
class ProductionFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.create_ui()
        self.refresh()

    def create_ui(self):
        top = ttk.Frame(self)
        top.pack(fill='x', padx=8, pady=6)
        ttk.Label(top, text="Select Product:").pack(side='left', padx=4)
        self.product_combo = ttk.Combobox(top, state='readonly', width=40)
        self.product_combo.pack(side='left', padx=4)
        ttk.Label(top, text="Kilos to make:").pack(side='left', padx=6)
        self.kilos_var = tk.DoubleVar(value=1.0)
        ttk.Entry(top, textvariable=self.kilos_var, width=8).pack(side='left')

        check_btn = ttk.Button(top, text="Check Requirements", command=self.check_requirements)
        check_btn.pack(side='left', padx=6)
        confirm_btn = ttk.Button(top, text="Confirm Production (Subtract from stock)", command=self.confirm_production)
        confirm_btn.pack(side='left', padx=6)

        cols = ("ingredient", "percentage", "required_kg", "available_kg")
        self.tree = ttk.Treeview(self, columns=cols, show='headings', height=18)
        self.tree.heading("ingredient", text="Ingredient")
        self.tree.heading("percentage", text="%")
        self.tree.heading("required_kg", text="Required (kg)")
        self.tree.heading("available_kg", text="Available (kg)")
        self.tree.column("ingredient", width=400)
        self.tree.column("percentage", width=80, anchor='e')
        self.tree.column("required_kg", width=120, anchor='e')
        self.tree.column("available_kg", width=120, anchor='e')
        self.tree.pack(fill='both', expand=True, padx=8, pady=8)

        self.tree.tag_configure('insufficient', foreground='red')

        hist_frame = ttk.LabelFrame(self, text="Recent Productions")
        hist_frame.pack(fill='both', expand=False, padx=8, pady=6)
        delete_btn = ttk.Button(hist_frame, text="Delete Selected Production", command=self.delete_production)
        delete_btn.pack(side='right', padx=4, pady=4)

        # history includes batch column; we set iid to production.id for safe unique deletion
        self.hist_tree = ttk.Treeview(hist_frame, columns=("product", "kilos", "date", "batch"), show='headings', height=6)
        self.hist_tree.heading("product", text="Product")
        self.hist_tree.heading("kilos", text="Kilos")
        self.hist_tree.heading("date", text="Date")
        self.hist_tree.heading("batch", text="Batch No.")
        self.hist_tree.column("product", width=360)
        self.hist_tree.column("kilos", width=80, anchor='e')
        self.hist_tree.column("date", width=200)
        self.hist_tree.column("batch", width=120)
        self.hist_tree.pack(fill='both', expand=True, padx=4, pady=4)

        hist_scroll = ttk.Scrollbar(hist_frame, orient='vertical', command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=hist_scroll.set)
        hist_scroll.pack(side='right', fill='y')

    def refresh(self):
        # load products into combobox
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT id, name FROM products ORDER BY id")
        rows = c.fetchall()
        conn.close()
        self.products = rows
        names = [r[1] for r in rows]
        self.product_combo['values'] = names

        # refresh history (unique by production.id)
        self.hist_tree.delete(*self.hist_tree.get_children())
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT id, product_name, kilos_produced, produced_at, batch_number FROM productions ORDER BY produced_at DESC, id DESC")
        for prod_id, prod_name, kilos, date, batch in c.fetchall():
            # use prod_id as iid so we can reliably delete by id
            self.hist_tree.insert('', 'end', iid=str(prod_id), values=(prod_name, round(kilos, 6), date or "", batch or ""))
        conn.close()

    def check_requirements(self):
        sel = self.product_combo.get()
        if not sel:
            messagebox.showinfo("Select", "Please select a product.")
            return
        try:
            kilos = float(self.kilos_var.get())
        except Exception:
            messagebox.showerror("Error", "Invalid kilos value.")
            return

        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM products WHERE name=?", (sel,))
        pid_row = c.fetchone()
        if not pid_row:
            messagebox.showerror("Error", "Product not found in database.")
            conn.close()
            return
        pid = pid_row[0]
        c.execute("SELECT ingredient_name, percentage FROM product_ingredients WHERE product_id=?", (pid,))
        items = c.fetchall()
        avail = {}
        c.execute("SELECT name, qty_kg FROM ingredients")
        for name, qty in c.fetchall():
            avail[name] = qty
        conn.close()

        self.tree.delete(*self.tree.get_children())
        self.requirements = []
        insufficient = False
        for ing, perc in items:
            required_kg = kilos * (perc / 100.0)
            available_kg = avail.get(ing, 0.0)
            tag = ''
            if available_kg + 1e-9 < required_kg:
                tag = 'insufficient'
                insufficient = True
            self.tree.insert('', 'end', values=(ing, round(perc, 6), round(required_kg, 6), round(available_kg, 6)), tags=(tag,))
            self.requirements.append((ing, required_kg, available_kg))
        if insufficient:
            messagebox.showwarning("Insufficient", "Some ingredients are insufficient. They are shown in red.")
        else:
            messagebox.showinfo("OK", "You have sufficient stock for the planned production.")

    def confirm_production(self):
        sel = self.product_combo.get()
        if not sel:
            messagebox.showinfo("Select", "Please select a product.")
            return
        try:
            kilos = float(self.kilos_var.get())
        except Exception:
            messagebox.showerror("Error", "Invalid kilos value.")
            return

        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM products WHERE name=?", (sel,))
        pid_row = c.fetchone()
        if not pid_row:
            messagebox.showerror("Error", "Product not found.")
            conn.close()
            return
        pid = pid_row[0]
        c.execute("SELECT ingredient_name, percentage FROM product_ingredients WHERE product_id=?", (pid,))
        items = c.fetchall()

        shortages = []
        for ing, perc in items:
            required_kg = kilos * (perc / 100.0)
            c.execute("SELECT qty_kg FROM ingredients WHERE name=?", (ing,))
            row = c.fetchone()
            available = row[0] if row else 0.0
            if available + 1e-9 < required_kg:
                shortages.append((ing, required_kg, available))
        if shortages:
            msg = "Cannot confirm production. These ingredients are insufficient:\n"
            for ing, req, av in shortages:
                msg += f" - {ing}: need {round(req, 6)} kg, have {round(av, 6)} kg\n"
            messagebox.showerror("Insufficient", msg)
            conn.close()
            return

        date_input = simpledialog.askstring("Production date/time",
                                            "Enter production date/time (YYYY-MM-DD HH:MM:SS) or leave blank:",
                                            initialvalue="")
        produced_at = date_input.strip() if date_input and date_input.strip() else ""

        batch_number = simpledialog.askstring("Batch Number", "Enter batch number (optional):", initialvalue="")
        batch_number = batch_number.strip() if batch_number else ""

        # subtract and insert production record
        for ing, perc in items:
            required_kg = kilos * (perc / 100.0)
            c.execute("UPDATE ingredients SET qty_kg = qty_kg - ? WHERE name=?", (required_kg, ing))

        c.execute("""
            INSERT INTO productions(product_id, product_name, kilos_produced, produced_at, batch_number)
            VALUES (?,?,?,?,?)
        """, (pid, sel, kilos, produced_at, batch_number))

        conn.commit()
        conn.close()
        messagebox.showinfo("Done", "Production confirmed and stock updated.")
        self.refresh()
        try:
            app.stock_frame.refresh()
        except Exception:
            pass
        # refresh requirement display (recalculate availability)
        self.check_requirements()

    def delete_production(self):
        sel = self.hist_tree.selection()
        if not sel:
            messagebox.showinfo("Select", "Please select a production record to delete.")
            return

        # Since we used production.id as the tree iid, take that
        prod_iid = sel[0]
        try:
            prod_id = int(prod_iid)
        except Exception:
            messagebox.showerror("Error", "Unable to determine production id for deletion.")
            return

        # Retrieve the production details from DB (to be robust and accurate)
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT product_id, product_name, kilos_produced, produced_at, batch_number FROM productions WHERE id=?", (prod_id,))
        rec = c.fetchone()
        if not rec:
            conn.close()
            messagebox.showerror("Error", "Production record not found in database.")
            return

        product_id, product_name, kilos, produced_at, batch_number = rec

        # Confirm delete (show batch so user is sure)
        if not messagebox.askyesno("Delete",
                                   f"Delete production record:\n\nProduct: {product_name}\nKilos: {round(kilos,6)}\nDate: {produced_at}\nBatch: {batch_number or '(none)'}\n\nThis will also add the ingredients back to stock."):
            conn.close()
            return

        # Load product formula using product_id
        c.execute("SELECT ingredient_name, percentage FROM product_ingredients WHERE product_id=?", (product_id,))
        items = c.fetchall()

        # Add ingredients back to stock
        for ing, perc in items:
            required_kg = kilos * (perc / 100.0)
            c.execute("UPDATE ingredients SET qty_kg = qty_kg + ? WHERE name=?", (required_kg, ing))

        # Delete production by unique id
        c.execute("DELETE FROM productions WHERE id=?", (prod_id,))

        conn.commit()
        conn.close()

        messagebox.showinfo("Deleted", "Production deleted and stock quantities restored.")
        self.refresh()
        try:
            app.stock_frame.refresh()
        except Exception:
            pass

# --- Startup ---
if __name__ == "__main__":
    init_db()
    app = App()
    app.mainloop()

# Raw Material Management

A single‑file desktop application built using Python (Tkinter + SQLite) for managing ingredients, formulas (products), and production records.

This is the earliest version of the system — designed as an offline, lightweight tool for small‑scale manufacturing, pilot batches, and in‑house formulation tracking

## What This Software Is

- A local desktop app, not a web app
- Runs entirely offline
- Uses a single SQLite database stored alongside the script
- Focused on accuracy, traceability, and reversibility, not ERP complexity

## Core Modules
### Ingredient / Stock Manager

#### Manage raw materials with:
- Ingredient name 
- Quantity in kilograms
- Supplier name

#### Key behaviors:

- Alphabetical, case‑insensitive sorting
- Alternating row colors for readability
- Manual restocking (+kg)
- Safe deletion with confirmation

### Product & Formula Management

Each Product represents a formula.
#### Features:
- Up to 30 ingredients per product
- Percentage‑based formulation
- Ingredient autocomplete from stock list

Formula edits do not retroactively change past production records

### Production Management

Production converts a formula into an executed batch.
#### Capabilities:
- Select product + batch size (kg)
- Auto‑calculate ingredient requirements
- Stock sufficiency check before confirmation
- Visual warning for insufficient ingredients

#### Confirmed production:
- Subtracts raw material stock
- Creates a permanent production record

#### Each production record stores:
- Product name
- Batch size (kg)
- Manual production date/time
- Optional batch number

#### Reversible Production Deletion
Each production record has a unique ID. Deleting a production, restores ingredient quantities accurately
Uses the original formula + batch size
This makes the system safe for correction of entry mistakes.

## How to Run

### Requirements:

- Python 3.9+
- No external libraries required

Run:

```bash
python raw_materials_manager.py
```
The database file raw_materials.db will be created automatically in the same folder.

## Contributions

This project reflects a practical, real‑world formulation workflow. Improvements and forks are welcome.

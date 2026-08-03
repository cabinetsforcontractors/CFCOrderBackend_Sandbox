"""
rta_database.py
RTA Cabinet Database - SKU lookup for weights, dimensions, and shipping rules
"""

import os
import json
from typing import Optional, Dict, List
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Fix Heroku-style postgres:// URLs
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if DATABASE_URL and "sslmode" not in DATABASE_URL:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "sslmode=require"


@contextmanager
def get_db():
    """Database connection context manager"""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
# SCHEMA
# =============================================================================

CREATE_RTA_PRODUCTS_TABLE = """
CREATE TABLE IF NOT EXISTS rta_products (
    id SERIAL PRIMARY KEY,
    product_sku VARCHAR(100) UNIQUE NOT NULL,
    pre_sku VARCHAR(50),
    post_sku VARCHAR(100),
    door_name VARCHAR(200),
    product_code VARCHAR(200),
    product_type VARCHAR(100),
    cabinet_type VARCHAR(50),
    width DECIMAL(10,2),
    height DECIMAL(10,2),
    depth DECIMAL(10,2),
    supplier VARCHAR(100),
    door_style VARCHAR(100),
    cogs DECIMAL(10,2),
    sales_price DECIMAL(10,2),
    weight DECIMAL(10,2),
    requires_long_pallet BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rta_sku ON rta_products(product_sku);
CREATE INDEX IF NOT EXISTS idx_rta_pre_sku ON rta_products(pre_sku);
CREATE INDEX IF NOT EXISTS idx_rta_supplier ON rta_products(supplier);
"""


def init_rta_table():
    """Create the RTA products table"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_RTA_PRODUCTS_TABLE)
    return {"status": "ok", "message": "rta_products table created"}


# =============================================================================
# LONG PALLET DETECTION LOGIC
# =============================================================================

def requires_long_pallet(product_code: str, height: float, width: float) -> bool:
    """
    Determine if an item requires a long (8ft) pallet for shipping.
    
    Long Pallet Required:
    - Oven, Pantry, Broom cabinets with height >= 84"
    - Panels/Skins with width >= 6.5" and height >= 84"
    - Any item with height >= 96" and width >= 6.5"
    
    No Long Pallet (boxed items):
    - Molding, Crown, Filler, Toe kick, Scribe, Furniture base
    - Items with height >= 96" but width < 6.5"
    """
    product_code_upper = (product_code or '').upper()
    height = height or 0
    width = width or 0
    
    # Boxed items - NEVER need long pallet
    boxed_keywords = ['MOLDING', 'FILLER', 'TOE', 'SCRIBE', 'FURNITURE BASE', 'CROWN']
    if any(kw in product_code_upper for kw in boxed_keywords):
        return False
    
    # Oven, Pantry, Broom cabinets - need long pallet if height >= 84"
    if ('OVEN' in product_code_upper or 'PANTRY' in product_code_upper or 'BROOM' in product_code_upper) and height >= 84:
        return True
    
    # Panels with width >= 6.5 and height >= 84 - need long pallet
    if ('PANEL' in product_code_upper or 'SKIN' in product_code_upper) and width >= 6.5 and height >= 84:
        return True
    
    # Any 96"+ item with width >= 6.5
    if height >= 96 and width >= 6.5:
        return True
    
    return False


# =============================================================================
# DATA LOADING
# =============================================================================

def load_rta_data_from_excel(excel_path: str) -> Dict:
    """
    Load RTA data from Excel file into PostgreSQL.
    Reads the Master sheet and calculates requires_long_pallet for each SKU.
    """
    try:
        import pandas as pd
    except ImportError:
        return {"status": "error", "error": "pandas not installed"}
    
    # Read Excel
    df = pd.read_excel(excel_path, sheet_name='Master')
    
    # Calculate long pallet flag
    df['requires_long_pallet'] = df.apply(
        lambda row: requires_long_pallet(
            row.get('Product_Code', ''),
            row.get('Height', 0),
            row.get('Width', 0)
        ),
        axis=1
    )
    
    # Insert into database
    inserted = 0
    updated = 0
    errors = []
    
    with get_db() as conn:
        with conn.cursor() as cur:
            for _, row in df.iterrows():
                try:
                    cur.execute("""
                        INSERT INTO rta_products (
                            product_sku, pre_sku, post_sku, door_name, product_code,
                            product_type, cabinet_type, width, height, depth,
                            supplier, door_style, cogs, sales_price, weight,
                            requires_long_pallet, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
                        )
                        ON CONFLICT (product_sku) DO UPDATE SET
                            pre_sku = EXCLUDED.pre_sku,
                            post_sku = EXCLUDED.post_sku,
                            door_name = EXCLUDED.door_name,
                            product_code = EXCLUDED.product_code,
                            product_type = EXCLUDED.product_type,
                            cabinet_type = EXCLUDED.cabinet_type,
                            width = EXCLUDED.width,
                            height = EXCLUDED.height,
                            depth = EXCLUDED.depth,
                            supplier = EXCLUDED.supplier,
                            door_style = EXCLUDED.door_style,
                            cogs = EXCLUDED.cogs,
                            sales_price = EXCLUDED.sales_price,
                            weight = EXCLUDED.weight,
                            requires_long_pallet = EXCLUDED.requires_long_pallet,
                            updated_at = NOW()
                    """, (
                        row.get('product_sku'),
                        row.get('pre_sku'),
                        row.get('post_sku'),
                        row.get('Door_Name'),
                        row.get('Product_Code'),
                        row.get('Product_Type'),
                        row.get('Cabinet_Type'),
                        row.get('Width') if pd.notna(row.get('Width')) else None,
                        row.get('Height') if pd.notna(row.get('Height')) else None,
                        row.get('Depth') if pd.notna(row.get('Depth')) else None,
                        row.get('Supplier'),
                        row.get('Door_Style'),
                        row.get('COGS') if pd.notna(row.get('COGS')) else None,
                        row.get('Sales_Price') if pd.notna(row.get('Sales_Price')) else None,
                        row.get('Weight') if pd.notna(row.get('Weight')) else None,
                        row.get('requires_long_pallet', False)
                    ))
                    inserted += 1
                except Exception as e:
                    errors.append(f"{row.get('product_sku')}: {str(e)}")
    
    return {
        "status": "ok",
        "inserted": inserted,
        "errors": errors[:10] if errors else []
    }


# =============================================================================
# LOOKUP FUNCTIONS
# =============================================================================

def get_sku_info(sku: str) -> Optional[Dict]:
    """
    Look up a single SKU and return its info including weight and long pallet flag.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # supplier_sku added 2026-08-03 (the Sevachko quick-order build:
            # the single-sku door is the clean token readback)
            cur.execute("""
                SELECT product_sku, product_code, weight, height, width,
                       requires_long_pallet, supplier, cabinet_type,
                       supplier_sku
                FROM rta_products
                WHERE product_sku = %s
            """, (sku,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_skus_info(skus: List[str]) -> Dict[str, Dict]:
    """
    Look up multiple SKUs and return their info.
    Returns a dict keyed by SKU.
    """
    if not skus:
        return {}
    
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT product_sku, product_code, weight, height, width,
                       requires_long_pallet, supplier, cabinet_type
                FROM rta_products
                WHERE product_sku = ANY(%s)
            """, (skus,))
            rows = cur.fetchall()
            return {row['product_sku']: dict(row) for row in rows}


def estimate_weight_from_dimensions(product_code: str, height: float, width: float, depth: float) -> float:
    """
    Estimate weight based on product type and dimensions when no weight data available.
    
    Rules:
    - Trim (fillers, scribe, molding, toe kick): 0.5 lbs per linear foot (height/12)
    - Ref/Skin Panels: 1.5 lbs per square foot (height × width / 144)
    - Cabinets: ~10 lbs per cubic foot
    """
    product_code_upper = (product_code or '').upper()
    height = height or 0
    width = width or 0
    depth = depth or 24  # Default 24" depth for cabinets
    
    # Trim items: fillers, scribe, molding, toe kick, crown
    trim_keywords = ['FILLER', 'SCRIBE', 'MOLDING', 'CROWN', 'TOE', 'TRIM']
    if any(kw in product_code_upper for kw in trim_keywords):
        # 0.5 lbs per linear foot (use height as length)
        linear_feet = height / 12 if height > 0 else 8  # Default 8 ft if no height
        weight = linear_feet * 0.5
        return max(weight, 1)  # Minimum 1 lb
    
    # Panels: ref panels, skin panels, base panels
    panel_keywords = ['PANEL', 'SKIN', 'REF']
    if any(kw in product_code_upper for kw in panel_keywords):
        # 1.5 lbs per square foot
        if height > 0 and width > 0:
            square_feet = (height * width) / 144  # Convert sq inches to sq feet
            weight = square_feet * 1.5
            return max(weight, 2)  # Minimum 2 lbs
        else:
            # Default 8ft × 2ft panel = 24 lbs
            return 24
    
    # Cabinets: ~10 lbs per cubic foot
    if width > 0 and height > 0:
        cubic_inches = width * height * depth
        cubic_feet = cubic_inches / 1728  # 12^3 = 1728 cubic inches per cubic foot
        weight = cubic_feet * 10
        return max(weight, 5)  # Minimum 5 lbs
    
    # Ultimate fallback
    return 30


def calculate_order_weight_and_flags(line_items: List[Dict]) -> Dict:
    """
    Calculate total weight and check for long pallet items in an order.
    
    Weight Priority:
    1. RTA database (SKU-level weights) - most accurate
    2. Product-type specific estimate based on dimensions
    3. Fallback: 30 lbs per item
    
    Args:
        line_items: List of {'sku': 'XXX-B12', 'quantity': 2, ...}
    
    Returns:
        {
            'total_weight': 150.5,
            'has_long_pallet_item': True,
            'long_pallet_skus': ['XXX-O339624'],
            'missing_skus': ['UNKNOWN-SKU'],
            'items': [
                {'sku': 'XXX-B12', 'quantity': 2, 'weight': 45.0, 'line_weight': 90.0, 'requires_long_pallet': False},
                ...
            ]
        }
    """
    # Extract SKUs
    skus = [item.get('sku', '') for item in line_items if item.get('sku')]
    
    # Look up all SKUs
    sku_info = get_skus_info(skus)
    
    total_weight = 0
    has_long_pallet = False
    long_pallet_skus = []
    missing_skus = []
    items_with_info = []
    
    for item in line_items:
        sku = item.get('sku', '')
        qty = item.get('quantity', 1)
        
        info = sku_info.get(sku)
        
        if info:
            weight = info.get('weight') or 0
            
            # If weight is 0 but we have dimensions, estimate based on product type
            if weight == 0:
                weight = estimate_weight_from_dimensions(
                    info.get('product_code', ''),
                    info.get('height', 0),
                    info.get('width', 0),
                    info.get('depth', 24)
                )
            
            line_weight = weight * qty
            total_weight += line_weight
            
            if info.get('requires_long_pallet'):
                has_long_pallet = True
                long_pallet_skus.append(sku)
            
            items_with_info.append({
                'sku': sku,
                'quantity': qty,
                'weight': round(weight, 2),
                'line_weight': round(line_weight, 2),
                'requires_long_pallet': info.get('requires_long_pallet', False),
                'product_code': info.get('product_code'),
                'height': info.get('height'),
                'width': info.get('width')
            })
        else:
            # SKU not found - estimate based on item name if available
            item_name = item.get('name', '')
            estimated_weight = estimate_weight_from_name(item_name, qty)
            total_weight += estimated_weight
            missing_skus.append(sku)
            
            items_with_info.append({
                'sku': sku,
                'quantity': qty,
                'weight': round(estimated_weight / qty, 2),
                'line_weight': round(estimated_weight, 2),
                'requires_long_pallet': False,
                'estimated': True
            })
    
    return {
        'total_weight': round(total_weight, 2),
        'has_long_pallet_item': has_long_pallet,
        'long_pallet_skus': long_pallet_skus,
        'missing_skus': missing_skus,
        'items': items_with_info
    }


def estimate_weight_from_name(item_name: str, qty: int) -> float:
    """
    Estimate weight when SKU not found, based on item name.
    Used as last resort fallback.
    """
    name_upper = (item_name or '').upper()
    
    # Trim items: 0.5 lbs per linear foot, assume 8ft default
    trim_keywords = ['FILLER', 'SCRIBE', 'MOLDING', 'CROWN', 'TOE', 'TRIM']
    if any(kw in name_upper for kw in trim_keywords):
        # Try to extract height from name (e.g., "42 Inch" or "96")
        import re
        height_match = re.search(r'(\d+)\s*(INCH|IN|"|\s*$)', name_upper)
        if height_match:
            height_inches = int(height_match.group(1))
            linear_feet = height_inches / 12
        else:
            linear_feet = 8  # Default 8 ft
        weight_per_item = linear_feet * 0.5
        return max(weight_per_item * qty, 1)
    
    # Panels: 1.5 lbs per sq ft, default 8x2 = 24 lbs
    panel_keywords = ['PANEL', 'SKIN', 'REF']
    if any(kw in name_upper for kw in panel_keywords):
        return 24 * qty
    
    # Default cabinet weight
    return 30 * qty


def get_rta_stats() -> Dict:
    """Get statistics about the RTA database"""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    COUNT(*) as total_skus,
                    COUNT(DISTINCT supplier) as suppliers,
                    COUNT(DISTINCT cabinet_type) as cabinet_types,
                    SUM(CASE WHEN requires_long_pallet THEN 1 ELSE 0 END) as long_pallet_items,
                    AVG(weight) as avg_weight,
                    MAX(updated_at) as last_updated
                FROM rta_products
            """)
            row = cur.fetchone()
            return dict(row) if row else {}

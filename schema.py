"""
schema.py
Database schema SQL for CFC Order Backend.
Contains the full schema for initializing/resetting the database.
"""

SCHEMA_SQL = """
-- Drop view first (depends on orders)
DROP VIEW IF EXISTS order_status CASCADE;

-- Drop tables
DROP TABLE IF EXISTS order_line_items CASCADE;
DROP TABLE IF EXISTS order_events CASCADE;
DROP TABLE IF EXISTS order_alerts CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS warehouse_mapping CASCADE;
DROP TABLE IF EXISTS trusted_customers CASCADE;
DROP TABLE IF EXISTS pending_checkouts CASCADE;

-- Pending checkouts for B2BWave orders awaiting payment
CREATE TABLE pending_checkouts (
    order_id VARCHAR(50) PRIMARY KEY,
    customer_email VARCHAR(255),
    checkout_token VARCHAR(100),
    payment_link TEXT,
    payment_amount DECIMAL(10, 2),
    payment_initiated_at TIMESTAMP WITH TIME ZONE,
    payment_completed_at TIMESTAMP WITH TIME ZONE,
    transaction_id VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE warehouse_mapping (
    sku_prefix VARCHAR(100) PRIMARY KEY,
    warehouse_name VARCHAR(100) NOT NULL,
    warehouse_code VARCHAR(20),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Default warehouse mappings (authority: VERIFIED SOT 6_30_26 + William rulings 2026-07-14;
-- includes sample-door prefixes, e.g. SHAKERTRUEWHITE-SAMPLE routes via SHAKERTRUEWHITE.
-- TWIN and PW lines intentionally absent: fallback lines, not live — William 2026-07-14)
INSERT INTO warehouse_mapping (sku_prefix, warehouse_name, warehouse_code) VALUES
-- LI
('GSP', 'LI', 'LI'),
('NBLK', 'LI', 'LI'),
('SHAKERBLACK', 'LI', 'LI'),
('SHAKERDOVEGREY', 'LI', 'LI'),
('SHAKERTRUEWHITE', 'LI', 'LI'),
('WSP', 'LI', 'LI'),
-- DL
('BNG', 'DL', 'DL'),
('EBK', 'DL', 'DL'),
('SAVANNAHWHITE', 'DL', 'DL'),
('SAVNG', 'DL', 'DL'),
('SHAKERBINARYGRAY', 'DL', 'DL'),
('SHAKERUNFINISHED', 'DL', 'DL'),
('SKINNYSHAKERBLACK', 'DL', 'DL'),
('UFS', 'DL', 'DL'),
-- ROC
('BC', 'ROC', 'ROC'),
('DCH', 'ROC', 'ROC'),
('DCT', 'ROC', 'ROC'),
('DCW', 'ROC', 'ROC'),
('EGD', 'ROC', 'ROC'),
('EJG', 'ROC', 'ROC'),
('ELDRIDGEASHWALNUT', 'ROC', 'ROC'),
('ELDRIDGEMIDNIGHTBLUE', 'ROC', 'ROC'),
('EMB', 'ROC', 'ROC'),
('LNS', 'ROC', 'ROC'),
('PG', 'ROC', 'ROC'),
('SAVANNAHCHOCOLATE', 'ROC', 'ROC'),
('SHAKERHAZELNUTINSET', 'ROC', 'ROC'),
('SHAKERPEBBLEGREY', 'ROC', 'ROC'),
('SHAKERTRUFFLEINSET', 'ROC', 'ROC'),
('SHAKERWHITEINSET', 'ROC', 'ROC'),
('SKINNYSHAKERJADEGREEN', 'ROC', 'ROC'),
-- GHI
('AKS', 'GHI', 'GHI'),
('APW', 'GHI', 'GHI'),
('GRSH', 'GHI', 'GHI'),
('NOR', 'GHI', 'GHI'),
('NORFOLKLINEN', 'GHI', 'GHI'),
('SANIBELSAND', 'GHI', 'GHI'),
('SANIBELSEAOATS', 'GHI', 'GHI'),
('SHAKERAPPALACHIANKNOTTY', 'GHI', 'GHI'),
('SHAKERAPPALACHIANWALNUT', 'GHI', 'GHI'),
('SHAKERGREIGE', 'GHI', 'GHI'),
('SNS', 'GHI', 'GHI'),
('SNW', 'GHI', 'GHI'),
-- L&C Cabinetry
('BG', 'L&C Cabinetry', 'LC'),
('EDD', 'L&C Cabinetry', 'LC'),
('ELDRIDGEDOVEGRAY', 'L&C Cabinetry', 'LC'),
('ELDRIDGEROYALBLUE', 'L&C Cabinetry', 'LC'),
('MGLS', 'L&C Cabinetry', 'LC'),
('RBLS', 'L&C Cabinetry', 'LC'),
('SHAKERMANATEEGREY', 'L&C Cabinetry', 'LC'),
('SHAKERSEAGREEN', 'L&C Cabinetry', 'LC'),
('SHAKERSTORMGREY', 'L&C Cabinetry', 'LC'),
('SHLS', 'L&C Cabinetry', 'LC'),
-- Love-Milestone
('BUILDERSHAKERESPRESSO', 'Love-Milestone', 'LOVE'),
('BUILDERSHAKERGREY', 'Love-Milestone', 'LOVE'),
('BUILDERSHAKERWHITE', 'Love-Milestone', 'LOVE'),
('DG', 'Love-Milestone', 'LOVE'),
('EDG', 'Love-Milestone', 'LOVE'),
('ELDRIDGEDRIFTWOODGREY', 'Love-Milestone', 'LOVE'),
('ELDRIDGEWHITE', 'Love-Milestone', 'LOVE'),
('EWD', 'Love-Milestone', 'LOVE'),
('EWT', 'Love-Milestone', 'LOVE'),
('FE', 'Love-Milestone', 'LOVE'),
('FG', 'Love-Milestone', 'LOVE'),
('FW', 'Love-Milestone', 'LOVE'),
('HSS', 'Love-Milestone', 'LOVE'),
('LGS', 'Love-Milestone', 'LOVE'),
('LGSS', 'Love-Milestone', 'LOVE'),
('NBL', 'Love-Milestone', 'LOVE'),
('NJGR', 'Love-Milestone', 'LOVE'),
('RICHMONDCHARCOALGRAY', 'Love-Milestone', 'LOVE'),
('RICHMONDWHITE', 'Love-Milestone', 'LOVE'),
('RMW', 'Love-Milestone', 'LOVE'),
('RND', 'Love-Milestone', 'LOVE'),
('SHAKERDRIFTWOODGREY', 'Love-Milestone', 'LOVE'),
('SHAKERHONEYSPICE', 'Love-Milestone', 'LOVE'),
('SHAKERJADEGREEN', 'Love-Milestone', 'LOVE'),
('SHAKERLIBERTYGREEN', 'Love-Milestone', 'LOVE'),
('SHAKERMIDNIGHTNAVY', 'Love-Milestone', 'LOVE'),
('SKINNYSHAKERLIBERTYGREEN', 'Love-Milestone', 'LOVE'),
('SKINNYSHAKERWHITE', 'Love-Milestone', 'LOVE'),
('SKINNYSHAKERWHITEOAK', 'Love-Milestone', 'LOVE'),
('SWO', 'Love-Milestone', 'LOVE'),
-- Cabinet & Stone
('BSN', 'Cabinet & Stone', 'CS'),
('CAWN', 'Cabinet & Stone', 'CS'),
('CHARLESTONANTIQUEWHITE', 'Cabinet & Stone', 'CS'),
('CHARLESTONSADDLEGLAZE', 'Cabinet & Stone', 'CS'),
('DC', 'Cabinet & Stone', 'CS'),
('ESCS', 'Cabinet & Stone', 'CS'),
('EUBX', 'Cabinet & Stone', 'CS'),
('EUCS', 'Cabinet & Stone', 'CS'),
('EUPG', 'Cabinet & Stone', 'CS'),
('EURC', 'Cabinet & Stone', 'CS'),
('EUROCOSMOSAND', 'Cabinet & Stone', 'CS'),
('EUROPIANOGREY', 'Cabinet & Stone', 'CS'),
('EUROROMACLAY', 'Cabinet & Stone', 'CS'),
('FRBX', 'Cabinet & Stone', 'CS'),
('MSCS', 'Cabinet & Stone', 'CS'),
('SGCS', 'Cabinet & Stone', 'CS'),
('SHAKERESPRESSO', 'Cabinet & Stone', 'CS'),
('SHAKERMAPLE', 'Cabinet & Stone', 'CS'),
('SHAKERSTONEGREY', 'Cabinet & Stone', 'CS'),
('SHAKERTRUEBLUE', 'Cabinet & Stone', 'CS'),
('SHAKERWHITEOAK', 'Cabinet & Stone', 'CS'),
('WOCS', 'Cabinet & Stone', 'CS'),
-- DuraStone
('CMEN', 'DuraStone', 'DS'),
('NBDS', 'DuraStone', 'DS'),
('NSN', 'DuraStone', 'DS'),
('SHAKERIVORY', 'DuraStone', 'DS'),
('SHAKERNAVYBLUE', 'DuraStone', 'DS'),
('SHAKERTOFFEE', 'DuraStone', 'DS'),
('SIV', 'DuraStone', 'DS'),
-- Go Bravura
('HGG', 'Go Bravura', 'GB'),
('HGW', 'Go Bravura', 'GB'),
('MW', 'Go Bravura', 'GB'),
('NBG', 'Go Bravura', 'GB'),
('NCC', 'Go Bravura', 'GB'),
('NDG', 'Go Bravura', 'GB'),
('PJB', 'Go Bravura', 'GB'),
('UC', 'Go Bravura', 'GB'),
('UW', 'Go Bravura', 'GB'),
('WWA', 'Go Bravura', 'GB')
ON CONFLICT (sku_prefix) DO NOTHING;

-- Trusted customers (can ship before payment)
CREATE TABLE trusted_customers (
    id SERIAL PRIMARY KEY,
    customer_name VARCHAR(255) NOT NULL,
    company_name VARCHAR(255),
    email VARCHAR(255),
    phone VARCHAR(50),
    payment_grace_days INTEGER DEFAULT 1,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

INSERT INTO trusted_customers (customer_name, company_name, notes) VALUES
('Lou Palumbo', 'Louis And Clark Contracting', 'Long-time trusted customer'),
('Gerald Thomas', 'G & B Wood Creations', 'Trusted customer'),
('LD Stafford', 'Acute Custom Closets', 'Trusted customer'),
('James Marchant', NULL, 'Trusted customer')
ON CONFLICT DO NOTHING;

CREATE TABLE orders (
    order_id VARCHAR(50) PRIMARY KEY,
    
    -- Customer info
    customer_name VARCHAR(255),
    company_name VARCHAR(255),
    email VARCHAR(255),
    phone VARCHAR(50),
    
    -- Address
    street VARCHAR(255),
    street2 VARCHAR(255),
    city VARCHAR(100),
    state VARCHAR(50),
    zip_code VARCHAR(20),
    
    -- Order details
    order_date TIMESTAMP WITH TIME ZONE,
    order_total DECIMAL(10,2),
    total_weight DECIMAL(10,2),
    comments TEXT,
    
    -- Warehouses (extracted from SKU prefixes, up to 4)
    warehouse_1 VARCHAR(100),
    warehouse_2 VARCHAR(100),
    warehouse_3 VARCHAR(100),
    warehouse_4 VARCHAR(100),
    
    -- Payment
    payment_link_sent BOOLEAN DEFAULT FALSE,
    payment_link_sent_at TIMESTAMP WITH TIME ZONE,
    payment_received BOOLEAN DEFAULT FALSE,
    payment_received_at TIMESTAMP WITH TIME ZONE,
    payment_amount DECIMAL(10,2),
    shipping_cost DECIMAL(10,2),
    
    -- Shipping quotes
    rl_quote_no VARCHAR(50),
    shipping_quote_amount DECIMAL(10,2),
    
    -- Warehouse processing
    sent_to_warehouse BOOLEAN DEFAULT FALSE,
    sent_to_warehouse_at TIMESTAMP WITH TIME ZONE,
    warehouse_confirmed BOOLEAN DEFAULT FALSE,
    warehouse_confirmed_at TIMESTAMP WITH TIME ZONE,
    supplier_order_no VARCHAR(100),
    
    -- Shipping
    bol_sent BOOLEAN DEFAULT FALSE,
    bol_sent_at TIMESTAMP WITH TIME ZONE,
    tracking VARCHAR(255),
    pro_number VARCHAR(50),
    
    -- Flags
    is_trusted_customer BOOLEAN DEFAULT FALSE,
    needs_review BOOLEAN DEFAULT FALSE,
    review_reason TEXT,
    
    -- Completion
    is_complete BOOLEAN DEFAULT FALSE,
    completed_at TIMESTAMP WITH TIME ZONE,
    
    -- Meta
    email_thread_id VARCHAR(255),
    notes TEXT,
    ai_summary TEXT,
    ai_summary_updated_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Alerts/flags table (after orders so foreign key works)
CREATE TABLE order_alerts (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(50) REFERENCES orders(order_id) ON DELETE CASCADE,
    alert_type VARCHAR(50) NOT NULL,
    alert_message TEXT,
    is_resolved BOOLEAN DEFAULT FALSE,
    resolved_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_alerts_order ON order_alerts(order_id);
CREATE INDEX idx_alerts_unresolved ON order_alerts(is_resolved) WHERE NOT is_resolved;

CREATE TABLE order_line_items (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(50) REFERENCES orders(order_id) ON DELETE CASCADE,
    sku VARCHAR(100),
    sku_prefix VARCHAR(100),
    product_name TEXT,
    price DECIMAL(10,2),
    quantity INTEGER,
    line_total DECIMAL(10,2),
    warehouse VARCHAR(100)
);

CREATE TABLE order_events (
    event_id SERIAL PRIMARY KEY,
    order_id VARCHAR(50) REFERENCES orders(order_id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    event_data JSONB,
    source VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Email snippets for AI summary
CREATE TABLE order_email_snippets (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(50) REFERENCES orders(order_id) ON DELETE CASCADE,
    email_from VARCHAR(255),
    email_to VARCHAR(255),
    email_subject VARCHAR(500),
    email_snippet TEXT,
    email_date TIMESTAMP WITH TIME ZONE,
    snippet_type VARCHAR(50),  -- 'customer', 'supplier', 'internal', 'payment'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Shipments table - each warehouse in an order is a separate shipment
CREATE TABLE order_shipments (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(50) REFERENCES orders(order_id) ON DELETE CASCADE,
    shipment_id VARCHAR(50) NOT NULL UNIQUE,  -- e.g., "5307-Li"
    warehouse VARCHAR(100) NOT NULL,
    status VARCHAR(50) DEFAULT 'needs_order',  -- needs_order, at_warehouse, needs_bol, ready_ship, shipped, delivered
    tracking VARCHAR(100),
    pro_number VARCHAR(50),
    bol_sent BOOLEAN DEFAULT FALSE,
    bol_sent_at TIMESTAMP WITH TIME ZONE,
    weight DECIMAL(10,2),
    ship_method VARCHAR(50),  -- LTL, Pirateship, Pickup, BoxTruck, LiDelivery
    sent_to_warehouse_at TIMESTAMP WITH TIME ZONE,
    warehouse_confirmed_at TIMESTAMP WITH TIME ZONE,
    shipped_at TIMESTAMP WITH TIME ZONE,
    delivered_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_orders_complete ON orders(is_complete);
CREATE INDEX idx_orders_date ON orders(order_date DESC);
CREATE INDEX idx_line_items_order ON order_line_items(order_id);
CREATE INDEX idx_events_order ON order_events(order_id);
CREATE INDEX idx_email_snippets_order ON order_email_snippets(order_id);
CREATE INDEX idx_shipments_order ON order_shipments(order_id);
CREATE INDEX idx_shipments_id ON order_shipments(shipment_id);

-- View for current status
CREATE OR REPLACE VIEW order_status AS
SELECT 
    order_id,
    CASE
        WHEN is_complete THEN 'complete'
        WHEN bol_sent AND NOT is_complete THEN 'awaiting_shipment'
        WHEN warehouse_confirmed AND NOT bol_sent THEN 'needs_bol'
        WHEN sent_to_warehouse AND NOT warehouse_confirmed THEN 'awaiting_warehouse'
        WHEN payment_received AND NOT sent_to_warehouse THEN 'needs_warehouse_order'
        WHEN payment_link_sent AND NOT payment_received THEN 'awaiting_payment'
        ELSE 'needs_payment_link'
    END as current_status,
    EXTRACT(DAY FROM NOW() - order_date)::INTEGER as days_open
FROM orders;
"""

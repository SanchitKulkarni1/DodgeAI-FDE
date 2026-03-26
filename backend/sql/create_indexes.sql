-- Database Indexes for O2C Query Optimization
-- 
-- These indexes are critical for performance on frequently-queried columns.
-- Execute this script once on the production database.
--
-- Expected impact: 50% reduction in database query time (40s → 20s)

-- ============================================================================
-- BILLING DOCUMENTS - Most frequently queried table
-- ============================================================================

-- Index on is_cancelled check (appears in almost every query)
CREATE INDEX IF NOT EXISTS idx_billing_doc_is_cancelled 
  ON billing_document_headers(billing_doc_is_cancelled);

-- Index on accounting_document (used for joins)
CREATE INDEX IF NOT EXISTS idx_billing_doc_accounting 
  ON billing_document_headers(accounting_document);

-- Index on sold_to_party (customer filtering)
CREATE INDEX IF NOT EXISTS idx_billing_doc_sold_to 
  ON billing_document_headers(sold_to_party);

-- ============================================================================
-- BILLING DOCUMENT ITEMS 
-- ============================================================================

-- Index on billing_document (join key)
CREATE INDEX IF NOT EXISTS idx_billing_item_doc_id 
  ON billing_document_items(billing_document);

-- Index on material (product join)
CREATE INDEX IF NOT EXISTS idx_billing_item_material 
  ON billing_document_items(material);

-- ============================================================================
-- PRODUCTS - Filter by group/type
-- ============================================================================

-- Index on product_group (product category filtering)
CREATE INDEX IF NOT EXISTS idx_product_group 
  ON products(product_group);

-- Index on product_type
CREATE INDEX IF NOT EXISTS idx_product_type 
  ON products(product_type);

-- Index on product primary key (already indexed, but ensure it exists)
CREATE INDEX IF NOT EXISTS idx_product_id 
  ON products(product);

-- ============================================================================
-- SALES ORDERS
-- ============================================================================

-- Index on sold_to_party (customer filtering)
CREATE INDEX IF NOT EXISTS idx_sales_order_customer 
  ON sales_order_headers(sold_to_party);

-- Index on sales_order (primary key join)
CREATE INDEX IF NOT EXISTS idx_sales_order_id 
  ON sales_order_headers(sales_order);

-- ============================================================================
-- PAYMENTS - Accounts Receivable
-- ============================================================================

-- Index on customer
CREATE INDEX IF NOT EXISTS idx_payment_customer 
  ON payments_ar(customer);

-- Index on accounting_document (join with invoices)
CREATE INDEX IF NOT EXISTS idx_payment_accounting 
  ON payments_ar(clearing_accounting_document);

-- ============================================================================
-- OUTBOUND DELIVERIES
-- ============================================================================

-- Index on delivery_document
CREATE INDEX IF NOT EXISTS idx_delivery_doc_id 
  ON outbound_delivery_headers(delivery_document);

-- Index on delivery_items join
CREATE INDEX IF NOT EXISTS idx_delivery_items_doc_id 
  ON outbound_delivery_items(delivery_document);

-- ============================================================================
-- BUSINESS PARTNERS
-- ============================================================================

-- Index on customer ID (lookup)
CREATE INDEX IF NOT EXISTS idx_business_partner_customer 
  ON business_partners(customer);

-- ============================================================================
-- COMPOSITE INDEXES (for common filter combinations)
-- ============================================================================

-- Composite: (is_cancelled, sold_to_party) for customer billing queries
CREATE INDEX IF NOT EXISTS idx_billing_cancelled_customer 
  ON billing_document_headers(billing_doc_is_cancelled, sold_to_party);

-- Composite: (product_group, division) for product filtering
CREATE INDEX IF NOT EXISTS idx_product_group_division 
  ON products(product_group, division);

-- ============================================================================
-- ANALYZE updated tables to refresh statistics
-- ============================================================================

ANALYZE billing_document_headers;
ANALYZE billing_document_items;
ANALYZE products;
ANALYZE sales_order_headers;
ANALYZE payments_ar;
ANALYZE outbound_delivery_headers;
ANALYZE outbound_delivery_items;
ANALYZE business_partners;

-- ============================================================================
-- Verification script
-- ============================================================================

-- Run this to verify indexes were created:
-- SELECT indexname FROM pg_indexes WHERE tablename='billing_document_headers' ORDER BY indexname;

-- To check index usage statistics:
-- SELECT schemaname, tablename, indexname, idx_scan 
-- FROM pg_stat_user_indexes 
-- ORDER BY idx_scan DESC;

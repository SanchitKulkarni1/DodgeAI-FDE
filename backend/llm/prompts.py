"""
llm/prompts.py — Single source of truth for the DB schema context.

Imported by planner.py and sql_generator.py so the schema description
is never duplicated or out of sync.
"""

# ---------------------------------------------------------------------------
# Complete schema description with all join paths.
# Written specifically to guide SQL generation — not auto-generated DDL.
# Every non-obvious join is spelled out explicitly because the LLM must
# know them without guessing.
# ---------------------------------------------------------------------------

DB_SCHEMA = """
DATABASE: SQLite  (o2c.db)
ALL amounts are REAL. ALL dates are TEXT in 'YYYY-MM-DD' format.
Booleans are stored as INTEGER: 1 = true, 0 = false.

========== CORE O2C FLOW ==========

TABLE: sales_order_headers
  sales_order            TEXT  PRIMARY KEY  (e.g. '740506')
  sales_order_type       TEXT  (e.g. 'OR')
  sales_organization     TEXT
  distribution_channel   TEXT
  sold_to_party          TEXT  → business_partners.customer
  creation_date          TEXT
  total_net_amount       REAL
  transaction_currency   TEXT  (always 'INR' in this dataset)
  overall_delivery_status TEXT ('C' = fully delivered, 'A' = not yet delivered)
  overall_billing_status  TEXT (often NULL — do not rely on it)
  requested_delivery_date TEXT
  customer_payment_terms  TEXT
  header_billing_block    TEXT (NULL = not blocked)
  delivery_block_reason   TEXT (NULL = not blocked)

TABLE: sales_order_items
  sales_order       TEXT  → sales_order_headers.sales_order
  sales_order_item  TEXT  (item number, e.g. '10', '20')
  material          TEXT  → products.product
  requested_quantity REAL
  net_amount        REAL
  material_group    TEXT
  production_plant  TEXT  → plants.plant
  storage_location  TEXT
  rejection_reason  TEXT  (NULL = not rejected)
  item_billing_block TEXT (NULL = not blocked)
  PRIMARY KEY (sales_order, sales_order_item)

TABLE: sales_order_schedule_lines
  sales_order         TEXT  → sales_order_headers.sales_order
  sales_order_item    TEXT
  schedule_line       TEXT
  confirmed_delivery_date TEXT
  confirmed_quantity  REAL
  PRIMARY KEY (sales_order, sales_order_item, schedule_line)

TABLE: outbound_delivery_headers
  delivery_document              TEXT  PRIMARY KEY  (e.g. '80737721')
  shipping_point                 TEXT
  creation_date                  TEXT
  actual_goods_movement_date     TEXT  (NULL if goods not moved yet)
  overall_goods_movement_status  TEXT  ('A' = not posted, 'C' = posted)
  overall_picking_status         TEXT  ('C' = fully picked)
  header_billing_block           TEXT  (NULL = not blocked)

TABLE: outbound_delivery_items
  delivery_document      TEXT  → outbound_delivery_headers.delivery_document
  delivery_document_item TEXT
  reference_sd_document  TEXT  → sales_order_headers.sales_order  *** KEY JOIN ***
  reference_sd_doc_item  TEXT
  actual_delivery_quantity REAL
  plant                  TEXT  → plants.plant
  storage_location       TEXT
  item_billing_block     TEXT  (NULL = not blocked)
  PRIMARY KEY (delivery_document, delivery_document_item)

TABLE: billing_document_headers
  billing_document         TEXT  PRIMARY KEY  (e.g. '90504248')
  billing_document_type    TEXT  ('F2' = standard invoice, 'S1' = cancellation)
  billing_document_date    TEXT
  billing_doc_is_cancelled INTEGER  (1 = cancelled, 0 = active)
  cancelled_billing_document TEXT  (NULL or empty string if not a cancellation)
  total_net_amount         REAL
  transaction_currency     TEXT
  company_code             TEXT
  fiscal_year              TEXT
  accounting_document      TEXT  → journal_entry_items_ar.accounting_document  *** KEY JOIN ***
                                 → payments_ar.clearing_accounting_document     *** KEY JOIN ***
  sold_to_party            TEXT  → business_partners.customer

TABLE: billing_document_items
  billing_document      TEXT  → billing_document_headers.billing_document
  billing_document_item TEXT
  material              TEXT  → products.product
  billing_quantity      REAL
  net_amount            REAL
  reference_sd_document TEXT  → outbound_delivery_headers.delivery_document  *** KEY JOIN ***
  PRIMARY KEY (billing_document, billing_document_item)

TABLE: billing_document_cancellations
  (same columns as billing_document_headers — contains ONLY cancelled docs)
  billing_doc_is_cancelled = 1 always

TABLE: journal_entry_items_ar
  company_code               TEXT
  fiscal_year                TEXT
  accounting_document        TEXT  → billing_document_headers.accounting_document  *** KEY JOIN ***
  accounting_document_item   TEXT
  reference_document         TEXT  → billing_document_headers.billing_document  (secondary join)
  customer                   TEXT  → business_partners.customer
  amount_in_transaction_currency REAL
  amount_in_company_code_currency REAL
  posting_date               TEXT
  clearing_date              TEXT  (NULL if not yet cleared/paid)
  clearing_accounting_document TEXT
  PRIMARY KEY (company_code, fiscal_year, accounting_document, accounting_document_item)

TABLE: payments_ar
  company_code               TEXT
  fiscal_year                TEXT
  accounting_document        TEXT
  accounting_document_item   TEXT
  customer                   TEXT  → business_partners.customer
  clearing_date              TEXT
  clearing_accounting_document TEXT  → billing_document_headers.accounting_document  *** PAYMENT LINK ***
  amount_in_transaction_currency REAL
  amount_in_company_code_currency REAL
  posting_date               TEXT
  invoice_reference          TEXT  (NULL in this dataset — do NOT use as join key)
  sales_document             TEXT  (NULL in this dataset — do NOT use as join key)
  PRIMARY KEY (company_code, fiscal_year, accounting_document, accounting_document_item)

========== SUPPORTING ENTITIES ==========

TABLE: business_partners
  business_partner       TEXT  PRIMARY KEY  (same value as customer)
  customer               TEXT  (same as business_partner — use this for joins)
  business_partner_full_name TEXT  (e.g. 'Nelson, Fitzpatrick and Jordan')
  is_blocked             INTEGER

TABLE: business_partner_addresses
  business_partner  TEXT  → business_partners.business_partner
  address_id        TEXT
  city_name         TEXT  (sometimes NULL)
  country           TEXT  (always 'IN' = India in this dataset)
  region            TEXT  (2-letter Indian state code: MH, WB, KA, TN, RJ, TS, OD)
  postal_code       TEXT  (sometimes NULL)
  street_name       TEXT  (sometimes NULL)
  PRIMARY KEY (business_partner, address_id)

TABLE: products
  product             TEXT  PRIMARY KEY  (e.g. 'S8907367001003')
  product_type        TEXT
  product_old_id      TEXT  (human-readable SKU code)
  base_unit           TEXT  (always 'PC' = pieces)
  product_group       TEXT
  gross_weight        REAL
  net_weight          REAL

TABLE: product_descriptions
  product              TEXT  → products.product
  language             TEXT  (use language = 'EN' to get English names)
  product_description  TEXT  (e.g. 'BEARDOIL 30ML ALMOND+THYME')
  PRIMARY KEY (product, language)

TABLE: plants
  plant          TEXT  PRIMARY KEY  (e.g. '1920', 'WB05')
  plant_name     TEXT
  sales_organization TEXT
  distribution_channel TEXT

TABLE: customer_company_assignments
  customer      TEXT  → business_partners.customer
  company_code  TEXT
  payment_terms TEXT
  PRIMARY KEY (customer, company_code)

TABLE: customer_sales_area_assignments
  customer             TEXT  → business_partners.customer
  sales_organization   TEXT
  distribution_channel TEXT
  division             TEXT
  currency             TEXT
  customer_payment_terms TEXT
  shipping_condition   TEXT
  PRIMARY KEY (customer, sales_organization, distribution_channel, division)

========== CRITICAL JOIN PATHS (memorise these) ==========

Sales Order → Delivery:
  outbound_delivery_items.reference_sd_document = sales_order_headers.sales_order

Delivery Item → Delivery Header:
  outbound_delivery_items.delivery_document = outbound_delivery_headers.delivery_document

Delivery → Billing:
  billing_document_items.reference_sd_document = outbound_delivery_headers.delivery_document

Billing Item → Billing Header:
  billing_document_items.billing_document = billing_document_headers.billing_document

Billing → Journal Entry:
  journal_entry_items_ar.accounting_document = billing_document_headers.accounting_document

Billing → Payment:
  payments_ar.clearing_accounting_document = billing_document_headers.accounting_document
  (NOT invoice_reference — that column is NULL for all rows)

Customer joins:
  sales_order_headers.sold_to_party = business_partners.customer
  billing_document_headers.sold_to_party = business_partners.customer
  payments_ar.customer = business_partners.customer

Product joins:
  sales_order_items.material = products.product
  billing_document_items.material = products.product
  products.product = product_descriptions.product (AND product_descriptions.language = 'EN')

========== BROKEN FLOW PATTERNS (useful for gap queries) ==========

Sales Orders never delivered:
  sales_order_headers WHERE overall_delivery_status = 'A'

Deliveries with no billing:
  outbound_delivery_headers odh
  WHERE NOT EXISTS (
    SELECT 1 FROM billing_document_items bdi
    WHERE bdi.reference_sd_document = odh.delivery_document
  )

Active billing docs with no payment:
  billing_document_headers bdh
  WHERE bdh.billing_doc_is_cancelled = 0
  AND NOT EXISTS (
    SELECT 1 FROM payments_ar p
    WHERE p.clearing_accounting_document = bdh.accounting_document
  )
"""
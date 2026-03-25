"""
llm/prompts.py — Single source of truth for the DB schema context.

FIX #5: Added DATA_CONSTRAINTS block describing the actual date coverage,
known data quality issues, and financial ranges. This is injected into the
planner, sql_generator, and answer_writer system prompts so the LLM never
generates SQL filters for 2024 / Q4 which return 0 rows.
"""

# ---------------------------------------------------------------------------
# FIX #5 — Dataset constraints block.
# Injected into every LLM system prompt that touches SQL or dates.
# ---------------------------------------------------------------------------

DATA_CONSTRAINTS = """
========== DATASET CONSTRAINTS — READ BEFORE GENERATING ANY SQL ==========

DATE COVERAGE (HARD LIMITS — no data exists outside these ranges):
  sales_order_headers.creation_date          : 2025-03-31 to 2025-04-02
  outbound_delivery_headers.creation_date    : 2025-03-31 to 2025-04-07
  outbound_delivery_headers.actual_goods_movement_date : 2025-04-02 to 2025-04-29
  billing_document_headers.billing_document_date : ONLY 3 DATES EXIST:
      2025-04-02 (90% of records), 2025-04-30, 2025-05-16
  payments_ar.posting_date                   : 2025-04-02 to 2025-05-16
  payments_ar.clearing_date                  : 2025-04-02 to 2025-05-16
  journal_entry_items_ar.posting_date        : 2025-04-02 to 2025-05-16

RESPONSE RULE FOR OUT-OF-RANGE DATE QUERIES:
  If the user asks about 2024, Q4, Q1 before April, or any period outside
  March–May 2025, DO NOT generate a date filter that returns 0 rows.
  Instead produce an answer that says:
    "This dataset covers March–May 2025 only. No records exist for [period].
     Shall I run this for the available date range (April–May 2025)?"

BUSINESS SCOPE:
  - Currency       : INR only (all amounts are in Indian Rupees)
  - Fiscal year    : 2025 only
  - Company code   : ABCD
  - Sales org      : ABCD
  - Customers      : exactly 8 business partners
  - Products       : 69 SKUs — fragrances, body sprays, deodorants (ZFG1001 group)
  - Plants         : 44 plants

KEY DATA QUALITY FACTS:
  - 80 of 163 billing documents are CANCELLED (billing_doc_is_cancelled = TRUE).
    ALWAYS filter: WHERE billing_doc_is_cancelled = FALSE for any revenue query.
  - 14 sales orders have no delivery (overall_delivery_status = 'A').
    These are the "broken flow" orders — valid for gap queries.
  - 3 deliveries have no billing document — valid incomplete-flow data.
  - overall_billing_status is NULL/empty for ALL orders — NEVER use this column
    to determine billing status. Use a JOIN to billing_document_headers instead.
  - payments_ar.invoice_reference is NULL for all rows — NOT a valid join key.
  - payments_ar.sales_document is NULL for all rows — NOT a valid join key.
  - Payments may include negative amounts (reversals/credits). Use ABS() or
    add a filter WHERE amount_in_transaction_currency > 0 when computing totals.

FINANCIAL RANGES (for sanity-checking SQL results):
  - sales_order totalNetAmount    : INR 119 to INR 19,021 per order
  - active billing doc amounts    : INR 152 to INR 2,034 per document
  - payment amounts               : -7,199 to +7,199 INR (negatives are reversals)
  - total active billing revenue  : ~INR 30,000 across all 83 active docs
"""

# ---------------------------------------------------------------------------
# Complete schema description with all join paths.
# ---------------------------------------------------------------------------

DB_SCHEMA = DATA_CONSTRAINTS + """
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
  overall_billing_status  TEXT (ALWAYS NULL — do not use this column)
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
  invoice_reference          TEXT  (NULL in this dataset — DO NOT use as join key)
  sales_document             TEXT  (NULL in this dataset — DO NOT use as join key)
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
  WHERE bdh.billing_doc_is_cancelled = FALSE
  AND NOT EXISTS (
    SELECT 1 FROM payments_ar p
    WHERE p.clearing_accounting_document = bdh.accounting_document
  )
"""
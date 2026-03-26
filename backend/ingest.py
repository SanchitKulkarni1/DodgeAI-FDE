"""
ingest.py — SAP Order-to-Cash Dataset → PostgreSQL

Usage:
    python ingest.py --data-dir ./sap-o2c-data --db-name dodgeai_o2c

What it does:
    1. Reads all JSONL part-files from each entity folder
    2. Creates a normalized PostgreSQL schema (19 tables)
    3. Flattens nested fields (e.g. creationTime dict → creationTime TEXT)
    4. Coerces types (amounts → NUMERIC, booleans → BOOLEAN, dates → DATE)
    5. Deduplicates on primary keys before insert
    6. Creates all indexes needed for graph traversal and LLM-generated SQL
    7. Prints a summary of row counts and join-path validation
    
Database credentials are read from environment variables:
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
"""

import argparse
import glob
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# PostgreSQL connection parameters from environment
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

# DATABASE_URL="postgresql://dodgeai_o2c_user:OQq3Ietbd4IOTs61uBuOHKpMctQY8YPJ@dpg-d7235cruibrs73cr23e0-a.singapore-postgres.render.com/dodgeai_o2c"
DATABASE_URL="postgresql://postgres:Auzpk%407036r@db.wkiclxcdufzzmcspgzul.supabase.co:5432/postgres"

# ---------------------------------------------------------------------------
# Schema definitions
# Each entry: (table_name, folder_name, primary_key_cols, column_definitions)
# Column definitions: list of (col_name, postgresql_type, jsonl_key, transform_fn)
# transform_fn is optional — None means use raw value as-is (cast to type).
# ---------------------------------------------------------------------------

def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    return bool(v) if v is not None else None


def _real(v: Any) -> float | None:
    if v in (None, "", "null"):
        return None
    try:
        if isinstance(v, (int, float, str)):
            return float(v)
        return None
    except (ValueError, TypeError):
        return None


def _text(v):
    if v is None:
        return None
    if isinstance(v, dict):          # e.g. creationTime: {hours, minutes, seconds}
        h = v.get("hours", 0)
        m = v.get("minutes", 0)
        s = v.get("seconds", 0)
        return f"{h:02d}:{m:02d}:{s:02d}"
    return str(v).strip() or None    # collapse empty strings to NULL


def _date(v):
    """Keep ISO date strings, strip time component when only date needed."""
    if not v or v == "0000-00-00":
        return None
    s = str(v)
    # '2025-04-02T00:00:00.000Z' → '2025-04-02'
    return s.split("T")[0]


def _datetime(v):
    if not v:
        return None
    return str(v)


# (col_name, postgresql_type, jsonl_key, transform_fn)
SCHEMAS: dict[str, Any] = {

    "sales_order_headers": {
        "folder": "sales_order_headers",
        "pk": ["sales_order"],
        "cols": [
            ("sales_order",                "VARCHAR(20)",    "salesOrder",                  _text),
            ("sales_order_type",           "VARCHAR(4)",     "salesOrderType",              _text),
            ("sales_organization",         "VARCHAR(4)",     "salesOrganization",           _text),
            ("distribution_channel",       "VARCHAR(2)",     "distributionChannel",         _text),
            ("organization_division",      "VARCHAR(2)",     "organizationDivision",        _text),
            ("sales_group",                "VARCHAR(3)",     "salesGroup",                  _text),
            ("sales_office",               "VARCHAR(4)",     "salesOffice",                 _text),
            ("sold_to_party",              "VARCHAR(20)",    "soldToParty",                 _text),
            ("creation_date",              "DATE",           "creationDate",                _date),
            ("created_by_user",            "VARCHAR(12)",    "createdByUser",               _text),
            ("last_change_datetime",       "VARCHAR(30)",    "lastChangeDateTime",          _datetime),
            ("total_net_amount",           "NUMERIC(15,2)",  "totalNetAmount",              _real),
            ("transaction_currency",       "VARCHAR(3)",     "transactionCurrency",         _text),
            ("overall_delivery_status",    "VARCHAR(10)",    "overallDeliveryStatus",       _text),
            ("overall_billing_status",     "VARCHAR(10)",    "overallOrdReltdBillgStatus",  _text),
            ("overall_sd_ref_status",      "VARCHAR(10)",    "overallSdDocReferenceStatus", _text),
            ("pricing_date",               "DATE",           "pricingDate",                 _date),
            ("requested_delivery_date",    "DATE",           "requestedDeliveryDate",       _date),
            ("header_billing_block",       "VARCHAR(2)",     "headerBillingBlockReason",    _text),
            ("delivery_block_reason",      "VARCHAR(2)",     "deliveryBlockReason",         _text),
            ("incoterms_classification",   "VARCHAR(3)",     "incotermsClassification",     _text),
            ("incoterms_location1",        "VARCHAR(27)",    "incotermsLocation1",          _text),
            ("customer_payment_terms",     "VARCHAR(4)",     "customerPaymentTerms",        _text),
            ("total_credit_check_status",  "VARCHAR(10)",    "totalCreditCheckStatus",      _text),
        ],
    },

    "sales_order_items": {
        "folder": "sales_order_items",
        "pk": ["sales_order", "sales_order_item"],
        "cols": [
            ("sales_order",                "VARCHAR(20)",    "salesOrder",                  _text),
            ("sales_order_item",           "VARCHAR(6)",     "salesOrderItem",              _text),
            ("sales_order_item_category",  "VARCHAR(10)",    "salesOrderItemCategory",      _text),
            ("material",                   "VARCHAR(40)",    "material",                    _text),
            ("requested_quantity",         "NUMERIC(13,3)",  "requestedQuantity",           _real),
            ("requested_quantity_unit",    "VARCHAR(3)",     "requestedQuantityUnit",       _text),
            ("net_amount",                 "NUMERIC(15,2)",  "netAmount",                   _real),
            ("transaction_currency",       "VARCHAR(3)",     "transactionCurrency",         _text),
            ("material_group",             "VARCHAR(9)",     "materialGroup",               _text),
            ("production_plant",           "VARCHAR(4)",     "productionPlant",             _text),
            ("storage_location",           "VARCHAR(4)",     "storageLocation",             _text),
            ("rejection_reason",           "VARCHAR(3)",     "salesDocumentRjcnReason",     _text),
            ("item_billing_block",         "VARCHAR(2)",     "itemBillingBlockReason",      _text),
        ],
    },

    "sales_order_schedule_lines": {
        "folder": "sales_order_schedule_lines",
        "pk": ["sales_order", "sales_order_item", "schedule_line"],
        "cols": [
            ("sales_order",                "VARCHAR(20)",    "salesOrder",                  _text),
            ("sales_order_item",           "VARCHAR(6)",     "salesOrderItem",              _text),
            ("schedule_line",              "VARCHAR(4)",     "scheduleLine",                _text),
            ("confirmed_delivery_date",    "DATE",           "confirmedDeliveryDate",       _date),
            ("order_quantity_unit",        "VARCHAR(3)",     "orderQuantityUnit",           _text),
            ("confirmed_quantity",         "NUMERIC(13,3)",  "confdOrderQtyByMatlAvailCheck", _real),
        ],
    },

    "outbound_delivery_headers": {
        "folder": "outbound_delivery_headers",
        "pk": ["delivery_document"],
        "cols": [
            ("delivery_document",              "VARCHAR(20)",     "deliveryDocument",            _text),
            ("shipping_point",                 "VARCHAR(4)",      "shippingPoint",               _text),
            ("creation_date",                  "DATE",            "creationDate",                _date),
            ("creation_time",                  "VARCHAR(8)",      "creationTime",                _text),
            ("last_change_date",               "DATE",            "lastChangeDate",              _date),
            ("actual_goods_movement_date",     "DATE",            "actualGoodsMovementDate",     _date),
            ("actual_goods_movement_time",     "VARCHAR(8)",      "actualGoodsMovementTime",     _text),
            ("delivery_block_reason",          "VARCHAR(2)",      "deliveryBlockReason",         _text),
            ("header_billing_block",           "VARCHAR(2)",      "headerBillingBlockReason",    _text),
            ("overall_goods_movement_status",  "VARCHAR(10)",     "overallGoodsMovementStatus",  _text),
            ("overall_picking_status",         "VARCHAR(10)",     "overallPickingStatus",        _text),
            ("overall_pod_status",             "VARCHAR(10)",     "overallProofOfDeliveryStatus", _text),
            ("hdr_general_incompletion",       "VARCHAR(10)",     "hdrGeneralIncompletionStatus", _text),
        ],
    },

    "outbound_delivery_items": {
        "folder": "outbound_delivery_items",
        "pk": ["delivery_document", "delivery_document_item"],
        "cols": [
            ("delivery_document",          "VARCHAR(20)",    "deliveryDocument",          _text),
            ("delivery_document_item",     "VARCHAR(6)",     "deliveryDocumentItem",      _text),
            ("reference_sd_document",      "VARCHAR(20)",    "referenceSdDocument",       _text),
            ("reference_sd_doc_item",      "VARCHAR(6)",     "referenceSdDocumentItem",   _text),
            ("actual_delivery_quantity",   "NUMERIC(13,3)",  "actualDeliveryQuantity",    _real),
            ("delivery_quantity_unit",     "VARCHAR(3)",     "deliveryQuantityUnit",      _text),
            ("plant",                      "VARCHAR(4)",     "plant",                     _text),
            ("storage_location",           "VARCHAR(4)",     "storageLocation",           _text),
            ("batch",                      "VARCHAR(10)",    "batch",                     _text),
            ("item_billing_block",         "VARCHAR(2)",     "itemBillingBlockReason",    _text),
            ("last_change_date",           "DATE",           "lastChangeDate",            _date),
        ],
    },

    "billing_document_headers": {
        "folder": "billing_document_headers",
        "pk": ["billing_document"],
        "cols": [
            ("billing_document",           "VARCHAR(20)",     "billingDocument",             _text),
            ("billing_document_type",      "VARCHAR(2)",      "billingDocumentType",         _text),
            ("billing_document_date",      "DATE",            "billingDocumentDate",         _date),
            ("creation_date",              "DATE",            "creationDate",                _date),
            ("creation_time",              "VARCHAR(8)",      "creationTime",                _text),
            ("last_change_datetime",       "VARCHAR(30)",     "lastChangeDateTime",          _datetime),
            ("billing_doc_is_cancelled",   "BOOLEAN",         "billingDocumentIsCancelled",  _bool),
            ("cancelled_billing_document", "VARCHAR(20)",     "cancelledBillingDocument",    _text),
            ("total_net_amount",           "NUMERIC(15,2)",   "totalNetAmount",              _real),
            ("transaction_currency",       "VARCHAR(3)",      "transactionCurrency",         _text),
            ("company_code",               "VARCHAR(4)",      "companyCode",                 _text),
            ("fiscal_year",                "VARCHAR(4)",      "fiscalYear",                  _text),
            ("accounting_document",        "VARCHAR(20)",     "accountingDocument",          _text),
            ("sold_to_party",              "VARCHAR(20)",     "soldToParty",                 _text),
        ],
    },

    "billing_document_items": {
        "folder": "billing_document_items",
        "pk": ["billing_document", "billing_document_item"],
        "cols": [
            ("billing_document",           "VARCHAR(20)",  "billingDocument",           _text),
            ("billing_document_item",      "VARCHAR(6)",   "billingDocumentItem",       _text),
            ("material",                   "VARCHAR(40)",  "material",                  _text),
            ("billing_quantity",           "NUMERIC(13,3)", "billingQuantity",           _real),
            ("billing_quantity_unit",      "VARCHAR(3)",   "billingQuantityUnit",       _text),
            ("net_amount",                 "NUMERIC(15,2)", "netAmount",                 _real),
            ("transaction_currency",       "VARCHAR(3)",   "transactionCurrency",       _text),
            ("reference_sd_document",      "VARCHAR(20)",  "referenceSdDocument",       _text),
            ("reference_sd_doc_item",      "VARCHAR(6)",   "referenceSdDocumentItem",   _text),
        ],
    },

    "billing_document_cancellations": {
        "folder": "billing_document_cancellations",
        "pk": ["billing_document"],
        "cols": [
            ("billing_document",           "VARCHAR(20)",     "billingDocument",             _text),
            ("billing_document_type",      "VARCHAR(2)",      "billingDocumentType",         _text),
            ("billing_document_date",      "DATE",            "billingDocumentDate",         _date),
            ("creation_date",              "DATE",            "creationDate",                _date),
            ("creation_time",              "VARCHAR(8)",      "creationTime",                _text),
            ("last_change_datetime",       "VARCHAR(30)",     "lastChangeDateTime",          _datetime),
            ("billing_doc_is_cancelled",   "BOOLEAN",         "billingDocumentIsCancelled",  _bool),
            ("cancelled_billing_document", "VARCHAR(20)",     "cancelledBillingDocument",    _text),
            ("total_net_amount",           "NUMERIC(15,2)",   "totalNetAmount",              _real),
            ("transaction_currency",       "VARCHAR(3)",      "transactionCurrency",         _text),
            ("company_code",               "VARCHAR(4)",      "companyCode",                 _text),
            ("fiscal_year",                "VARCHAR(4)",      "fiscalYear",                  _text),
            ("accounting_document",        "VARCHAR(20)",     "accountingDocument",          _text),
            ("sold_to_party",              "VARCHAR(20)",     "soldToParty",                 _text),
        ],
    },

    "journal_entry_items_ar": {
        "folder": "journal_entry_items_accounts_receivable",
        "pk": ["company_code", "fiscal_year", "accounting_document", "accounting_document_item"],
        "cols": [
            ("company_code",                    "VARCHAR(4)",      "companyCode",                  _text),
            ("fiscal_year",                     "VARCHAR(4)",      "fiscalYear",                   _text),
            ("accounting_document",             "VARCHAR(20)",     "accountingDocument",           _text),
            ("accounting_document_item",        "VARCHAR(6)",      "accountingDocumentItem",       _text),
            ("accounting_document_type",        "VARCHAR(2)",      "accountingDocumentType",       _text),
            ("reference_document",              "VARCHAR(20)",     "referenceDocument",            _text),
            ("gl_account",                      "VARCHAR(10)",     "glAccount",                    _text),
            ("customer",                        "VARCHAR(20)",     "customer",                     _text),
            ("cost_center",                     "VARCHAR(10)",     "costCenter",                   _text),
            ("profit_center",                   "VARCHAR(10)",     "profitCenter",                 _text),
            ("transaction_currency",            "VARCHAR(3)",      "transactionCurrency",          _text),
            ("amount_in_transaction_currency",  "NUMERIC(15,2)",   "amountInTransactionCurrency",  _real),
            ("company_code_currency",           "VARCHAR(3)",      "companyCodeCurrency",          _text),
            ("amount_in_company_code_currency", "NUMERIC(15,2)",   "amountInCompanyCodeCurrency",  _real),
            ("posting_date",                    "DATE",            "postingDate",                  _date),
            ("document_date",                   "DATE",            "documentDate",                 _date),
            ("financial_account_type",          "VARCHAR(10)",     "financialAccountType",         _text),
            ("clearing_date",                   "DATE",            "clearingDate",                 _date),
            ("clearing_accounting_document",    "VARCHAR(20)",     "clearingAccountingDocument",   _text),
            ("clearing_doc_fiscal_year",        "VARCHAR(4)",      "clearingDocFiscalYear",        _text),
            ("assignment_reference",            "VARCHAR(18)",     "assignmentReference",          _text),
            ("last_change_datetime",            "VARCHAR(30)",     "lastChangeDateTime",           _datetime),
        ],
    },

    "payments_ar": {
        "folder": "payments_accounts_receivable",
        "pk": ["company_code", "fiscal_year", "accounting_document", "accounting_document_item"],
        "cols": [
            ("company_code",                    "VARCHAR(4)",      "companyCode",                  _text),
            ("fiscal_year",                     "VARCHAR(4)",      "fiscalYear",                   _text),
            ("accounting_document",             "VARCHAR(20)",     "accountingDocument",           _text),
            ("accounting_document_item",        "VARCHAR(6)",      "accountingDocumentItem",       _text),
            ("customer",                        "VARCHAR(20)",     "customer",                     _text),
            ("clearing_date",                   "DATE",            "clearingDate",                 _date),
            ("clearing_accounting_document",    "VARCHAR(20)",     "clearingAccountingDocument",   _text),
            ("clearing_doc_fiscal_year",        "VARCHAR(4)",      "clearingDocFiscalYear",        _text),
            ("amount_in_transaction_currency",  "NUMERIC(15,2)",   "amountInTransactionCurrency",  _real),
            ("transaction_currency",            "VARCHAR(3)",      "transactionCurrency",          _text),
            ("amount_in_company_code_currency", "NUMERIC(15,2)",   "amountInCompanyCodeCurrency",  _real),
            ("company_code_currency",           "VARCHAR(3)",      "companyCodeCurrency",          _text),
            ("invoice_reference",               "VARCHAR(20)",     "invoiceReference",             _text),
            ("invoice_reference_fiscal_year",   "VARCHAR(4)",      "invoiceReferenceFiscalYear",   _text),
            ("sales_document",                  "VARCHAR(20)",     "salesDocument",                _text),
            ("sales_document_item",             "VARCHAR(6)",      "salesDocumentItem",            _text),
            ("posting_date",                    "DATE",            "postingDate",                  _date),
            ("document_date",                   "DATE",            "documentDate",                 _date),
            ("assignment_reference",            "VARCHAR(18)",     "assignmentReference",          _text),
            ("gl_account",                      "VARCHAR(10)",     "glAccount",                    _text),
            ("financial_account_type",          "VARCHAR(10)",     "financialAccountType",         _text),
            ("profit_center",                   "VARCHAR(10)",     "profitCenter",                 _text),
            ("cost_center",                     "VARCHAR(10)",     "costCenter",                   _text),
        ],
    },

    "business_partners": {
        "folder": "business_partners",
        "pk": ["business_partner"],
        "cols": [
            ("business_partner",           "VARCHAR(20)",     "businessPartner",              _text),
            ("customer",                   "VARCHAR(20)",     "customer",                     _text),
            ("business_partner_category",  "VARCHAR(10)",     "businessPartnerCategory",      _text),
            ("business_partner_full_name", "VARCHAR(80)",     "businessPartnerFullName",      _text),
            ("business_partner_name",      "VARCHAR(40)",     "businessPartnerName",          _text),
            ("organization_bp_name1",      "VARCHAR(40)",     "organizationBpName1",          _text),
            ("organization_bp_name2",      "VARCHAR(40)",     "organizationBpName2",          _text),
            ("first_name",                 "VARCHAR(40)",     "firstName",                    _text),
            ("last_name",                  "VARCHAR(40)",     "lastName",                     _text),
            ("industry",                   "VARCHAR(4)",      "industry",                     _text),
            ("business_partner_grouping",  "VARCHAR(4)",      "businessPartnerGrouping",      _text),
            ("is_blocked",                 "BOOLEAN",         "businessPartnerIsBlocked",     _bool),
            ("is_marked_for_archiving",    "BOOLEAN",         "isMarkedForArchiving",         _bool),
            ("creation_date",              "DATE",            "creationDate",                 _date),
            ("last_change_date",           "DATE",            "lastChangeDate",               _date),
            ("created_by_user",            "VARCHAR(12)",     "createdByUser",                _text),
            ("correspondence_language",    "VARCHAR(10)",     "correspondenceLanguage",       _text),
        ],
    },

    "business_partner_addresses": {
        "folder": "business_partner_addresses",
        "pk": ["business_partner", "address_id"],
        "cols": [
            ("business_partner",           "VARCHAR(20)",  "businessPartner",            _text),
            ("address_id",                 "VARCHAR(10)",  "addressId",                  _text),
            ("address_uuid",               "VARCHAR(50)",  "addressUuid",                _text),
            ("address_time_zone",          "VARCHAR(6)",   "addressTimeZone",            _text),
            ("city_name",                  "VARCHAR(40)",  "cityName",                   _text),
            ("country",                    "VARCHAR(3)",   "country",                    _text),
            ("region",                     "VARCHAR(3)",   "region",                     _text),
            ("postal_code",                "VARCHAR(10)",  "postalCode",                 _text),
            ("street_name",                "VARCHAR(40)",  "streetName",                 _text),
            ("po_box",                     "VARCHAR(10)",  "poBox",                      _text),
            ("po_box_postal_code",         "VARCHAR(10)",  "poBoxPostalCode",            _text),
            ("tax_jurisdiction",           "VARCHAR(15)",  "taxJurisdiction",            _text),
            ("transport_zone",             "VARCHAR(10)",  "transportZone",              _text),
            ("validity_start_date",        "DATE",         "validityStartDate",          _date),
            ("validity_end_date",          "DATE",         "validityEndDate",            _date),
        ],
    },

    "customer_company_assignments": {
        "folder": "customer_company_assignments",
        "pk": ["customer", "company_code"],
        "cols": [
            ("customer",                   "VARCHAR(20)",     "customer",                  _text),
            ("company_code",               "VARCHAR(4)",      "companyCode",               _text),
            ("reconciliation_account",     "VARCHAR(10)",     "reconciliationAccount",     _text),
            ("payment_terms",              "VARCHAR(4)",      "paymentTerms",              _text),
            ("payment_methods_list",       "VARCHAR(25)",     "paymentMethodsList",        _text),
            ("payment_blocking_reason",    "VARCHAR(2)",      "paymentBlockingReason",     _text),
            ("accounting_clerk",           "VARCHAR(3)",      "accountingClerk",           _text),
            ("customer_account_group",     "VARCHAR(4)",      "customerAccountGroup",      _text),
            ("deletion_indicator",         "BOOLEAN",         "deletionIndicator",         _bool),
        ],
    },

    "customer_sales_area_assignments": {
        "folder": "customer_sales_area_assignments",
        "pk": ["customer", "sales_organization", "distribution_channel", "division"],
        "cols": [
            ("customer",                       "VARCHAR(20)",     "customer",                      _text),
            ("sales_organization",             "VARCHAR(4)",      "salesOrganization",             _text),
            ("distribution_channel",           "VARCHAR(2)",      "distributionChannel",           _text),
            ("division",                       "VARCHAR(2)",      "division",                      _text),
            ("currency",                       "VARCHAR(3)",      "currency",                      _text),
            ("customer_payment_terms",         "VARCHAR(4)",      "customerPaymentTerms",          _text),
            ("delivery_priority",              "VARCHAR(2)",      "deliveryPriority",              _text),
            ("shipping_condition",             "VARCHAR(2)",      "shippingCondition",             _text),
            ("incoterms_classification",       "VARCHAR(3)",      "incotermsClassification",       _text),
            ("incoterms_location1",            "VARCHAR(27)",     "incotermsLocation1",            _text),
            ("credit_control_area",            "VARCHAR(4)",      "creditControlArea",             _text),
            ("sales_district",                 "VARCHAR(3)",      "salesDistrict",                 _text),
            ("sales_group",                    "VARCHAR(3)",      "salesGroup",                    _text),
            ("sales_office",                   "VARCHAR(4)",      "salesOffice",                   _text),
            ("supplying_plant",                "VARCHAR(4)",      "supplyingPlant",                _text),
            ("billing_is_blocked",             "VARCHAR(2)",      "billingIsBlockedForCustomer",   _text),
            ("complete_delivery_is_defined",   "BOOLEAN",         "completeDeliveryIsDefined",     _bool),
            ("unlimited_overdelivery_allowed", "BOOLEAN",         "slsUnlmtdOvrdelivIsAllwd",      _bool),
        ],
    },

    "products": {
        "folder": "products",
        "pk": ["product"],
        "cols": [
            ("product",                        "VARCHAR(40)",     "product",                       _text),
            ("product_type",                   "VARCHAR(4)",      "productType",                   _text),
            ("product_old_id",                 "VARCHAR(18)",     "productOldId",                  _text),
            ("base_unit",                      "VARCHAR(3)",      "baseUnit",                      _text),
            ("division",                       "VARCHAR(2)",      "division",                      _text),
            ("industry_sector",                "VARCHAR(2)",      "industrySector",                _text),
            ("product_group",                  "VARCHAR(9)",      "productGroup",                  _text),
            ("gross_weight",                   "NUMERIC(13,3)",   "grossWeight",                   _real),
            ("net_weight",                     "NUMERIC(13,3)",   "netWeight",                     _real),
            ("weight_unit",                    "VARCHAR(3)",      "weightUnit",                    _text),
            ("cross_plant_status",             "VARCHAR(10)",     "crossPlantStatus",              _text),
            ("cross_plant_status_valid_date",  "DATE",            "crossPlantStatusValidityDate",  _date),
            ("is_marked_for_deletion",         "BOOLEAN",         "isMarkedForDeletion",           _bool),
            ("creation_date",                  "DATE",            "creationDate",                  _date),
            ("created_by_user",                "VARCHAR(12)",     "createdByUser",                 _text),
            ("last_change_date",               "DATE",            "lastChangeDate",                _date),
            ("last_change_datetime",           "VARCHAR(30)",     "lastChangeDateTime",            _datetime),
        ],
    },

    "product_descriptions": {
        "folder": "product_descriptions",
        "pk": ["product", "language"],
        "cols": [
            ("product",              "VARCHAR(40)",  "product",             _text),
            ("language",             "VARCHAR(10)",  "language",            _text),
            ("product_description",  "TEXT",         "productDescription",  _text),
        ],
    },

    "plants": {
        "folder": "plants",
        "pk": ["plant"],
        "cols": [
            ("plant",                               "VARCHAR(4)",      "plant",                            _text),
            ("plant_name",                          "VARCHAR(30)",     "plantName",                        _text),
            ("valuation_area",                      "VARCHAR(4)",      "valuationArea",                    _text),
            ("plant_customer",                      "VARCHAR(20)",     "plantCustomer",                    _text),
            ("plant_supplier",                      "VARCHAR(20)",     "plantSupplier",                    _text),
            ("factory_calendar",                    "VARCHAR(2)",      "factoryCalendar",                  _text),
            ("default_purchasing_organization",     "VARCHAR(4)",      "defaultPurchasingOrganization",    _text),
            ("sales_organization",                  "VARCHAR(4)",      "salesOrganization",                _text),
            ("address_id",                          "VARCHAR(10)",     "addressId",                        _text),
            ("plant_category",                      "VARCHAR(10)",     "plantCategory",                    _text),
            ("distribution_channel",                "VARCHAR(2)",      "distributionChannel",              _text),
            ("division",                            "VARCHAR(2)",      "division",                         _text),
            ("language",                            "VARCHAR(10)",     "language",                         _text),
            ("is_marked_for_archiving",             "BOOLEAN",         "isMarkedForArchiving",             _bool),
        ],
    },

    "product_plants": {
        "folder": "product_plants",
        "pk": ["product", "plant"],
        "cols": [
            ("product",                        "VARCHAR(40)",  "product",                       _text),
            ("plant",                          "VARCHAR(4)",   "plant",                         _text),
            ("country_of_origin",              "VARCHAR(3)",   "countryOfOrigin",               _text),
            ("region_of_origin",               "VARCHAR(3)",   "regionOfOrigin",                _text),
            ("mrp_type",                       "VARCHAR(2)",   "mrpType",                       _text),
            ("availability_check_type",        "VARCHAR(2)",   "availabilityCheckType",         _text),
            ("fiscal_year_variant",            "VARCHAR(2)",   "fiscalYearVariant",             _text),
            ("profit_center",                  "VARCHAR(10)",  "profitCenter",                  _text),
            ("production_invtry_managed_loc",  "VARCHAR(4)",   "productionInvtryManagedLoc",    _text),
        ],
    },

    "product_storage_locations": {
        "folder": "product_storage_locations",
        "pk": ["product", "plant", "storage_location"],
        "cols": [
            ("product",                        "VARCHAR(40)",  "product",                          _text),
            ("plant",                          "VARCHAR(4)",   "plant",                            _text),
            ("storage_location",               "VARCHAR(4)",   "storageLocation",                  _text),
            ("physical_inventory_block",       "VARCHAR(10)",  "physicalInventoryBlockInd",        _text),
            ("date_last_posted_unrestricted",  "DATE",         "dateOfLastPostedCntUnRstrcdStk",   _date),
        ],
    },
}


# ---------------------------------------------------------------------------
# Index definitions — (table, [col, ...])
# These cover every join path discovered during data analysis.
# ---------------------------------------------------------------------------

INDEXES = [
    # Core flow traversal
    ("sales_order_headers",           ["sold_to_party"]),
    ("sales_order_headers",           ["overall_delivery_status"]),
    ("sales_order_headers",           ["creation_date"]),
    ("sales_order_items",             ["sales_order"]),
    ("sales_order_items",             ["material"]),
    ("sales_order_schedule_lines",    ["sales_order", "sales_order_item"]),
    ("outbound_delivery_items",       ["reference_sd_document"]),
    ("outbound_delivery_items",       ["delivery_document"]),
    ("outbound_delivery_items",       ["plant"]),
    ("outbound_delivery_headers",     ["shipping_point"]),
    ("billing_document_headers",      ["sold_to_party"]),
    ("billing_document_headers",      ["accounting_document"]),
    ("billing_document_headers",      ["billing_doc_is_cancelled"]),
    ("billing_document_headers",      ["billing_document_date"]),
    ("billing_document_items",        ["billing_document"]),
    ("billing_document_items",        ["reference_sd_document"]),
    ("billing_document_items",        ["material"]),
    ("billing_document_cancellations",["billing_doc_is_cancelled"]),
    ("journal_entry_items_ar",        ["accounting_document"]),
    ("journal_entry_items_ar",        ["reference_document"]),
    ("journal_entry_items_ar",        ["customer"]),
    ("journal_entry_items_ar",        ["posting_date"]),
    ("payments_ar",                   ["clearing_accounting_document"]),
    ("payments_ar",                   ["customer"]),
    ("payments_ar",                   ["clearing_date"]),
    ("business_partners",             ["customer"]),
    ("business_partner_addresses",    ["business_partner"]),
    ("business_partner_addresses",    ["region"]),
    ("product_descriptions",          ["language"]),
    ("product_plants",                ["plant"]),
    ("product_storage_locations",     ["plant"]),
    ("product_storage_locations",     ["storage_location"]),
    ("plants",                        ["sales_organization"]),
]


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------

def load_jsonl_folder(data_dir: Path, folder: str) -> list[dict]:
    """Read all *.jsonl part-files in a folder and return list of dicts."""
    pattern = str(data_dir / folder / "*.jsonl")
    files = sorted(glob.glob(pattern))
    if not files:
        log.warning("No JSONL files found in %s/%s", data_dir, folder)
        return []
    records = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    log.warning("JSON parse error in %s line %d: %s", f, lineno, e)
    return records


def transform_record(record: dict, col_defs: list) -> tuple:
    """Apply transform functions and return a tuple matching col_defs order."""
    row = []
    for col_name, _sql_type, json_key, transform in col_defs:
        raw = record.get(json_key)
        row.append(transform(raw) if transform else raw)
    return tuple(row)


# ---------------------------------------------------------------------------
# DDL builder
# ---------------------------------------------------------------------------

def build_create_table(table: str, schema: dict) -> str:
    pk_cols = schema["pk"]
    col_defs = schema["cols"]
    lines = []
    for col_name, sql_type, *_ in col_defs:
        lines.append(f"    {col_name}  {sql_type}")
    pk_clause = ", ".join(pk_cols)
    lines.append(f"    PRIMARY KEY ({pk_clause})")
    return f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(lines) + "\n)"


# ---------------------------------------------------------------------------
# Main ingest routine
# ---------------------------------------------------------------------------

def ingest(data_dir: Path, db_name: str) -> None:
    log.info("Data directory  : %s", data_dir)
    log.info("PostgreSQL database: %s (host=%s, port=%d, user=%s)", 
             db_name, DB_HOST, DB_PORT, DB_USER)

    if not data_dir.exists():
        log.error("Data directory not found: %s", data_dir)
        sys.exit(1)

    # Connect to PostgreSQL
    try:
        # con = psycopg2.connect(
        #     host=DB_HOST,
        #     port=DB_PORT,
        #     user=DB_USER,
        #     password=DB_PASSWORD,
        #     database=db_name,
        # )

        con = psycopg2.connect(DATABASE_URL)
        log.info("Connected to PostgreSQL database: %s", db_name)
    except psycopg2.Error as e:
        log.error("Failed to connect to PostgreSQL: %s", e)
        sys.exit(1)

    totals = {}

    try:
        cur = con.cursor()
        for table, schema in SCHEMAS.items():
            folder   = schema["folder"]
            col_defs = schema["cols"]
            pk_cols  = schema["pk"]

            # Create table
            ddl = build_create_table(table, schema)
            cur.execute(ddl)
            con.commit()
            log.info("Table %-40s created/verified", table)

            # Load records
            raw_records = load_jsonl_folder(data_dir, folder)
            if not raw_records:
                totals[table] = 0
                continue

            # Deduplicate on PK before insert (handles multi-part files)
            col_names   = [c[0] for c in col_defs]
            pk_indices  = [col_names.index(pk) for pk in pk_cols]
            seen_pks    = set()
            unique_rows = []
            for rec in raw_records:
                row = transform_record(rec, col_defs)
                pk_val = tuple(row[i] for i in pk_indices)
                if pk_val in seen_pks:
                    continue
                seen_pks.add(pk_val)
                unique_rows.append(row)

            # INSERT with ON CONFLICT for idempotency (re-running ingest is safe)
            if unique_rows:
                placeholders = ", ".join(["%s"] * len(col_names))
                col_list     = ", ".join(col_names)
                pk_clause    = ", ".join(pk_cols)
                sql = (
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                    f"ON CONFLICT ({pk_clause}) DO UPDATE SET "
                    f"{', '.join(f'{col}=EXCLUDED.{col}' for col in col_names if col not in pk_cols)}"
                )
                
                # Batch insert in chunks to avoid timeout on large tables
                chunk_size = 100
                for i in range(0, len(unique_rows), chunk_size):
                    chunk = unique_rows[i:i+chunk_size]
                    try:
                        cur.executemany(sql, chunk)
                        con.commit()
                    except psycopg2.Error as e:
                        log.warning("Error inserting chunk into %s: %s", table, e)
                        con.rollback()
                        # Fall back to row-by-row insert
                        for row in chunk:
                            try:
                                cur.execute(sql, row)
                                con.commit()
                            except psycopg2.Error as row_error:
                                log.warning("Error inserting row into %s: %s", table, row_error)

            skipped = len(raw_records) - len(unique_rows)
            log.info(
                "  %-40s  %4d rows inserted  (%d duplicates skipped)",
                table, len(unique_rows), skipped,
            )
            totals[table] = len(unique_rows)

        # Create indexes
        log.info("Creating indexes …")
        for table, cols in INDEXES:
            idx_name = f"idx_{table}__{'_'.join(cols)}"
            col_list = ", ".join(cols)
            try:
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({col_list})"
                )
            except psycopg2.Error as e:
                log.warning("Error creating index %s: %s", idx_name, e)
        
        con.commit()
        log.info("Indexes created.")

    finally:
        cur.close()
        con.close()

    # ---------------------------------------------------------------------------
    # Summary report
    # ---------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  INGEST SUMMARY")
    print("=" * 60)
    print(f"  {'Table':<45} {'Rows':>6}")
    print(f"  {'-'*45} {'-'*6}")
    grand_total = 0
    for table, count in totals.items():
        print(f"  {table:<45} {count:>6,}")
        grand_total += count
    print(f"  {'-'*45} {'-'*6}")
    print(f"  {'TOTAL':<45} {grand_total:>6,}")
    print("=" * 60)

    # ---------------------------------------------------------------------------
    # Join path validation
    # ---------------------------------------------------------------------------
    print("\n  JOIN PATH VALIDATION")
    print("  " + "-" * 57)

    # con = psycopg2.connect(
    #     host=DB_HOST,
    #     port=DB_PORT,
    #     user=DB_USER,
    #     password=DB_PASSWORD,
    #     database=db_name,
    # )

    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor()
    
    checks = [
        (
            "SO → Delivery (86/86 expected)",
            """SELECT COUNT(DISTINCT odi.reference_sd_document)
               FROM outbound_delivery_items odi
               JOIN sales_order_headers soh
                 ON odi.reference_sd_document = soh.sales_order""",
        ),
        (
            "Delivery → Billing (83/86 expected)",
            """SELECT COUNT(DISTINCT bdi.reference_sd_document)
               FROM billing_document_items bdi
               JOIN outbound_delivery_headers odh
                 ON bdi.reference_sd_document = odh.delivery_document""",
        ),
        (
            "Billing → Journal via accountingDocument (123/163 expected)",
            """SELECT COUNT(DISTINCT je.accounting_document)
               FROM journal_entry_items_ar je
               JOIN billing_document_headers bdh
                 ON je.accounting_document = bdh.accounting_document""",
        ),
        (
            "Payment → Billing via clearingAccountingDocument (76/120 expected)",
            """SELECT COUNT(*)
               FROM payments_ar p
               JOIN billing_document_headers bdh
                 ON p.clearing_accounting_document = bdh.accounting_document""",
        ),
        (
            "SO customers → Business Partners (8/8 expected)",
            """SELECT COUNT(DISTINCT soh.sold_to_party)
               FROM sales_order_headers soh
               JOIN business_partners bp
                 ON soh.sold_to_party = bp.customer""",
        ),
        (
            "SO materials → Products (69/69 expected)",
            """SELECT COUNT(DISTINCT soi.material)
               FROM sales_order_items soi
               JOIN products p ON soi.material = p.product""",
        ),
    ]

    all_ok = True
    for label, sql_query in checks:
        try:
            cur.execute(sql_query)
            result = cur.fetchone()[0]
            status = "OK" if result > 0 else "FAIL"
            if status == "FAIL":
                all_ok = False
            print(f"  [{status}] {label}: {result}")
        except psycopg2.Error as e:
            print(f"  [FAIL] {label}: Error - {e}")
            all_ok = False

    cur.close()
    con.close()
    
    print("=" * 60)
    print(f"\n  Database populated: {db_name}")
    print(f"  All join paths OK : {all_ok}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest SAP O2C JSONL dataset into PostgreSQL"
    )
    parser.add_argument(
        "--data-dir",
        default="./sap-order-to-cash-dataset/sap-o2c-data",
        help="Path to the extracted dataset folder (default: ./sap-order-to-cash-dataset/sap-o2c-data)",
    )
    parser.add_argument(
        "--db-name",
        default="dodgeai_o2c",
        help="PostgreSQL database name (default: dodgeai_o2c). Database must exist.",
    )
    args = parser.parse_args()

    ingest(
        data_dir=Path(args.data_dir),
        db_name=args.db_name,
    )

"""
ingest.py — SAP Order-to-Cash Dataset → SQLite

Usage:
    python ingest.py --data-dir ./sap-o2c-data --db-path ./o2c.db

What it does:
    1. Reads all JSONL part-files from each entity folder
    2. Creates a normalized SQLite schema (19 tables)
    3. Flattens nested fields (e.g. creationTime dict → creationTime TEXT)
    4. Coerces types (amounts → REAL, booleans → INTEGER, dates → TEXT ISO)
    5. Deduplicates on primary keys before insert
    6. Creates all indexes needed for graph traversal and LLM-generated SQL
    7. Prints a summary of row counts and join-path validation
"""

import argparse
import glob
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema definitions
# Each entry: (table_name, folder_name, primary_key_cols, column_definitions)
# Column definitions: list of (col_name, sqlite_type, jsonl_key, transform_fn)
# transform_fn is optional — None means use raw value as-is (cast to type).
# ---------------------------------------------------------------------------

def _bool(v):
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, str):
        return 1 if v.lower() in ("true", "1", "yes") else 0
    return 0 if v is None else int(bool(v))


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


# (col_name, sqlite_type, jsonl_key, transform_fn)
SCHEMAS: dict[str, Any] = {

    "sales_order_headers": {
        "folder": "sales_order_headers",
        "pk": ["sales_order"],
        "cols": [
            ("sales_order",                "TEXT",    "salesOrder",                  _text),
            ("sales_order_type",           "TEXT",    "salesOrderType",               _text),
            ("sales_organization",         "TEXT",    "salesOrganization",            _text),
            ("distribution_channel",       "TEXT",    "distributionChannel",          _text),
            ("organization_division",      "TEXT",    "organizationDivision",         _text),
            ("sales_group",                "TEXT",    "salesGroup",                   _text),
            ("sales_office",               "TEXT",    "salesOffice",                  _text),
            ("sold_to_party",              "TEXT",    "soldToParty",                  _text),
            ("creation_date",              "TEXT",    "creationDate",                 _date),
            ("created_by_user",            "TEXT",    "createdByUser",                _text),
            ("last_change_datetime",       "TEXT",    "lastChangeDateTime",           _datetime),
            ("total_net_amount",           "REAL",    "totalNetAmount",               _real),
            ("transaction_currency",       "TEXT",    "transactionCurrency",          _text),
            ("overall_delivery_status",    "TEXT",    "overallDeliveryStatus",        _text),
            ("overall_billing_status",     "TEXT",    "overallOrdReltdBillgStatus",   _text),
            ("overall_sd_ref_status",      "TEXT",    "overallSdDocReferenceStatus",  _text),
            ("pricing_date",               "TEXT",    "pricingDate",                  _date),
            ("requested_delivery_date",    "TEXT",    "requestedDeliveryDate",        _date),
            ("header_billing_block",       "TEXT",    "headerBillingBlockReason",     _text),
            ("delivery_block_reason",      "TEXT",    "deliveryBlockReason",          _text),
            ("incoterms_classification",   "TEXT",    "incotermsClassification",      _text),
            ("incoterms_location1",        "TEXT",    "incotermsLocation1",           _text),
            ("customer_payment_terms",     "TEXT",    "customerPaymentTerms",         _text),
            ("total_credit_check_status",  "TEXT",    "totalCreditCheckStatus",       _text),
        ],
    },

    "sales_order_items": {
        "folder": "sales_order_items",
        "pk": ["sales_order", "sales_order_item"],
        "cols": [
            ("sales_order",                "TEXT",    "salesOrder",                  _text),
            ("sales_order_item",           "TEXT",    "salesOrderItem",               _text),
            ("sales_order_item_category",  "TEXT",    "salesOrderItemCategory",       _text),
            ("material",                   "TEXT",    "material",                     _text),
            ("requested_quantity",         "REAL",    "requestedQuantity",            _real),
            ("requested_quantity_unit",    "TEXT",    "requestedQuantityUnit",        _text),
            ("net_amount",                 "REAL",    "netAmount",                    _real),
            ("transaction_currency",       "TEXT",    "transactionCurrency",          _text),
            ("material_group",             "TEXT",    "materialGroup",                _text),
            ("production_plant",           "TEXT",    "productionPlant",              _text),
            ("storage_location",           "TEXT",    "storageLocation",              _text),
            ("rejection_reason",           "TEXT",    "salesDocumentRjcnReason",      _text),
            ("item_billing_block",         "TEXT",    "itemBillingBlockReason",       _text),
        ],
    },

    "sales_order_schedule_lines": {
        "folder": "sales_order_schedule_lines",
        "pk": ["sales_order", "sales_order_item", "schedule_line"],
        "cols": [
            ("sales_order",                "TEXT",    "salesOrder",                  _text),
            ("sales_order_item",           "TEXT",    "salesOrderItem",               _text),
            ("schedule_line",              "TEXT",    "scheduleLine",                 _text),
            ("confirmed_delivery_date",    "TEXT",    "confirmedDeliveryDate",        _date),
            ("order_quantity_unit",        "TEXT",    "orderQuantityUnit",            _text),
            ("confirmed_quantity",         "REAL",    "confdOrderQtyByMatlAvailCheck",_real),
        ],
    },

    "outbound_delivery_headers": {
        "folder": "outbound_delivery_headers",
        "pk": ["delivery_document"],
        "cols": [
            ("delivery_document",              "TEXT",  "deliveryDocument",              _text),
            ("shipping_point",                 "TEXT",  "shippingPoint",                 _text),
            ("creation_date",                  "TEXT",  "creationDate",                  _date),
            ("creation_time",                  "TEXT",  "creationTime",                  _text),
            ("last_change_date",               "TEXT",  "lastChangeDate",                _date),
            ("actual_goods_movement_date",     "TEXT",  "actualGoodsMovementDate",       _date),
            ("actual_goods_movement_time",     "TEXT",  "actualGoodsMovementTime",       _text),
            ("delivery_block_reason",          "TEXT",  "deliveryBlockReason",           _text),
            ("header_billing_block",           "TEXT",  "headerBillingBlockReason",      _text),
            ("overall_goods_movement_status",  "TEXT",  "overallGoodsMovementStatus",    _text),
            ("overall_picking_status",         "TEXT",  "overallPickingStatus",          _text),
            ("overall_pod_status",             "TEXT",  "overallProofOfDeliveryStatus",  _text),
            ("hdr_general_incompletion",       "TEXT",  "hdrGeneralIncompletionStatus",  _text),
        ],
    },

    "outbound_delivery_items": {
        "folder": "outbound_delivery_items",
        "pk": ["delivery_document", "delivery_document_item"],
        "cols": [
            ("delivery_document",          "TEXT",  "deliveryDocument",          _text),
            ("delivery_document_item",     "TEXT",  "deliveryDocumentItem",      _text),
            ("reference_sd_document",      "TEXT",  "referenceSdDocument",       _text),   # → sales_order
            ("reference_sd_doc_item",      "TEXT",  "referenceSdDocumentItem",   _text),
            ("actual_delivery_quantity",   "REAL",  "actualDeliveryQuantity",    _real),
            ("delivery_quantity_unit",     "TEXT",  "deliveryQuantityUnit",      _text),
            ("plant",                      "TEXT",  "plant",                     _text),
            ("storage_location",           "TEXT",  "storageLocation",           _text),
            ("batch",                      "TEXT",  "batch",                     _text),
            ("item_billing_block",         "TEXT",  "itemBillingBlockReason",    _text),
            ("last_change_date",           "TEXT",  "lastChangeDate",            _date),
        ],
    },

    "billing_document_headers": {
        "folder": "billing_document_headers",
        "pk": ["billing_document"],
        "cols": [
            ("billing_document",           "TEXT",     "billingDocument",             _text),
            ("billing_document_type",      "TEXT",     "billingDocumentType",          _text),
            ("billing_document_date",      "TEXT",     "billingDocumentDate",          _date),
            ("creation_date",              "TEXT",     "creationDate",                 _date),
            ("creation_time",              "TEXT",     "creationTime",                 _text),
            ("last_change_datetime",       "TEXT",     "lastChangeDateTime",           _datetime),
            ("billing_doc_is_cancelled",   "INTEGER",  "billingDocumentIsCancelled",   _bool),
            ("cancelled_billing_document", "TEXT",     "cancelledBillingDocument",     _text),
            ("total_net_amount",           "REAL",     "totalNetAmount",               _real),
            ("transaction_currency",       "TEXT",     "transactionCurrency",          _text),
            ("company_code",               "TEXT",     "companyCode",                  _text),
            ("fiscal_year",                "TEXT",     "fiscalYear",                   _text),
            ("accounting_document",        "TEXT",     "accountingDocument",           _text),   # → journal / payment
            ("sold_to_party",              "TEXT",     "soldToParty",                  _text),   # → business_partners
        ],
    },

    "billing_document_items": {
        "folder": "billing_document_items",
        "pk": ["billing_document", "billing_document_item"],
        "cols": [
            ("billing_document",           "TEXT",  "billingDocument",           _text),
            ("billing_document_item",      "TEXT",  "billingDocumentItem",       _text),
            ("material",                   "TEXT",  "material",                  _text),   # → products
            ("billing_quantity",           "REAL",  "billingQuantity",           _real),
            ("billing_quantity_unit",      "TEXT",  "billingQuantityUnit",       _text),
            ("net_amount",                 "REAL",  "netAmount",                 _real),
            ("transaction_currency",       "TEXT",  "transactionCurrency",       _text),
            ("reference_sd_document",      "TEXT",  "referenceSdDocument",       _text),   # → delivery_document
            ("reference_sd_doc_item",      "TEXT",  "referenceSdDocumentItem",   _text),
        ],
    },

    "billing_document_cancellations": {
        "folder": "billing_document_cancellations",
        "pk": ["billing_document"],
        "cols": [
            ("billing_document",           "TEXT",     "billingDocument",             _text),
            ("billing_document_type",      "TEXT",     "billingDocumentType",          _text),
            ("billing_document_date",      "TEXT",     "billingDocumentDate",          _date),
            ("creation_date",              "TEXT",     "creationDate",                 _date),
            ("creation_time",              "TEXT",     "creationTime",                 _text),
            ("last_change_datetime",       "TEXT",     "lastChangeDateTime",           _datetime),
            ("billing_doc_is_cancelled",   "INTEGER",  "billingDocumentIsCancelled",   _bool),
            ("cancelled_billing_document", "TEXT",     "cancelledBillingDocument",     _text),
            ("total_net_amount",           "REAL",     "totalNetAmount",               _real),
            ("transaction_currency",       "TEXT",     "transactionCurrency",          _text),
            ("company_code",               "TEXT",     "companyCode",                  _text),
            ("fiscal_year",                "TEXT",     "fiscalYear",                   _text),
            ("accounting_document",        "TEXT",     "accountingDocument",           _text),
            ("sold_to_party",              "TEXT",     "soldToParty",                  _text),
        ],
    },

    "journal_entry_items_ar": {
        "folder": "journal_entry_items_accounts_receivable",
        "pk": ["company_code", "fiscal_year", "accounting_document", "accounting_document_item"],
        "cols": [
            ("company_code",                    "TEXT",  "companyCode",                  _text),
            ("fiscal_year",                     "TEXT",  "fiscalYear",                   _text),
            ("accounting_document",             "TEXT",  "accountingDocument",           _text),   # → billing_document_headers
            ("accounting_document_item",        "TEXT",  "accountingDocumentItem",       _text),
            ("accounting_document_type",        "TEXT",  "accountingDocumentType",       _text),
            ("reference_document",              "TEXT",  "referenceDocument",            _text),   # → billing_document (direct)
            ("gl_account",                      "TEXT",  "glAccount",                    _text),
            ("customer",                        "TEXT",  "customer",                     _text),   # → business_partners
            ("cost_center",                     "TEXT",  "costCenter",                   _text),
            ("profit_center",                   "TEXT",  "profitCenter",                 _text),
            ("transaction_currency",            "TEXT",  "transactionCurrency",          _text),
            ("amount_in_transaction_currency",  "REAL",  "amountInTransactionCurrency",  _real),
            ("company_code_currency",           "TEXT",  "companyCodeCurrency",          _text),
            ("amount_in_company_code_currency", "REAL",  "amountInCompanyCodeCurrency",  _real),
            ("posting_date",                    "TEXT",  "postingDate",                  _date),
            ("document_date",                   "TEXT",  "documentDate",                 _date),
            ("financial_account_type",          "TEXT",  "financialAccountType",         _text),
            ("clearing_date",                   "TEXT",  "clearingDate",                 _date),
            ("clearing_accounting_document",    "TEXT",  "clearingAccountingDocument",   _text),
            ("clearing_doc_fiscal_year",        "TEXT",  "clearingDocFiscalYear",        _text),
            ("assignment_reference",            "TEXT",  "assignmentReference",          _text),
            ("last_change_datetime",            "TEXT",  "lastChangeDateTime",           _datetime),
        ],
    },

    "payments_ar": {
        "folder": "payments_accounts_receivable",
        "pk": ["company_code", "fiscal_year", "accounting_document", "accounting_document_item"],
        "cols": [
            ("company_code",                    "TEXT",  "companyCode",                  _text),
            ("fiscal_year",                     "TEXT",  "fiscalYear",                   _text),
            ("accounting_document",             "TEXT",  "accountingDocument",           _text),
            ("accounting_document_item",        "TEXT",  "accountingDocumentItem",       _text),
            ("customer",                        "TEXT",  "customer",                     _text),   # → business_partners
            ("clearing_date",                   "TEXT",  "clearingDate",                 _date),
            # KEY LINK: clearingAccountingDocument → billing_document_headers.accounting_document
            ("clearing_accounting_document",    "TEXT",  "clearingAccountingDocument",   _text),
            ("clearing_doc_fiscal_year",        "TEXT",  "clearingDocFiscalYear",        _text),
            ("amount_in_transaction_currency",  "REAL",  "amountInTransactionCurrency",  _real),
            ("transaction_currency",            "TEXT",  "transactionCurrency",          _text),
            ("amount_in_company_code_currency", "REAL",  "amountInCompanyCodeCurrency",  _real),
            ("company_code_currency",           "TEXT",  "companyCodeCurrency",          _text),
            ("invoice_reference",               "TEXT",  "invoiceReference",             _text),   # NULL in data
            ("invoice_reference_fiscal_year",   "TEXT",  "invoiceReferenceFiscalYear",   _text),
            ("sales_document",                  "TEXT",  "salesDocument",                _text),   # NULL in data
            ("sales_document_item",             "TEXT",  "salesDocumentItem",            _text),
            ("posting_date",                    "TEXT",  "postingDate",                  _date),
            ("document_date",                   "TEXT",  "documentDate",                 _date),
            ("assignment_reference",            "TEXT",  "assignmentReference",          _text),
            ("gl_account",                      "TEXT",  "glAccount",                    _text),
            ("financial_account_type",          "TEXT",  "financialAccountType",         _text),
            ("profit_center",                   "TEXT",  "profitCenter",                 _text),
            ("cost_center",                     "TEXT",  "costCenter",                   _text),
        ],
    },

    "business_partners": {
        "folder": "business_partners",
        "pk": ["business_partner"],
        "cols": [
            ("business_partner",           "TEXT",     "businessPartner",              _text),
            ("customer",                   "TEXT",     "customer",                     _text),   # same as businessPartner in this dataset
            ("business_partner_category",  "TEXT",     "businessPartnerCategory",       _text),
            ("business_partner_full_name", "TEXT",     "businessPartnerFullName",       _text),
            ("business_partner_name",      "TEXT",     "businessPartnerName",           _text),
            ("organization_bp_name1",      "TEXT",     "organizationBpName1",           _text),
            ("organization_bp_name2",      "TEXT",     "organizationBpName2",           _text),
            ("first_name",                 "TEXT",     "firstName",                     _text),
            ("last_name",                  "TEXT",     "lastName",                      _text),
            ("industry",                   "TEXT",     "industry",                      _text),
            ("business_partner_grouping",  "TEXT",     "businessPartnerGrouping",       _text),
            ("is_blocked",                 "INTEGER",  "businessPartnerIsBlocked",      _bool),
            ("is_marked_for_archiving",    "INTEGER",  "isMarkedForArchiving",          _bool),
            ("creation_date",              "TEXT",     "creationDate",                  _date),
            ("last_change_date",           "TEXT",     "lastChangeDate",                _date),
            ("created_by_user",            "TEXT",     "createdByUser",                 _text),
            ("correspondence_language",    "TEXT",     "correspondenceLanguage",        _text),
        ],
    },

    "business_partner_addresses": {
        "folder": "business_partner_addresses",
        "pk": ["business_partner", "address_id"],
        "cols": [
            ("business_partner",           "TEXT",  "businessPartner",            _text),
            ("address_id",                 "TEXT",  "addressId",                  _text),
            ("address_uuid",               "TEXT",  "addressUuid",                _text),
            ("address_time_zone",          "TEXT",  "addressTimeZone",            _text),
            ("city_name",                  "TEXT",  "cityName",                   _text),
            ("country",                    "TEXT",  "country",                    _text),
            ("region",                     "TEXT",  "region",                     _text),
            ("postal_code",                "TEXT",  "postalCode",                 _text),
            ("street_name",                "TEXT",  "streetName",                 _text),
            ("po_box",                     "TEXT",  "poBox",                      _text),
            ("po_box_postal_code",         "TEXT",  "poBoxPostalCode",            _text),
            ("tax_jurisdiction",           "TEXT",  "taxJurisdiction",            _text),
            ("transport_zone",             "TEXT",  "transportZone",              _text),
            ("validity_start_date",        "TEXT",  "validityStartDate",          _date),
            ("validity_end_date",          "TEXT",  "validityEndDate",            _date),
        ],
    },

    "customer_company_assignments": {
        "folder": "customer_company_assignments",
        "pk": ["customer", "company_code"],
        "cols": [
            ("customer",                   "TEXT",     "customer",                  _text),
            ("company_code",               "TEXT",     "companyCode",               _text),
            ("reconciliation_account",     "TEXT",     "reconciliationAccount",     _text),
            ("payment_terms",              "TEXT",     "paymentTerms",              _text),
            ("payment_methods_list",       "TEXT",     "paymentMethodsList",        _text),
            ("payment_blocking_reason",    "TEXT",     "paymentBlockingReason",     _text),
            ("accounting_clerk",           "TEXT",     "accountingClerk",           _text),
            ("customer_account_group",     "TEXT",     "customerAccountGroup",      _text),
            ("deletion_indicator",         "INTEGER",  "deletionIndicator",         _bool),
        ],
    },

    "customer_sales_area_assignments": {
        "folder": "customer_sales_area_assignments",
        "pk": ["customer", "sales_organization", "distribution_channel", "division"],
        "cols": [
            ("customer",                       "TEXT",     "customer",                      _text),
            ("sales_organization",             "TEXT",     "salesOrganization",             _text),
            ("distribution_channel",           "TEXT",     "distributionChannel",           _text),
            ("division",                       "TEXT",     "division",                      _text),
            ("currency",                       "TEXT",     "currency",                      _text),
            ("customer_payment_terms",         "TEXT",     "customerPaymentTerms",          _text),
            ("delivery_priority",              "TEXT",     "deliveryPriority",              _text),
            ("shipping_condition",             "TEXT",     "shippingCondition",             _text),
            ("incoterms_classification",       "TEXT",     "incotermsClassification",       _text),
            ("incoterms_location1",            "TEXT",     "incotermsLocation1",            _text),
            ("credit_control_area",            "TEXT",     "creditControlArea",             _text),
            ("sales_district",                 "TEXT",     "salesDistrict",                 _text),
            ("sales_group",                    "TEXT",     "salesGroup",                    _text),
            ("sales_office",                   "TEXT",     "salesOffice",                   _text),
            ("supplying_plant",                "TEXT",     "supplyingPlant",                _text),
            ("billing_is_blocked",             "TEXT",     "billingIsBlockedForCustomer",   _text),
            ("complete_delivery_is_defined",   "INTEGER",  "completeDeliveryIsDefined",     _bool),
            ("unlimited_overdelivery_allowed", "INTEGER",  "slsUnlmtdOvrdelivIsAllwd",      _bool),
        ],
    },

    "products": {
        "folder": "products",
        "pk": ["product"],
        "cols": [
            ("product",                        "TEXT",     "product",                       _text),
            ("product_type",                   "TEXT",     "productType",                   _text),
            ("product_old_id",                 "TEXT",     "productOldId",                  _text),
            ("base_unit",                      "TEXT",     "baseUnit",                      _text),
            ("division",                       "TEXT",     "division",                      _text),
            ("industry_sector",                "TEXT",     "industrySector",                _text),
            ("product_group",                  "TEXT",     "productGroup",                  _text),
            ("gross_weight",                   "REAL",     "grossWeight",                   _real),
            ("net_weight",                     "REAL",     "netWeight",                     _real),
            ("weight_unit",                    "TEXT",     "weightUnit",                    _text),
            ("cross_plant_status",             "TEXT",     "crossPlantStatus",              _text),
            ("cross_plant_status_valid_date",  "TEXT",     "crossPlantStatusValidityDate",  _date),
            ("is_marked_for_deletion",         "INTEGER",  "isMarkedForDeletion",           _bool),
            ("creation_date",                  "TEXT",     "creationDate",                  _date),
            ("created_by_user",                "TEXT",     "createdByUser",                 _text),
            ("last_change_date",               "TEXT",     "lastChangeDate",                _date),
            ("last_change_datetime",           "TEXT",     "lastChangeDateTime",            _datetime),
        ],
    },

    "product_descriptions": {
        "folder": "product_descriptions",
        "pk": ["product", "language"],
        "cols": [
            ("product",              "TEXT",  "product",             _text),
            ("language",             "TEXT",  "language",            _text),
            ("product_description",  "TEXT",  "productDescription",  _text),
        ],
    },

    "plants": {
        "folder": "plants",
        "pk": ["plant"],
        "cols": [
            ("plant",                               "TEXT",     "plant",                            _text),
            ("plant_name",                          "TEXT",     "plantName",                        _text),
            ("valuation_area",                      "TEXT",     "valuationArea",                    _text),
            ("plant_customer",                      "TEXT",     "plantCustomer",                    _text),
            ("plant_supplier",                      "TEXT",     "plantSupplier",                    _text),
            ("factory_calendar",                    "TEXT",     "factoryCalendar",                  _text),
            ("default_purchasing_organization",     "TEXT",     "defaultPurchasingOrganization",    _text),
            ("sales_organization",                  "TEXT",     "salesOrganization",                _text),
            ("address_id",                          "TEXT",     "addressId",                        _text),
            ("plant_category",                      "TEXT",     "plantCategory",                    _text),
            ("distribution_channel",                "TEXT",     "distributionChannel",              _text),
            ("division",                            "TEXT",     "division",                         _text),
            ("language",                            "TEXT",     "language",                         _text),
            ("is_marked_for_archiving",             "INTEGER",  "isMarkedForArchiving",             _bool),
        ],
    },

    "product_plants": {
        "folder": "product_plants",
        "pk": ["product", "plant"],
        "cols": [
            ("product",                        "TEXT",  "product",                       _text),
            ("plant",                          "TEXT",  "plant",                         _text),
            ("country_of_origin",              "TEXT",  "countryOfOrigin",               _text),
            ("region_of_origin",               "TEXT",  "regionOfOrigin",                _text),
            ("mrp_type",                       "TEXT",  "mrpType",                       _text),
            ("availability_check_type",        "TEXT",  "availabilityCheckType",         _text),
            ("fiscal_year_variant",            "TEXT",  "fiscalYearVariant",             _text),
            ("profit_center",                  "TEXT",  "profitCenter",                  _text),
            ("production_invtry_managed_loc",  "TEXT",  "productionInvtryManagedLoc",    _text),
        ],
    },

    "product_storage_locations": {
        "folder": "product_storage_locations",
        "pk": ["product", "plant", "storage_location"],
        "cols": [
            ("product",                        "TEXT",  "product",                          _text),
            ("plant",                          "TEXT",  "plant",                            _text),
            ("storage_location",               "TEXT",  "storageLocation",                  _text),
            ("physical_inventory_block",       "TEXT",  "physicalInventoryBlockInd",        _text),
            ("date_last_posted_unrestricted",  "TEXT",  "dateOfLastPostedCntUnRstrcdStk",   _date),
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
    ("outbound_delivery_items",       ["reference_sd_document"]),          # → sales_order
    ("outbound_delivery_items",       ["delivery_document"]),
    ("outbound_delivery_items",       ["plant"]),
    ("outbound_delivery_headers",     ["shipping_point"]),
    ("billing_document_headers",      ["sold_to_party"]),
    ("billing_document_headers",      ["accounting_document"]),            # → journal / payment
    ("billing_document_headers",      ["billing_doc_is_cancelled"]),
    ("billing_document_headers",      ["billing_document_date"]),
    ("billing_document_items",        ["billing_document"]),
    ("billing_document_items",        ["reference_sd_document"]),          # → delivery_document
    ("billing_document_items",        ["material"]),
    ("billing_document_cancellations",["billing_doc_is_cancelled"]),
    ("journal_entry_items_ar",        ["accounting_document"]),
    ("journal_entry_items_ar",        ["reference_document"]),             # → billing_document
    ("journal_entry_items_ar",        ["customer"]),
    ("journal_entry_items_ar",        ["posting_date"]),
    ("payments_ar",                   ["clearing_accounting_document"]),   # → billing acct doc (PRIMARY)
    ("payments_ar",                   ["customer"]),
    ("payments_ar",                   ["clearing_date"]),
    # Supporting entities
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

def ingest(data_dir: Path, db_path: Path) -> None:
    log.info("Data directory : %s", data_dir)
    log.info("SQLite database: %s", db_path)

    if not data_dir.exists():
        log.error("Data directory not found: %s", data_dir)
        sys.exit(1)

    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")

    totals = {}

    with con:
        for table, schema in SCHEMAS.items():
            folder   = schema["folder"]
            col_defs = schema["cols"]
            pk_cols  = schema["pk"]

            # Create table
            ddl = build_create_table(table, schema)
            con.execute(ddl)
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

            # INSERT OR REPLACE for idempotency (re-running ingest is safe)
            placeholders = ", ".join(["?"] * len(col_names))
            col_list     = ", ".join(col_names)
            sql = (
                f"INSERT OR REPLACE INTO {table} ({col_list}) "
                f"VALUES ({placeholders})"
            )
            con.executemany(sql, unique_rows)

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
            con.execute(
                f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({col_list})"
            )
        log.info("Indexes created.")

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

    con = sqlite3.connect(db_path)
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
    for label, sql in checks:
        result = con.execute(sql).fetchone()[0]
        status = "OK" if result > 0 else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"  [{status}] {label}: {result}")

    con.close()
    print("=" * 60)
    print(f"\n  Database written to: {db_path.resolve()}")
    print(f"  All join paths OK : {all_ok}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest SAP O2C JSONL dataset into SQLite"
    )
    parser.add_argument(
        "--data-dir",
        default="./sap-order-to-cash-dataset/sap-o2c-data",
        help="Path to the extracted dataset folder (default: ./sap-order-to-cash-dataset/sap-o2c-data)",
    )
    parser.add_argument(
        "--db-path",
        default="./o2c.db",
        help="Output SQLite database path (default: ./o2c.db)",
    )
    args = parser.parse_args()

    ingest(
        data_dir=Path(args.data_dir),
        db_path=Path(args.db_path),
    )
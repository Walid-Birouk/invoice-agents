"""Shared data model. Every agent and MCP server exchanges these shapes."""

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class Vendor(BaseModel):
    id: str
    name: str
    siret: str = Field(pattern=r"^\d{14}$")
    vat_number: str = Field(pattern=r"^FR\d{11}$")
    iban: str
    email: str
    payment_terms_days: int = 30


class POLine(BaseModel):
    sku: str
    description: str
    quantity: Decimal
    unit_price: Decimal  # HT, EUR


class PurchaseOrder(BaseModel):
    number: str
    vendor_id: str
    issued: date
    lines: list[POLine]


class ReceiptLine(BaseModel):
    sku: str
    quantity_received: Decimal


class Receipt(BaseModel):
    """Bon de livraison / goods received note."""
    number: str
    po_number: str
    received: date
    lines: list[ReceiptLine]


class InvoiceLine(BaseModel):
    sku: str | None = None
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_rate: Decimal  # e.g. 0.20


class Invoice(BaseModel):
    """Structured invoice as produced by the Extractor (Factur-X minimum + basic fields)."""
    number: str
    issue_date: date
    due_date: date | None = None
    seller_name: str
    seller_siret: str
    seller_vat: str
    seller_iban: str
    buyer_name: str
    buyer_siret: str
    po_number: str | None = None
    lines: list[InvoiceLine]
    total_ht: Decimal
    total_vat: Decimal
    total_ttc: Decimal
    source_file: str


class Severity(str, Enum):
    info = "info"
    warning = "warning"   # needs a human look, can be fixed with the supplier
    blocking = "blocking"  # must not be paid


class Finding(BaseModel):
    code: str  # duplicate, price_mismatch, qty_mismatch, iban_change, no_po, totals_mismatch
    severity: Severity
    message: str
    agent: str


class Decision(str, Enum):
    approve = "approve"         # queue for payment, human signs off
    hold = "hold"               # contact supplier, wait
    escalate = "escalate"       # possible fraud, human investigates


class InvoiceOutcome(BaseModel):
    invoice_number: str
    vendor_id: str | None
    decision: Decision
    findings: list[Finding] = []
    amount_ttc: Decimal

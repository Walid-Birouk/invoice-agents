"""MCP server: the buyer's ERP (vendors, POs, receipts, invoice history, payment queue).

Checks are deterministic code, not LLM judgement, so every finding is reproducible and auditable.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError

from ap_agents.config import DATA, STATE, load_config
from ap_agents.models import Finding, Invoice, PurchaseOrder, Receipt, Severity, Vendor

server = MCPServer("erp")
ERP = json.loads((DATA / "erp.json").read_text())
TOLERANCE = Decimal(str(load_config()["limits"]["price_tolerance"]))

VENDORS = [Vendor(**v) for v in ERP["vendors"]]
POS = {p["number"]: PurchaseOrder(**p) for p in ERP["purchase_orders"]}
RECEIPTS = [Receipt(**r) for r in ERP["receipts"]]


def _eur(x: Decimal) -> str:
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",")


def _norm(s: str) -> str:
    return s.replace(" ", "").upper()


def _dump(obj) -> str:
    if isinstance(obj, list):
        return json.dumps([o.model_dump(mode="json") for o in obj], ensure_ascii=False)
    return obj.model_dump_json() if obj is not None else "null"


def _parse(invoice: dict, agent: str) -> Invoice | str:
    """Fail safe: an invoice we cannot read becomes a blocking finding, never an empty (= clean) result."""
    try:
        return Invoice(**invoice)
    except ValidationError as e:
        return _dump([Finding(code="unreadable_invoice", severity=Severity.blocking, agent=agent,
                              message=f"Facture illisible (données extraites invalides), contrôle impossible : {e}")])


@server.tool(structured_output=False)
def find_vendor(siret: str) -> str:
    """Look up a vendor in master data by SIRET (14 digits)."""
    return _dump(next((v for v in VENDORS if v.siret == _norm(siret)), None))


@server.tool(structured_output=False)
def get_vendor(vendor_id: str) -> str:
    """Get a vendor's master data (name, email, verified IBAN) by vendor id, e.g. V001."""
    return _dump(next((v for v in VENDORS if v.id == vendor_id.strip()), None))


@server.tool(structured_output=False)
def get_purchase_order(number: str) -> str:
    """Get a purchase order by number, e.g. PO-2026-0412."""
    return _dump(POS.get(number.strip()))


@server.tool(structured_output=False)
def get_receipts(po_number: str) -> str:
    """Get goods receipts (bons de livraison) recorded against a purchase order."""
    return _dump([r for r in RECEIPTS if r.po_number == po_number.strip()])


@server.tool(structured_output=False)
def list_invoices_history(vendor_id: str) -> str:
    """Invoices from this vendor already recorded or paid."""
    return json.dumps([h for h in ERP["invoice_history"] if h["vendor_id"] == vendor_id], ensure_ascii=False)


@server.tool(structured_output=False)
def check_three_way_match(invoice: dict) -> str:
    """Compare an extracted invoice with its purchase order and goods receipts. Returns findings."""
    inv = _parse(invoice, "matcher")
    if isinstance(inv, str):
        return inv
    findings: list[Finding] = []

    def add(code, sev, msg):
        findings.append(Finding(code=code, severity=sev, message=msg, agent="matcher"))

    po = POS.get(inv.po_number or "")
    if po is None:
        add("no_po", Severity.warning, f"Aucun bon de commande trouvé pour la référence {inv.po_number!r}.")
        return _dump(findings)

    po_lines = {l.sku: l for l in po.lines}
    received: dict[str, Decimal] = {}
    for r in RECEIPTS:
        if r.po_number == po.number:
            for l in r.lines:
                received[l.sku] = received.get(l.sku, Decimal(0)) + l.quantity_received

    for line in inv.lines:
        pl = po_lines.get(line.sku or "")
        if pl is None:
            add("unknown_item", Severity.warning, f"La ligne {line.sku or line.description!r} ne figure pas sur {po.number}.")
            continue
        drift = (line.unit_price - pl.unit_price) / pl.unit_price
        if abs(drift) > TOLERANCE:
            add("price_mismatch", Severity.warning,
                f"{line.sku} : facturé {_eur(line.unit_price)} au lieu de {_eur(pl.unit_price)} prévus sur {po.number} ({drift:+.1%}).")
        got = received.get(line.sku, Decimal(0))
        if line.quantity > got:
            add("qty_mismatch", Severity.warning,
                f"{line.sku} : {line.quantity} facturés, {got} reçus.")

    lines_ht = sum((l.quantity * l.unit_price for l in inv.lines), Decimal(0))
    if abs(lines_ht - inv.total_ht) > Decimal("0.01") or abs(inv.total_ht + inv.total_vat - inv.total_ttc) > Decimal("0.01"):
        add("totals_mismatch", Severity.warning, f"La somme des lignes ({_eur(lines_ht)}) ne correspond pas aux totaux de la facture.")
    return _dump(findings)


@server.tool(structured_output=False)
def check_vendor_and_duplicates(invoice: dict) -> str:
    """Check the seller identity and bank details against master data, and look for duplicate invoices."""
    inv = _parse(invoice, "validator")
    if isinstance(inv, str):
        return inv
    findings: list[Finding] = []

    def add(code, sev, msg):
        findings.append(Finding(code=code, severity=sev, message=msg, agent="validator"))

    vendor = next((v for v in VENDORS if v.siret == _norm(inv.seller_siret)), None)
    if vendor is None:
        add("unknown_vendor", Severity.blocking, f"Le SIRET {inv.seller_siret} est inconnu du fichier fournisseurs.")
        return _dump(findings)
    if _norm(inv.seller_vat) != vendor.vat_number:
        add("vat_mismatch", Severity.blocking, f"Le n° de TVA {inv.seller_vat} diffère du fichier fournisseurs ({vendor.vat_number}).")
    if _norm(inv.seller_iban) != _norm(vendor.iban):
        add("iban_change", Severity.blocking,
            f"L'IBAN de la facture ({inv.seller_iban}) diffère de l'IBAN vérifié du fournisseur. "
            "Risque de fraude au changement de RIB : confirmer par téléphone avec un contact connu avant tout paiement.")
    for h in ERP["invoice_history"]:
        if h["vendor_id"] != vendor.id:
            continue
        same_number = _norm(h["number"]) == _norm(inv.number)
        same_amount = Decimal(h["total_ttc"]) == inv.total_ttc and h.get("po_number") == inv.po_number
        if same_number or same_amount:
            add("duplicate", Severity.blocking,
                f"Doublon de la facture {h['number']}, déjà payée le {h.get('paid_on', '?')}.")
    return _dump(findings)


@server.tool(structured_output=False)
def queue_payment(invoice_number: str, vendor_id: str, amount_ttc: str) -> str:
    """Put an invoice in the payment queue as PENDING human approval. Never pays directly."""
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "payment_queue.json"
    queue = json.loads(path.read_text()) if path.exists() else []
    if any(q["invoice_number"] == invoice_number and q["vendor_id"] == vendor_id for q in queue):
        return f"{invoice_number} is already in the payment queue"
    vendor = next((v for v in VENDORS if v.id == vendor_id), None)
    if vendor is None:
        return f"error: unknown vendor {vendor_id}"
    queue.append({"invoice_number": invoice_number, "vendor_id": vendor_id, "amount_ttc": amount_ttc,
                  "iban": vendor.iban, "status": "pending_approval",
                  "queued_at": datetime.now(timezone.utc).isoformat()})
    path.write_text(json.dumps(queue, ensure_ascii=False, indent=2))
    return f"{invoice_number} queued for payment, pending human approval"


if __name__ == "__main__":
    server.run()

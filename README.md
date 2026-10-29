# invoice-agents

Multi-agent system that processes supplier invoices end to end, built on MCP (agents to tools) and A2A (agents to agents). Designed for French/EU accounts payable: SIRET, intra-EU VAT numbers, IBAN, Factur-X style invoices.

## What it does

PDF invoices arrive in an inbox. For each one, the agents:

1. Extract the invoice data (number, supplier, lines, totals, VAT, IBAN, PO number).
2. Three-way match it against the purchase order and the delivery receipt.
3. Check the supplier: known SIRET, VAT number, verified IBAN, duplicate invoices.
4. Decide: **approve** (queue the payment), **hold** (draft an email to the supplier) or **escalate** (draft an alert to finance).

A human approves every payment and reviews every email draft on the dashboard. Nothing is paid or sent automatically.

## Agents

| Agent | Role | Tools (MCP) |
|---|---|---|
| Coordinator | Delegates each step over A2A, applies the decision policy | none |
| Extractor | Reads the PDF into structured data | `inbox.read_invoice` |
| Matcher | Invoice vs purchase order vs receipts | `erp.check_three_way_match`, `erp.get_purchase_order`, `erp.get_receipts` |
| Validator | Supplier identity, bank details, duplicates | `erp.find_vendor`, `erp.check_vendor_and_duplicates`, `erp.list_invoices_history` |
| Communicator | Queues approved payments, drafts emails | `erp.get_vendor`, `erp.queue_payment`, `mail.draft_email` |

Each agent sees only its own tools.

## Checks

Checks are deterministic code, so every finding is reproducible and auditable. The model extracts, orchestrates and writes; it does not judge.

| Finding | Risk it covers | Severity |
|---|---|---|
| `iban_change` | Bank-detail fraud | blocking |
| `unknown_vendor`, `vat_mismatch` | Fake supplier invoices | blocking |
| `duplicate` | Paying the same invoice twice (same number, or same amount and PO) | blocking |
| `price_mismatch` | Price above the PO, beyond a 2% tolerance | warning |
| `qty_mismatch` | More invoiced than received | warning |
| `unknown_item`, `no_po` | Lines or invoices without a purchase order | warning |
| `totals_mismatch` | Lines, VAT and totals that don't add up | warning |

Policy: any blocking finding escalates, any warning holds, otherwise approve.

## Safety

- Human approval for every payment and every outgoing email.
- Guardrails verify agents' actual tool calls, not their claims, and block forbidden actions.
- Tool-call limits per task, timeouts and a circuit breaker between agents.
- Every agent step is written to an audit log.
- Unreadable or invalid data fails safe (blocking finding, never an empty result).

## Model

Any OpenAI-compatible API, set in `config/agents.yaml` or through environment variables: DeepSeek, Mistral, Qwen, or a self-hosted model. Data in this repository is fictional.

## Results

12 test invoices (clean, overcharge, short delivery, duplicate, IBAN change, unknown supplier, no PO, wrong totals, two VAT rates, prompt injection in the PDF), 3 runs on `deepseek-chat`: 36/36 correct decisions and findings, no payment queued for an invoice that should not be paid, about 20 s per invoice with all 12 processed in parallel.

## Run

Requires Python 3.11+.

```
pip install -e .
cp .env.example .env                      # set LLM_API_KEY (LLM_BASE_URL / LLM_MODEL optional)
python scripts/make_demo_data.py          # optional: regenerate data/
python -m ap_agents.serve                 # terminal 1: start the agents
python -m ap_agents.dashboard             # terminal 2: http://127.0.0.1:8080
python -m ap_agents.run --runs 3          # or: process the inbox and score against data/expected.json
python scripts/make_replay.py             # build site/, a static replay of a real run
```

# A2A invoice agents (AgenticP)

Multi-agent system that processes supplier invoices end to end for French/EU businesses. Built on MCP (agents to tools) and A2A (agents to agents). Reference: `Design Multi-Agent AI Systems Using MCP and A2A` (ch. 7 to 10).

## Goal
A working demo of one business process, run end to end, for outreach to France/EU companies.

## Demo scenario
A batch of French supplier invoices (Factur-X style: SIRET, TVA, PO number) lands in an inbox. Agents extract them, match each against its purchase order and delivery receipt, catch a duplicate and an overcharge, and queue clean invoices for payment. Agents draft the emails (supplier, finance); a human reviews and sends them, and approves payments, on a dashboard.

## Agents (A2A)
- Coordinator: receives the batch, plans, delegates, aggregates.
- Extractor: reads invoices into structured data.
- Matcher: three-way match invoice / PO / receipt.
- Validator: duplicates, price or bank-detail changes, anomalies.
- Communicator: approval requests and supplier emails.

## Tools (MCP, mocked data)
- `erp`: vendors, POs, receipts, payment queue.
- `inbox`: incoming invoices.
- `mail`: email drafts only; a human sends them from the dashboard (demo: never really sent).
Each agent only gets the tools it needs.

## Rules
- Model-agnostic: develop on DeepSeek API; the model is a config value so clients can run Mistral, EU-hosted DeepSeek, Qwen or any other.
- EU AI Act and GDPR in mind: fake data only in the demo, every agent step logged for audit, no payment and no email without human approval.
- Safety: iteration limits, circuit breakers, strict tool schemas, structured logs.
- Python. Code stays local. Keep docs minimal: this file is the plan.

## Steps
1. Design: agents, tools, data model, sample invoices.
2. Skeleton: MCP servers and A2A agents running the happy path.
3. Safety: approval gate, limits, audit log, dashboard.
4. Run end to end with realistic scenarios and measure.

## Run
```
pip install -e .
python scripts/make_demo_data.py          # regenerate data/ (optional)
cp .env.example .env                      # add LLM_API_KEY; LLM_BASE_URL / LLM_MODEL override config
python -m ap_agents.serve                 # terminal 1: all agents
python -m ap_agents.run                   # terminal 2: process the inbox, score vs data/expected.json
python -m ap_agents.dashboard             # or instead: http://127.0.0.1:8080, run + approve payments
```
Guardrails (`src/ap_agents/guards.py`) check agents' real tool calls, not their claims. `LLM_BASE_URL=mock` runs a scripted stand-in (wiring test only, no model). Runtime output goes to `data/state/` (audit log, payment queue, email drafts).

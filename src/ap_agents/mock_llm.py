"""Scripted stand-in for the LLM, to test the MCP/A2A wiring offline (model.base_url: mock).

It follows each agent's prompt mechanically. It is not a model and is never used in the demo.
"""

import asyncio
import json
import os
import random
import re
from types import SimpleNamespace
from uuid import uuid4

from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageFunctionToolCall


def _call(name: str, args: dict) -> ChatCompletionMessage:
    tc = ChatCompletionMessageFunctionToolCall(id=uuid4().hex, type="function",
                                               function={"name": name, "arguments": json.dumps(args)})
    return ChatCompletionMessage(role="assistant", content=None, tool_calls=[tc])


def _answer(obj: dict) -> ChatCompletionMessage:
    return ChatCompletionMessage(role="assistant", content=json.dumps(obj, ensure_ascii=False))


def _num(s: str) -> str:
    return s.replace("€", "").replace(" ", "").replace(" ", "").replace(",", ".").strip()


def _extract(text: str, file: str) -> dict:
    def grab(pattern):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else None

    def iso(d):
        dd, mm, yy = d.split("/")
        return f"{yy}-{mm}-{dd}"

    lines = []
    for m in re.finditer(r"^\s+([A-Z]{2,}-\S+)\s{2,}(.+?)\s{2,}(\d+)\s+([\d  ]+,\d{2}) €", text, re.M):
        lines.append({"sku": m.group(1), "description": m.group(2).strip(), "quantity": m.group(3),
                      "unit_price": _num(m.group(4)), "vat_rate": "0.20"})
    return {
        "number": grab(r"N° (\S+-\S+)"), "issue_date": iso(grab(r"Date : (\S+)")),
        "due_date": iso(grab(r"Échéance : (\S+)")), "seller_name": re.split(r"\s{2,}", text.strip().splitlines()[0])[0],
        "seller_siret": grab(r"SIRET : (\d{14})"), "seller_vat": grab(r"TVA intracom\. : (\S+)"),
        "seller_iban": grab(r"IBAN : (.+)"), "buyer_name": "Atelier Moreau SAS" if "Atelier Moreau SAS" in text else None,
        "buyer_siret": re.findall(r"SIRET : (\d{14})", text)[1], "po_number": grab(r"Votre commande : (\S+)"),
        "lines": lines, "total_ht": _num(grab(r"Total HT[ \t]+(.+€)")), "total_vat": str(round(sum(float(_num(v)) for v in re.findall(r"TVA [\d,]+ %[ \t]+(.+€)", text)), 2)),
        "total_ttc": _num(grab(r"Total TTC[ \t]+(.+€)")), "source_file": file,
    }


def _findings(text: str) -> list:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return data if isinstance(data, list) else data.get("findings", [])


def _step(agent: str, user: str, results: list[str], retried: bool) -> ChatCompletionMessage:
    n = len(results)
    if agent == "extractor":
        file = user.split()[-1]
        return _call("inbox__read_invoice", {"file": file}) if n == 0 else _answer(_extract(results[0], file))
    if agent == "matcher":
        return _call("erp__check_three_way_match", {"invoice": json.loads(user)}) if n == 0 else \
            _answer({"findings": _findings(results[0]), "summary": "mock"})
    if agent == "validator":
        inv = json.loads(user)
        if n == 0:
            return _call("erp__find_vendor", {"siret": inv["seller_siret"]})
        if n == 1:
            return _call("erp__check_vendor_and_duplicates", {"invoice": inv})
        vendor = json.loads(results[0])
        return _answer({"vendor_id": vendor and vendor["id"], "findings": _findings(results[1]), "summary": "mock"})
    if agent == "communicator":
        d = json.loads(user)
        inv = d["invoice"]
        if d["decision"] == "approve":
            if n == 0:
                return _call("erp__queue_payment", {"invoice_number": inv["number"], "vendor_id": d["vendor_id"],
                                                    "amount_ttc": inv["total_ttc"]})
            if n == 1:
                return _call("mail__draft_email", {"to": "compta@atelier-moreau.example",
                                                  "subject": f"À valider : {inv['number']}", "body": "mock"})
        elif d["decision"] == "hold":
            if n == 0:
                return _call("erp__get_vendor", {"vendor_id": d["vendor_id"]})
            if n == 1:
                return _call("mail__draft_email", {"to": json.loads(results[0])["email"],
                                                  "subject": f"Facture {inv['number']}", "body": "mock"})
        elif os.environ.get("AP_MOCK_FAULT") == "skip_escalation_email" and not retried:
            return _answer({"actions": ["escalated to finance (claimed, not done)"]})
        elif n == 0:
            return _call("mail__draft_email", {"to": "compta@atelier-moreau.example",
                                              "subject": f"ALERTE {inv['number']}", "body": "mock"})
        return _answer({"actions": results})
    if agent == "coordinator":
        file = user.split()[-1]
        if n == 0:
            return _call("ask_extractor", {"message": f"Extract invoice file: {file}"})
        inv = json.loads(results[0])
        if n == 1:
            return _call("ask_matcher", {"message": results[0]})
        if n == 2:
            return _call("ask_validator", {"message": results[0]})
        validation = json.loads(results[2])
        findings = _findings(results[1]) + validation["findings"]
        sev = {f["severity"] for f in findings}
        decision = "escalate" if "blocking" in sev else "hold" if "warning" in sev else "approve"
        if n == 3:
            return _call("ask_communicator", {"message": json.dumps(
                {"decision": decision, "invoice": inv, "vendor_id": validation["vendor_id"], "findings": findings})})
        return _answer({"invoice_number": inv["number"], "vendor_id": validation["vendor_id"], "decision": decision,
                        "amount_ttc": inv["total_ttc"], "findings": findings,
                        "actions": json.loads(results[3]).get("actions", [])})
    raise ValueError(agent)


class MockLLM:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, model, messages, tools=None, **_):
        await asyncio.sleep(random.uniform(0.5, 1.5) * float(os.environ.get("AP_MOCK_DELAY", 0)))  # mimic model latency
        agent = re.search(r"You are the (\w+) agent", messages[0]["content"]).group(1)
        results = [m["content"] for m in messages if m["role"] == "tool"]
        retried = sum(m["role"] == "user" for m in messages) > 1
        msg = _step(agent, messages[1]["content"], results, retried)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

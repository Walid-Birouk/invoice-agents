"""Trigger: send every invoice in the inbox to the coordinator over A2A, and measure the run against data/expected.json.

Start the agents first: python -m ap_agents.serve
python -m ap_agents.run [--runs N] [file ...]
Measures per invoice: decision, findings, extraction (number, vendor, amount), latency, LLM calls and tokens,
tool calls, guardrail interventions; and checks that no payment was queued for an invoice that should not be paid.
"""

import argparse
import asyncio
import json
import os
import statistics
import time
from decimal import Decimal

from ap_agents.agent import agent_url, send_a2a
from ap_agents.config import DATA, STATE, load_config


async def process(url: str, file: str, timeout_s: float) -> dict:
    text = await send_a2a(url, f"Process invoice file: {file}", timeout_s)
    try:
        return {"file": file, **json.loads(text[text.find("{"):text.rfind("}") + 1])}
    except ValueError:
        return {"file": file, "error": text}


async def _timed(url: str, file: str, timeout_s: float) -> dict:
    started = time.monotonic()
    try:
        outcome = await process(url, file, timeout_s)
    except Exception as e:  # a crashed invoice is a measured failure, not the end of the run
        outcome = {"file": file, "error": f"{type(e).__name__}: {e}"}
    return outcome | {"seconds": time.monotonic() - started}


def _audit_lines() -> list[dict]:
    path = STATE / "audit.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def _score(o: dict, e: dict, events: list[dict]) -> dict:
    mine = [ev for ev in events if ev.get("ref") == o["file"]]
    amount = o.get("amount_ttc")
    return {
        "file": o["file"], "seconds": o["seconds"], "error": o.get("error"),
        "decision": o.get("decision"), "codes": sorted({f["code"] for f in o.get("findings", [])}),
        "decision_ok": o.get("decision") == e["decision"],
        "findings_ok": sorted({f["code"] for f in o.get("findings", [])}) == sorted(e["finding_codes"]),
        "extraction_ok": o.get("invoice_number") == e["invoice_number"] and o.get("vendor_id") == e["vendor_id"]
                         and amount is not None and Decimal(str(amount)) == Decimal(e["total_ttc"]),
        "llm_calls": sum(ev["kind"] == "llm" for ev in mine),
        "tokens_in": sum(ev.get("tokens_in") or 0 for ev in mine if ev["kind"] == "llm"),
        "tokens_out": sum(ev.get("tokens_out") or 0 for ev in mine if ev["kind"] == "llm"),
        "tool_calls": sum(ev["kind"] == "tool_call" for ev in mine),
        "guard": sum(ev["kind"] in ("guard_block", "guard_fail") for ev in mine),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=1, help="repeat the batch to measure consistency")
    parser.add_argument("files", nargs="*", help="only these inbox files (default: all)")
    args = parser.parse_args()

    cfg = load_config()
    url = agent_url(cfg, "coordinator")
    expected = {e["file"]: e for e in json.loads((DATA / "expected.json").read_text())}
    files = args.files or sorted(p.name for p in (DATA / "inbox").glob("*.pdf"))
    timeout = 2 * cfg["limits"]["a2a_timeout_s"]
    scores, walls = [], []

    for run in range(1, args.runs + 1):
        seen = len(_audit_lines())
        started = time.monotonic()
        outcomes = await asyncio.gather(*(_timed(url, f, timeout) for f in files))
        walls.append(time.monotonic() - started)
        events = _audit_lines()[seen:]
        batch = [_score(o, expected[o["file"]], events) for o in outcomes]
        scores += batch
        print(f"\nRun {run}/{args.runs}: {len(files)} invoices in {walls[-1]:.0f} s")
        for s in batch:
            marks = "".join("." if s[k] else "x" for k in ("decision_ok", "findings_ok", "extraction_ok"))
            print(f"  {s['file']:<36} {s['decision'] or 'ERROR':<9} {marks}  {s['seconds']:5.1f} s  "
                  f"{s['tokens_in'] + s['tokens_out']:>6} tok  {s['codes'] or s['error'] or ''}")

    # Safety: every queued payment must be for an invoice expected to be approved, and still wait for a human.
    payable = {e["invoice_number"] for e in expected.values() if e["decision"] == "approve"}
    queue = json.loads((STATE / "payment_queue.json").read_text()) if (STATE / "payment_queue.json").exists() else []
    unsafe = [q["invoice_number"] for q in queue if q["invoice_number"] not in payable]

    n = len(scores)
    secs = sorted(s["seconds"] for s in scores)

    def rate(key: str) -> str:
        ok = sum(s[key] for s in scores)
        return f"{ok}/{n} ({ok / n:.0%})"

    model = "mock" if os.environ.get("LLM_BASE_URL") == "mock" else os.environ.get("LLM_MODEL", cfg["model"]["name"])
    print(f"\n=== {n} invoice runs ({len(files)} invoices x {args.runs}), model {model} ===")
    print(f"decision correct      {rate('decision_ok')}")
    print(f"findings correct      {rate('findings_ok')}")
    print(f"extraction correct    {rate('extraction_ok')}  (number, vendor, amount TTC)")
    print(f"errors                {sum(bool(s['error']) for s in scores)}")
    print(f"latency per invoice   median {statistics.median(secs):.1f} s, max {secs[-1]:.1f} s")
    print(f"batch wall time       median {statistics.median(walls):.0f} s ({len(files)} invoices in parallel)")
    print(f"per invoice           {statistics.mean(s['llm_calls'] for s in scores):.1f} LLM calls, "
          f"{statistics.mean(s['tool_calls'] for s in scores):.1f} tool calls, "
          f"{statistics.mean(s['tokens_in'] for s in scores):,.0f} tokens in / "
          f"{statistics.mean(s['tokens_out'] for s in scores):,.0f} out")
    print(f"guardrail corrections {sum(s['guard'] for s in scores)} (blocked tool calls + rejected answers)")
    print(f"payments queued       {len(queue)}, all pending human approval: "
          f"{all(q['status'] == 'pending_approval' for q in queue)}; for non-payable invoices: {unsafe or 'none'}")


if __name__ == "__main__":
    asyncio.run(main())

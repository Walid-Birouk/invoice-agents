"""Finance team dashboard: run the inbox, watch the agents live, approve or reject payments, review email drafts.

python -m ap_agents.dashboard   (agents must be running: python -m ap_agents.serve)
Then open http://127.0.0.1:8080
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

from ap_agents.agent import agent_url, audit
from ap_agents.config import DATA, STATE, load_config
from ap_agents.run import process

CFG = load_config()
INBOX = DATA / "inbox"
VENDORS = json.loads((DATA / "erp.json").read_text())["vendors"]
status: dict[str, dict] = {}  # file -> {"state": waiting|processing|done, "outcome": {...}, "started", "finished"}


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _files() -> list[str]:
    return sorted(p.name for p in INBOX.glob("*.pdf"))


async def _process(file: str) -> None:
    started = time.time()
    status[file] = {"state": "processing", "started": started}
    outcome = await process(agent_url(CFG, "coordinator"), file, 2 * CFG["limits"]["a2a_timeout_s"])
    status[file] = {"state": "done", "outcome": outcome, "started": started, "finished": time.time()}


async def run_inbox(request: Request) -> JSONResponse:
    """Analyse every invoice, or only the one named in the path (/api/run/<file>)."""
    only = request.path_params.get("file")
    if only and only not in _files():
        return JSONResponse({"error": "unknown invoice"}, status_code=404)
    files = [f for f in ([only] if only else _files()) if status.get(f, {}).get("state") != "processing"]
    for f in files:
        asyncio.create_task(_process(f))
    return JSONResponse({"started": files})


async def reset(request: Request) -> JSONResponse:
    if any(s.get("state") == "processing" for s in status.values()):
        return JSONResponse({"error": "processing in progress"}, status_code=409)
    status.clear()
    for name in ("audit.jsonl", "emails.json", "payment_queue.json"):
        (STATE / name).unlink(missing_ok=True)
    return JSONResponse({"ok": True})


async def decide_payment(request: Request) -> JSONResponse:
    """The human approval gate: only here does a queued payment become approved."""
    number, action = request.path_params["number"], request.path_params["action"]
    if action not in ("approve", "reject"):
        return JSONResponse({"error": "unknown action"}, status_code=400)
    path = STATE / "payment_queue.json"
    queue = _read_json(path, [])
    for q in queue:
        if q["invoice_number"] == number and q["status"] == "pending_approval":
            q["status"] = "approved" if action == "approve" else "rejected"
            q["decided_by"] = "Responsable comptable (démo)"
            q["decided_at"] = datetime.now(timezone.utc).isoformat()
            path.write_text(json.dumps(queue, ensure_ascii=False, indent=2))
            audit("human", f"payment_{q['status']}", {"invoice": number, "amount_ttc": q["amount_ttc"]})
            return JSONResponse(q)
    return JSONResponse({"error": "no pending payment for this invoice"}, status_code=404)


async def decide_email(request: Request) -> JSONResponse:
    """Agents only draft emails: a human edits, sends or discards each one here. Demo mode never really sends."""
    email_id, action = int(request.path_params["id"]), request.path_params["action"]
    if action not in ("send", "discard"):
        return JSONResponse({"error": "unknown action"}, status_code=400)
    edits = await request.json() if action == "send" and await request.body() else {}
    path = STATE / "emails.json"
    emails = _read_json(path, [])
    for m in emails:
        if m["id"] == email_id and m["status"] == "draft":
            edited = any(k in edits and edits[k] != m[k] for k in ("subject", "body"))
            m.update({k: str(edits[k]) for k in ("subject", "body") if k in edits})
            m["status"] = "sent" if action == "send" else "discarded"
            m["decided_by"] = "Responsable comptable (démo)"
            m["decided_at"] = datetime.now(timezone.utc).isoformat()
            path.write_text(json.dumps(emails, ensure_ascii=False, indent=2))
            audit("human", f"email_{m['status']}", {"to": m["to"], "subject": m["subject"], "edited": edited})
            return JSONResponse(m)
    return JSONResponse({"error": "no draft with this id"}, status_code=404)


async def state(request: Request) -> JSONResponse:
    since = int(request.query_params.get("since", 0))
    lines = (STATE / "audit.jsonl").read_text().splitlines() if (STATE / "audit.jsonl").exists() else []
    return JSONResponse({
        "invoices": [{"file": f, **status.get(f, {"state": "waiting"})} for f in _files()],
        "events": [json.loads(l) for l in lines[since:]], "next": len(lines),
        "payments": _read_json(STATE / "payment_queue.json", []),
        "emails": _read_json(STATE / "emails.json", []),
        "model": "simulation (mock)" if os.environ.get("LLM_BASE_URL") == "mock"
                 else os.environ.get("LLM_MODEL", CFG["model"]["name"]),
        "vendors": {v["id"]: {"name": v["name"], "email": v["email"]} for v in VENDORS},
        "agents": list(CFG["agents"]),
        "now": time.time(),
    })


async def invoice_file(request: Request):
    path = (INBOX / request.path_params["file"]).resolve()
    if path.parent != INBOX.resolve() or not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)


async def index(request: Request) -> HTMLResponse:
    return HTMLResponse((Path(__file__).parent / "dashboard.html").read_text())


app = Starlette(routes=[
    Route("/", index),
    Route("/api/state", state),
    Route("/api/run", run_inbox, methods=["POST"]),
    Route("/api/run/{file}", run_inbox, methods=["POST"]),
    Route("/api/reset", reset, methods=["POST"]),
    Route("/api/payments/{number}/{action}", decide_payment, methods=["POST"]),
    Route("/api/emails/{id:int}/{action}", decide_email, methods=["POST"]),
    Route("/inbox/{file}", invoice_file),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")

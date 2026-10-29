"""Record one real run and build site/: the dashboard as a static page that replays it (no server, no API key).

python scripts/make_replay.py      (agents running: python -m ap_agents.serve)
Then publish site/ on any static host (GitHub Pages, Cloudflare Pages...).
"""

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ap_agents.agent import agent_url  # noqa: E402
from ap_agents.config import DATA, ROOT, STATE, load_config  # noqa: E402
from ap_agents.run import process  # noqa: E402

SITE = ROOT / "site"

# Stands in for the dashboard's /api/*: each invoice replays its recorded timeline from the moment it is started,
# and the human actions (approve, send, discard) work in the visitor's browser.
SHIM = r"""
<script>
(() => {
  const R = __RECORDING__, ms = s => new Date(s).getTime();
  let runs = [], mails = {}, pays = {}, human = [];
  const answerAt = f => ms(R.outcomes[f].at) - ms(R.started[f]);
  const reveal = (run, at) => run.start + ms(at) - ms(R.started[run.file]);
  function visible(now) {
    const ev = runs.flatMap(run => R.events.filter(e => e.ref === run.file)
      .map(e => ({ ...e, at: new Date(reveal(run, e.at)).toISOString() })));
    return ev.concat(human).filter(e => ms(e.at) <= now).sort((a, b) => ms(a.at) - ms(b.at));
  }
  function state(since) {
    const now = Date.now(), ev = visible(now), last = {};
    runs.forEach(run => last[run.file] = run);
    const emails = runs.flatMap(run => R.emails.filter(m => m.file === run.file && reveal(run, m.at) <= now)
      .map(m => ({ ...m, id: `${run.n}-${m.id}`, at: new Date(reveal(run, m.at)).toISOString(), ...(mails[`${run.n}-${m.id}`] || {}) })));
    const payments = Object.values(R.payments).filter(p => last[p.file] && reveal(last[p.file], p.queued_at) <= now)
      .map(p => ({ ...p, ...(pays[p.invoice_number] || {}) }));
    return {
      invoices: R.files.map(f => {
        const run = last[f];
        if (!run) return { file: f, state: "waiting" };
        const end = run.start + answerAt(f);
        return end <= now ? { file: f, state: "done", outcome: R.outcomes[f].outcome, started: run.start / 1000, finished: end / 1000 }
                          : { file: f, state: "processing", started: run.start / 1000 };
      }),
      events: ev.slice(since), next: ev.length, payments, emails, model: R.model, vendors: R.vendors, agents: R.agents, now: now / 1000,
    };
  }
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const decided = () => ({ decided_by: "Vous (démo)", decided_at: new Date().toISOString() });
  const log = (kind, data) => human.push({ at: new Date().toISOString(), agent: "human", kind, ref: null, ...data });
  const realFetch = window.fetch;
  window.fetch = async (url, opts = {}) => {
    const u = new URL(url, location.href), p = u.pathname.split("/");
    const i = p.indexOf("api");
    if (i < 0) return realFetch(url, opts);
    const [what, a, b] = p.slice(i + 1).map(decodeURIComponent);
    const busy = f => { const r = runs.filter(r => r.file === f).pop(); return r && r.start + answerAt(f) > Date.now(); };
    if (what === "state") return json(state(Number(u.searchParams.get("since") || 0)));
    if (what === "run") {
      const files = (a ? [a] : R.files).filter(f => !busy(f));
      files.forEach(f => runs.push({ file: f, start: Date.now(), n: runs.length + 1 }));
      return json({ started: files });
    }
    if (what === "reset") {
      if (R.files.some(busy)) return json({ error: "processing in progress" }, 409);
      runs = []; mails = {}; pays = {}; human = [];
      return json({ ok: true });
    }
    if (what === "payments") {
      const pay = state(0).payments.find(x => x.invoice_number === a && x.status === "pending_approval");
      if (!pay) return json({ error: "no pending payment" }, 404);
      pays[a] = { status: b === "approve" ? "approved" : "rejected", ...decided() };
      log(`payment_${pays[a].status}`, { invoice: a, amount_ttc: pay.amount_ttc });
      return json({ ...pay, ...pays[a] });
    }
    if (what === "emails") {
      const m = state(0).emails.find(x => x.id === a && x.status === "draft");
      if (!m) return json({ error: "no draft" }, 404);
      const edits = b === "send" && opts.body ? JSON.parse(opts.body) : {};
      mails[a] = { ...edits, status: b === "send" ? "sent" : "discarded", ...decided() };
      log(`email_${mails[a].status}`, { to: m.to, subject: edits.subject || m.subject });
      return json({ ...m, ...mails[a] });
    }
    return json({ error: "not found" }, 404);
  };
})();
</script>
"""


async def record(files: list[str]) -> dict:
    cfg = load_config()
    shutil.rmtree(STATE, ignore_errors=True)
    url, timeout = agent_url(cfg, "coordinator"), 2 * cfg["limits"]["a2a_timeout_s"]

    async def one(f):
        outcome = await process(url, f, timeout)
        return f, {"outcome": outcome, "at": datetime.now().astimezone().isoformat()}

    outcomes = dict(await asyncio.gather(*(one(f) for f in files)))
    events = [json.loads(l) for l in (STATE / "audit.jsonl").read_text().splitlines()]
    started = {f: next(e["at"] for e in events if e["ref"] == f and e["agent"] == "coordinator" and e["kind"] == "task")
               for f in files}
    numbers = {o["outcome"].get("invoice_number"): f for f, o in outcomes.items()}

    def file_of(text: str) -> str | None:
        return next((f for n, f in numbers.items() if n and n in text), None)

    emails = json.loads((STATE / "emails.json").read_text()) if (STATE / "emails.json").exists() else []
    queue = json.loads((STATE / "payment_queue.json").read_text()) if (STATE / "payment_queue.json").exists() else []
    erp = json.loads((DATA / "erp.json").read_text())
    return {
        "files": files, "started": started, "outcomes": outcomes,
        "events": [e for e in events if e["kind"] != "llm"],  # token counts aren't shown; keeps the page small
        "emails": [m | {"file": file_of(m["subject"] + m["body"])} for m in emails],
        "payments": {p["invoice_number"]: p | {"file": numbers.get(p["invoice_number"])} for p in queue},
        "model": f"rediffusion d'une exécution réelle · {os.environ.get('LLM_MODEL', cfg['model']['name'])}",
        "vendors": {v["id"]: {"name": v["name"], "email": v["email"]} for v in erp["vendors"]},
        "agents": list(cfg["agents"]),
    }


def build(rec: dict) -> None:
    shutil.rmtree(SITE, ignore_errors=True)
    (SITE / "inbox").mkdir(parents=True)
    for f in rec["files"]:
        shutil.copy(DATA / "inbox" / f, SITE / "inbox" / f)
    html = (ROOT / "src/ap_agents/dashboard.html").read_text()
    html = html.replace('href="/inbox/', 'href="inbox/')  # relative, so the site works under any sub-path
    shim = SHIM.replace("__RECORDING__", json.dumps(rec, ensure_ascii=False).replace("</", "<\\/"))
    html = html.replace("<script>", shim + "<script>", 1)
    (SITE / "index.html").write_text(html)


if __name__ == "__main__":
    files = sorted(p.name for p in (DATA / "inbox").glob("*.pdf"))
    rec = asyncio.run(record(files))
    build(rec)
    bad = [f for f, o in rec["outcomes"].items() if "decision" not in o["outcome"]]
    print(f"site/ built from a run of {len(files)} invoices ({len(bad)} errors: {bad or 'none'})")

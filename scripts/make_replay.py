"""Record one real run and build docs/: the dashboard as a static page that replays it (no server, no API key).

python scripts/make_replay.py            (agents running: python -m ap_agents.serve)
python scripts/make_replay.py --rebuild  (reuse the run already in docs/, e.g. after a dashboard change)
GitHub Pages serves docs/ directly (Settings > Pages > main /docs); any static host works.
"""

import asyncio
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ap_agents.agent import agent_url  # noqa: E402
from ap_agents.config import DATA, ROOT, STATE, load_config  # noqa: E402
from ap_agents.run import process  # noqa: E402

SITE = ROOT / "docs"

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

INTRO = """
<style>
  .intro { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 16px;
           background: rgba(0,0,0,.38); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); }
  .intro[hidden] { display: none; }
  .intro .sheet { background: var(--surface); color: var(--text); border-radius: 22px; max-width: 560px; width: 100%;
                  max-height: calc(100vh - 32px); overflow: auto; padding: 32px 32px 26px; box-shadow: 0 30px 80px rgba(0,0,0,.3); }
  .intro .kicker { font-size: 13px; font-weight: 600; color: var(--accent); margin: 0 0 8px; }
  .intro h2 { font-size: 28px; line-height: 1.15; letter-spacing: -.025em; margin: 0 0 12px; }
  .intro p { font-size: 15px; line-height: 1.5; color: var(--secondary); margin: 0 0 18px; }
  .intro ul { list-style: none; padding: 0; margin: 0 0 20px; display: grid; gap: 10px; }
  .intro li { display: grid; grid-template-columns: 22px 1fr; gap: 10px; font-size: 14.5px; line-height: 1.45; }
  .intro li b { color: var(--text); font-weight: 600; }
  .intro li i { font-style: normal; color: var(--green); font-weight: 700; }
  .intro .fine { font-size: 12.5px; color: var(--tertiary); margin: 0 0 22px; }
  .intro .actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .intro .actions .btn { flex: 1; min-width: 160px; padding: 12px 18px; font-size: 15px; }
  .help { position: fixed; right: 20px; bottom: 20px; z-index: 40; width: 40px; height: 40px; border-radius: 50%;
          border: 1px solid var(--hairline); background: var(--surface); color: var(--secondary); font-size: 17px; font-weight: 600;
          box-shadow: 0 6px 20px rgba(0,0,0,.12); cursor: pointer; }
  @media (max-width: 560px) { .intro .sheet { padding: 24px 20px 20px; } .intro h2 { font-size: 23px; } }
</style>
<div class="intro" id="intro" role="dialog" aria-modal="true" aria-labelledby="intro-title">
  <div class="sheet">
    <div class="kicker">Démo · comptabilité fournisseurs</div>
    <h2 id="intro-title">Vos factures fournisseurs contrôlées en une vingtaine de secondes.</h2>
    <p>Cinq agents IA traitent 12 factures comme le ferait votre service comptable : ils lisent le PDF, le rapprochent du bon de commande
       et du bon de livraison, vérifient le fournisseur, puis préparent le paiement ou le courrier.</p>
    <ul>
      <li><i>✓</i><span><b>Fraude au changement de RIB</b> : l'IBAN de la facture ne correspond pas au fournisseur, le paiement est bloqué.</span></li>
      <li><i>✓</i><span><b>Doublons et surfacturations</b> : relance déjà payée, prix au-dessus de la commande, quantité non livrée.</span></li>
      <li><i>✓</i><span><b>Faux fournisseurs</b> : SIRET inconnu, n° de TVA incohérent, et même une facture qui tente de manipuler l'IA.</span></li>
      <li><i>✓</i><span><b>Vous gardez la main</b> : aucun paiement ni email ne part sans votre validation, chaque étape est tracée.</span></li>
    </ul>
    <p class="fine">Rediffusion d'une exécution réelle. Données fictives. Conçu pour le RGPD et l'AI Act : modèle au choix (Mistral, Qwen, DeepSeek hébergé en UE), journal d'audit complet.</p>
    <div class="actions">
      <button class="btn primary" id="intro-go">Lancer l'analyse</button>
      <button class="btn" id="intro-close">Explorer d'abord</button>
    </div>
  </div>
</div>
<button class="help" id="help" title="À propos de cette démo" aria-label="À propos de cette démo">?</button>
<script>
(() => {
  const box = document.getElementById("intro");
  const close = () => box.hidden = true;
  document.getElementById("intro-close").onclick = close;
  document.getElementById("intro-go").onclick = () => { close(); document.getElementById("run").click(); };
  document.getElementById("help").onclick = () => box.hidden = false;
  box.onclick = e => { if (e.target === box) close(); };
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !box.hidden) { e.stopImmediatePropagation(); close(); } }, true);
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
    html = html.replace("<script>", shim + "<script>", 1).replace("</body>", INTRO + "</body>", 1)
    (SITE / "index.html").write_text(html)


def recorded() -> dict:
    """The run embedded in the page already built."""
    page = (SITE / "index.html").read_text()
    return json.loads(re.search(r"const R = (\{.*?\}), ms =", page, re.S).group(1).replace("<\\/", "</"))


if __name__ == "__main__":
    files = sorted(p.name for p in (DATA / "inbox").glob("*.pdf"))
    rec = recorded() if "--rebuild" in sys.argv else asyncio.run(record(files))
    build(rec)
    bad = [f for f, o in rec["outcomes"].items() if "decision" not in o["outcome"]]
    print(f"docs/ built from {'the existing' if '--rebuild' in sys.argv else 'a new'} run of {len(files)} invoices ({len(bad)} errors: {bad or 'none'})")

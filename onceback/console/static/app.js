"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ZERO_DEC = new Set(["JPY", "KRW"]);
const money = (n, cur) => n == null ? "—" : Number(n).toLocaleString("en-GB", {minimumFractionDigits: ZERO_DEC.has(cur) ? 0 : 2, maximumFractionDigits: ZERO_DEC.has(cur) ? 0 : 2});
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("en-GB", {hour12: false});

const ACTION = {
  REFUND_CARD: "Card refund", PAYOUT_BANK: "Bank payout", REQUEST_BANK_DETAILS: "Bank payout",
  ACCEPT_DISPUTE: "Accept chargeback", CHALLENGE_DISPUTE: "Challenge chargeback", DECLINE: "Decline", ESCALATE: "Escalate",
};
const OUTCOME = {
  REFUNDED_TO_CARD: ["Refunded to card", "money"], REFUNDED_TO_BANK: ["Paid to bank account", "money"],
  ACCEPT_DISPUTE: ["Chargeback accepted · no second refund", "money"], CHALLENGE_DISPUTE: ["Chargeback challenged with refund receipt", "money"],
  DECLINE: ["Declined", "neutral"], ESCALATE: ["Escalated to a person", "wait"], AWAITING_BANK_DETAILS: ["Waiting for bank details", "wait"],
  REJECTED_BY_APPROVER: ["Rejected by approver · nothing moved", "risk"], BLOCKED_DUPLICATE: ["Blocked duplicate", "risk"],
  PAYOUT_IN_FLIGHT: ["Payout in flight", "wait"], AWAITING_APPROVAL: ["Not approved · nothing moved", "risk"],
};
const STAGE = {GOAL: "Request", READ: "Read message", CHECK: "Check", PLAN: "Policy decision", NEW_INFO: "New information",
  REVISED: "Revised plan", APPROVAL: "Approval", ACT: "Airwallex", RESULT: "Result", GUARD: "Guard"};
const MATCH = {FULL_MATCH: ["Full match", "money"], PARTIAL_MATCH: ["Partial match", "wait"], NOT_MATCHED: ["Not matched", "risk"],
  CANNOT_VERIFY: ["No name check in this corridor", "wait"]};

let S = null, selected = null, detail = null;
const painted = {};
function paint(id, html) {
  if (painted[id] === html) return;
  painted[id] = html;
  const el = document.getElementById(id);
  if (el) el.innerHTML = html;
}

async function api(path, opts = {}) {
  const r = await fetch(path, {headers: {"content-type": "application/json"}, ...opts});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || `${r.status} ${path}`);
  return j;
}
const post = (path, body) => api(path, {method: "POST", body: body ? JSON.stringify(body) : undefined});

function statusPill(c, short = false) {
  if (c.status === "running") return `<span class="pill run">Agent working…</span>`;
  if (c.status === "awaiting_approval") return `<span class="pill wait">Needs approval</span>`;
  if (c.status === "error") return `<span class="pill risk">Error</span>`;
  if (c.status === "new") return `<span class="pill">New</span>`;
  let [label, tone] = OUTCOME[c.outcome] || [c.outcome, "neutral"];
  if (short) label = label.split(" · ")[0].replace(" with refund receipt", "").replace(" account", "");
  return `<span class="pill ${tone === "neutral" ? "" : tone}">${esc(label)}</span>`;
}

function renderTop() {
  paint("env", `<span class="tag ${S.mode === "live" ? "live" : ""}">${S.mode === "live" ? "Airwallex sandbox · live" : "offline mirror"}</span>`
    + (S.tag ? `<span class="tag">${esc(S.tag)}</span>` : "") + `<span>today ${esc(S.today)}</span>`);
  paint("balances", Object.entries(S.balances || {}).slice(0, 5)
    .map(([k, v]) => `<span><b>${esc(k)}</b>${money(v, k)}</span>`).join(""));
  const st = S.setup;
  let h;
  if (st.status === "idle") h = `<button class="btn" id="btn-setup">Prepare demo orders</button>`;
  else if (st.status === "running") h = `<span class="hint">Paying orders with test card ${st.done}/${st.total}…</span><span class="bar"><i style="width:${100 * st.done / st.total}%"></i></span>`;
  else if (st.status === "ready") h = `<span class="pill money">${st.total} orders paid</span>`;
  else h = `<span class="pill risk" title="${esc(st.error)}">setup failed</span><button class="btn ghost" id="btn-setup">Retry</button>`;
  paint("setup", h);
}

function renderInbox() {
  const cases = S.cases;
  paint("inbox-count", `${cases.length}`);
  paint("inbox", cases.map((c) => `
    <button class="item ${c.kind === "chargeback" ? "cb" : ""} ${c.id === selected ? "sel" : ""}" data-case="${esc(c.id)}">
      <div class="row"><span class="id">${esc(c.id)}</span><span class="who">${esc(c.customer)}</span><span class="amt">${money(c.amount, c.currency)} ${esc(c.currency)}</span></div>
      <div class="msg">${c.kind === "chargeback" ? `Chargeback ${c.dispute_id ? `<span class="mono">${esc(c.dispute_id)}</span>` : ""} on ${esc(c.order_id)}` : esc(c.message)}</div>
      <div class="meta">${c.kind === "chargeback" ? `<span class="src">arrived by ${esc(c.source)}</span>` : `<span class="mono">${esc(c.order_id)}</span>`}<span style="margin-left:auto">${statusPill(c, true)}</span></div>
    </button>`).join(""));
}

const INJ_RE = /(SYSTEM\s*(OVERRIDE|PROMPT|MESSAGE)[^.\n]*\.?|ignore (all|any|previous|prior) (instructions|rules)[^.\n]*\.?|\b(the )?(assistant|agent|AI|bot)\b[^.\n]{0,40}\b(must|should|shall)\b[^.\n]*\.?)/gi;

function quote(c, reading) {
  let txt = esc(c.message);
  for (const cite of (reading && reading.citations) || []) {
    const e = esc(cite);
    if (e && txt.includes(e)) txt = txt.replace(e, `<mark>${e}</mark>`);
  }
  if (reading && reading.injection) txt = txt.replace(INJ_RE, (m) => `<span class="inj">${m}</span>`);
  const att = (c.attachments || []).map((a) => `📎 ${esc(a)}`).join(" · ");
  return `<div class="quote">${txt}${reading && reading.injection ? `<span class="flag">instruction injection · treated as data</span>` : ""}${att ? `<span class="att">${att}</span>` : ""}</div>`;
}

function facts(steps) {
  const f = {};
  for (const s of steps) {
    const d = s.data || {};
    if (d.kind === "reading") f.reading = d;
    if (d.kind === "dispute_check" && !f.dispute) f.check = (f.check || 0) + 1;
    if (d.kind === "dispute") f.dispute = d;
    if (d.kind === "name_check") f.name = d;
    if (d.kind === "challenge") { f.evidence = true; f.refunded = d.refunded; }
    if ((s.stage === "PLAN" || s.stage === "REVISED") && d.action) { f.plan = f.plan || d; f.last = d; if (d.refunded != null) f.refunded = d.refunded; }
  }
  return f;
}

function strip(c, f) {
  if (c.kind === "chargeback") {
    const ds = f.dispute || {}, resp = f.last ? (ACTION[f.last.action] || f.last.action) : "—";
    return `<div class="strip">
      <div class="cell"><div class="k">Response</div><div class="v">${esc(resp)}</div><div class="s">decided by code from the ledger</div></div>
      <div class="cell"><div class="k">Chargeback</div><div class="v risk">${ds.amount != null ? money(ds.amount, ds.currency) + " " + esc(ds.currency) : "—"}</div><div class="s">${esc((ds.reason || "").replaceAll("_", " ").toLowerCase())} ${ds.code ? "(" + esc(ds.code) + ")" : ""}</div></div>
      <div class="cell"><div class="k">Already refunded</div><div class="v money">${f.refunded != null ? money(f.refunded, ds.currency) + " " + esc(ds.currency) : "—"}</div><div class="s">${f.refunded ? "partial refund · not auto-defended by Airwallex" : ""}</div></div>
      <div class="cell"><div class="k">Evidence</div><div class="v">${f.evidence ? "Refund receipt" : "—"}</div><div class="s">${f.evidence ? "uploaded to File Service" : ""}</div></div>
    </div>`;
  }
  const last = f.last, plan = f.plan, r = f.reading;
  const chan = last ? (ACTION[last.action] || last.action) : "—";
  const chanSub = f.name ? `name check: ${esc((MATCH[f.name.match || f.name.code] || [f.name.match || f.name.code])[0])}` :
    last && last.action === "REFUND_CARD" ? "original card" : last && last.action === "REQUEST_BANK_DETAILS" ? "card rail closed" : "";
  const cb = f.dispute ? `<div class="v risk">Chargeback open</div><div class="s">${esc(f.dispute.dispute_id)} · ${esc((f.dispute.reason || "").replaceAll("_", " ").toLowerCase())}</div>`
    : f.check ? `<div class="v money">Clear</div><div class="s">checked ${f.check}× before money moved</div>` : `<div class="v">—</div><div class="s"></div>`;
  const safe = r ? (r.injection ? `<div class="v risk">Injection flagged</div><div class="s">instructions ignored · person must approve</div>`
    : `<div class="v money">Clean</div><div class="s">${esc(r.source)} · ${esc(r.intent.replace("_", " "))}</div>`) : `<div class="v">—</div><div class="s"></div>`;
  const amt = plan && plan.amount ? `<div class="v">${money(plan.amount, plan.currency)} ${esc(plan.currency)}</div>` : `<div class="v">—</div>`;
  const asked = r && r.claimed_amount != null ? `<div class="s">customer asked <span class="strike">${money(r.claimed_amount, c.currency)}</span></div>` : `<div class="s">computed by policy, not the model</div>`;
  return `<div class="strip">
    <div class="cell"><div class="k">Channel</div><div class="v">${esc(chan)}</div><div class="s">${chanSub}</div></div>
    <div class="cell"><div class="k">Chargeback check</div>${cb}</div>
    <div class="cell"><div class="k">Message safety</div>${safe}</div>
    <div class="cell"><div class="k">Policy amount</div>${amt}${asked}</div>
  </div>`;
}

function stepText(s) {
  const d = s.data || {};
  let h = esc(s.text);
  if (d.kind === "reading") {
    h = `${esc(d.source)} read the message: <b>${esc(d.intent.replace("_", " "))}</b>${d.evidence && d.evidence.length ? ` · evidence ${esc(d.evidence.join(", "))}` : ""}${d.injection ? ` · <b class="flag">injection flagged</b>` : ""}`;
  } else if ((s.stage === "PLAN" || s.stage === "REVISED") && d.action) {
    h = `<b>${esc(ACTION[d.action] || d.action)}</b>${d.amount ? ` ${money(d.amount, d.currency)} ${esc(d.currency)}` : ""}${d.needs_approval ? " · needs a person" : ""}<span class="why">${(d.reasons || []).map(esc).join(" · ")}</span>`;
  } else if (d.kind === "name_check") {
    const [m, tone] = MATCH[d.match || d.code] || [d.match || d.code, ""];
    h = `Account check ${esc(d.country)} <span class="mono">${esc(d.account)}</span>: <span class="pill ${tone}">${esc(m)}</span> · bank holder ${esc(d.bank_holder || "—")}, buyer ${esc(d.buyer)}`;
  } else if (d.kind === "dispute") {
    h = `Chargeback <span class="mono">${esc(d.dispute_id)}</span> · <b>${money(d.amount, d.currency)} ${esc(d.currency)}</b> · ${esc((d.reason || "").replaceAll("_", " ").toLowerCase())} (${esc(d.code)}) · stage ${esc(d.stage_name)}`;
  } else if (d.kind === "request") {
    h = `Waiting for a person to approve <b>${money(d.binding.amount, d.binding.currency)} ${esc(d.binding.currency)}</b> via ${esc(ACTION[d.binding.rail] || d.binding.rail)} <span class="mono">binding ${esc(d.hash)}</span>`;
  } else if (d.kind === "answer") {
    h = d.approved ? `Approved <span class="mono">${esc(d.hash)}</span>` : `Not approved · nothing moved`;
  }
  return h;
}

function renderCase() {
  const el = document.getElementById("case");
  if (!detail) return;
  const c = detail, f = facts(c.steps || []);
  const head = `<div class="case-head">
      <div><h1>${esc(c.id)} · ${esc(c.customer)}</h1>
        <div class="facts"><span>order <b>${esc(c.order_id)}</b></span><span>paid <b>${money(c.amount, c.currency)} ${esc(c.currency)}</b> on <b>${esc(c.paid_on)}</b></span><span>${esc(c.country)}</span><span class="mono">${esc(c.payment_intent_id)}</span></div></div>
      <div class="acts">${statusPill(c)}
        ${c.kind === "request" && c.status === "new" ? `<button class="btn" id="btn-run" ${S.setup.status !== "ready" ? "disabled" : ""}>Let the agent work this case</button>` : ""}
        ${c.kind === "request" && c.status === "done" && ["REFUNDED_TO_CARD", "REFUNDED_TO_BANK"].includes(c.outcome) && !S.cases.some((x) => x.id === c.order_id + "-chargeback")
          ? `<button class="btn ghost" id="btn-cb" title="sandbox simulation">Simulate a chargeback on ${esc(c.order_id)}</button>` : ""}
      </div></div>`;
  const msg = c.kind === "request" ? quote(c, f.reading) : "";
  const steps = (c.steps || []).map((s, i, a) => `<div class="step ${c.status === "running" && i === a.length - 1 ? "live" : ""}">
      <span class="t">${clock(s.at)}</span><span class="g ${esc(s.stage)}">${esc(STAGE[s.stage] || s.stage)}</span><span class="x">${stepText(s)}</span></div>`).join("");
  const tl = `<div class="tl"><h3>Timeline</h3>${steps || `<p class="none">Nothing yet. The agent has not looked at this case.</p>`}${c.error ? `<p class="pill risk">${esc(c.error)}</p>` : ""}</div>`;
  const html = head + msg + (c.steps && c.steps.length ? strip(c, f) : "") + tl;
  if (painted.case !== html) { painted.case = html; el.innerHTML = html; }
}

function approvalCard(p, focus) {
  const b = p.binding || {};
  const name = b.name_check ? (MATCH[b.name_check] || [b.name_check])[0] : null;
  return `<div class="appr ${focus ? "focus" : ""}" data-appr="${esc(p.case_id)}">
    <div class="hd"><b>${esc(p.case_id)} · ${esc(ACTION[p.action] || p.action)}</b><span class="mono">${esc(b.order_id)}</span></div>
    <div class="big">${money(p.amount, p.currency)}<span>${esc(p.currency)}</span></div>
    <dl><dt>Rail</dt><dd>${esc(ACTION[b.rail] || b.rail)}</dd>
      <dt>Payment</dt><dd class="mono">${esc(b.payment_intent_id)}</dd>
      ${b.beneficiary ? `<dt>Beneficiary</dt><dd>${esc(b.beneficiary)}</dd>` : ""}
      ${name ? `<dt>Name check</dt><dd><span class="pill ${(MATCH[b.name_check] || [0, ""])[1]}">${esc(name)}</span></dd>` : ""}
      ${b.beneficiary_account ? `<dt>Account id</dt><dd class="mono">${esc(b.beneficiary_account)}</dd>` : ""}</dl>
    <div class="why"><ul>${(p.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>
    <div class="hash mono">binding ${esc(p.hash)} · approval covers exactly these terms</div>
    <div class="btns"><button class="no" data-act="reject" data-case="${esc(p.case_id)}" data-hash="${esc(p.hash)}">Reject</button><button class="ok" data-act="approve" data-case="${esc(p.case_id)}" data-hash="${esc(p.hash)}">Approve</button></div>
  </div>`;
}

function renderApprovals() {
  const list = S.approvals || [];
  paint("appr-count", list.length ? `${list.length}` : "");
  paint("approvals", list.length ? list.map((p) => approvalCard(p, p.case_id === selected)).join("") : `<p class="none">Nothing waiting. Cards appear here when the policy needs a person.</p>`);
}

function renderLedger() {
  paint("ledger", !S.ledger.length ? `<p class="none">No payments yet.</p>` :
    `<div class="legend"><span><i style="background:var(--money)"></i>refunded</span><span><i style="background:var(--risk)"></i>chargeback</span><span><i style="background:var(--wait)"></i>in flight</span></div>` +
    S.ledger.map((l) => {
      const pct = (v) => l.captured ? Math.min(100, 100 * v / l.captured).toFixed(1) : 0;
      return `<div class="led"><div class="o">${esc(l.order_id)}<span>${esc(l.customer)}</span></div>
        <div class="n">${money(l.returned, l.currency)} / ${money(l.captured, l.currency)} ${esc(l.currency)}</div>
        <div class="track"><div class="r" style="width:${pct(l.refunded)}%"></div><div class="c" style="width:${pct(l.chargeback)}%"></div><div class="f" style="width:${pct(l.in_flight)}%"></div></div>
        <div class="sub"><span class="mono">${esc(l.payment_intent_id)}</span><span>${l.returned <= l.captured + 0.005 ? "never more than captured" : "OVER-RETURNED"}</span></div></div>`;
    }).join(""));
}

function renderActivity() {
  paint("wh-hint", S.webhook_configured ? "webhook verified · polling fallback on" : "polling (no webhook secret)");
  paint("activity", S.activity.map((a) => `<div class="ev"><span class="t">${clock(a.at)}</span><span class="k ${esc(a.kind)}">${esc(a.kind)}</span><span class="x">${esc(a.text)}</span></div>`).join(""));
}

function alertErr(e) { console.error(e); const el = document.createElement("div"); el.textContent = e.message; Object.assign(el.style, {position: "fixed", bottom: "16px", left: "50%", transform: "translateX(-50%)", background: "#17191c", color: "#fff", padding: "8px 14px", borderRadius: "6px"}); document.body.appendChild(el); setTimeout(() => el.remove(), 4000); }

document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.act) {
      b.disabled = true;
      await post(`/api/approvals/${b.dataset.case}`, {hash: b.dataset.hash, decision: b.dataset.act, approver: "operator"});
    } else if (b.id === "btn-setup") {
      b.disabled = true; await post("/api/setup");
    } else if (b.id === "btn-run" && selected) {
      b.disabled = true; await post(`/api/cases/${selected}/run`);
    } else if (b.id === "btn-cb" && detail) {
      b.disabled = true; await post(`/api/orders/${detail.order_id}/chargeback`);
    } else if (b.dataset.case) {
      await select(b.dataset.case);
      return;
    } else return;
    await tick();
  } catch (e) { b.disabled = false; alertErr(e); }
});

async function select(id) { selected = id; painted.case = null; detail = await api(`/api/cases/${id}`); renderInbox(); renderCase(); history.replaceState(null, "", "#" + id); }

async function tick() {
  try {
    S = await api("/api/state");
    renderTop(); renderInbox(); renderApprovals(); renderLedger(); renderActivity();
    if (selected) { detail = await api(`/api/cases/${selected}`); renderCase(); }
  } catch (e) { console.error(e); }
}
setInterval(tick, 1000);
tick().then(() => { const h = location.hash.slice(1); if (h && S.cases.some((c) => c.id === h)) select(h); });

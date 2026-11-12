/* WINTER front end: iPad-style lock screen + dashboard. No data is embedded in the page:
   everything comes from /api/data, which the Pi only answers with a token from the passcode.
   The token lives in memory only, so every visit / reload starts at the lock screen. */
(() => {
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
const S = { token:null, D:null, digits:4, entry:"", busy:false, waitUntil:0, last:Date.now(),
            autolock:10, poll:null, first:true, offset:0 };

/* ---------------- formatting ---------------- */
const money = (x, sign) => (x < 0 ? "−" : (sign && x > 0 ? "+" : "")) + "$" + Math.abs(x).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const cents = x => x == null ? "–" : (x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x*100).toFixed(1) + "¢";
const pct = x => x == null ? "–" : (x*100).toFixed(0) + "%";
const arrow = x => x > 0 ? '<span class="up">▲ ' : x < 0 ? '<span class="down">▼ ' : '<span>';
const hms = s => { s = Math.max(0, Math.floor(s)); const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(x).padStart(2,"0"); };
const verdictCls = v => ({"EDGE LIKELY":"good","LOSES":"bad","NOT PROVEN":"warn"}[v] || "mut");
const nowSrv = () => Date.now()/1000 + S.offset;

/* ---------------- API ---------------- */
async function api(path, opt = {}) {
  const headers = Object.assign({"Content-Type":"application/json"}, S.token ? {"X-Winter-Token": S.token} : {});
  const r = await fetch(path, Object.assign({cache:"no-store", headers}, opt));
  let body = null; try { body = await r.json(); } catch (e) {}
  return {status:r.status, body};
}

/* ================= LOCK SCREEN ================= */
const LETTERS = ["", "", "ABC", "DEF", "GHI", "JKL", "MNO", "PQRS", "TUV", "WXYZ"];
function buildKeys() {
  const k = $("keys"); k.innerHTML = "";
  [1,2,3,4,5,6,7,8,9,null,0,null].forEach(n => {
    const b = document.createElement("button");
    b.type = "button";
    if (n === null) { b.className = "key blank"; b.tabIndex = -1; b.setAttribute("aria-hidden","true"); }
    else {
      b.className = "key"; b.dataset.n = n; b.setAttribute("aria-label", String(n));
      b.innerHTML = `<b>${n}</b><small>${LETTERS[n] || "&nbsp;"}</small>`;
      b.addEventListener("pointerdown", e => { e.preventDefault(); press(String(n), b); });
    }
    k.appendChild(b);
  });
}
function drawDots(state) {
  const d = $("dots");
  if (d.children.length !== S.digits) d.innerHTML = "<i></i>".repeat(S.digits);
  [...d.children].forEach((x, i) => x.classList.toggle("on", i < S.entry.length));
  d.classList.toggle("ok", state === "ok");
  $("lkDel").hidden = S.entry.length === 0;
}
function flash(b) { if (!b) return; b.classList.add("pressed"); setTimeout(() => b.classList.remove("pressed"), 140); }
function press(n, btn) {
  if (S.busy || Date.now() < S.waitUntil || S.entry.length >= S.digits) return;
  flash(btn); S.entry += n; drawDots();
  if (navigator.vibrate) navigator.vibrate(8);
  if (S.entry.length === S.digits) setTimeout(submit, 120);
}
function del() { if (S.busy) return; S.entry = S.entry.slice(0, -1); drawDots(); }
function lockMsg(t) { $("lkMsg").innerHTML = t || "&nbsp;"; }

async function submit() {
  S.busy = true;
  let res;
  try { res = await api("/api/unlock", {method:"POST", body:JSON.stringify({code:S.entry})}); }
  catch (e) { S.busy = false; S.entry = ""; drawDots(); lockMsg("Winter is not reachable"); return; }
  const b = res.body || {};
  if (res.status === 200 && b.token) {
    S.token = b.token; S.autolock = b.autolock_min || 10;
    drawDots("ok"); lockMsg("&nbsp;");
    if (navigator.vibrate) navigator.vibrate([10, 40, 10]);
    const got = await refresh(true);
    setTimeout(() => openApp(got), 260);
    return;
  }
  S.entry = "";
  const dots = $("dots"); dots.classList.remove("shake"); void dots.offsetWidth; dots.classList.add("shake");
  if (navigator.vibrate) navigator.vibrate([40, 30, 40]);
  setTimeout(() => { drawDots(); S.busy = false; }, 500);
  if (b.wait > 0) startLockout(b.wait, b.attempts);
  else { const left = 6 - (b.attempts || 0); lockMsg(left <= 3 && left > 0 ? `Wrong passcode · ${left} ${left === 1 ? "try" : "tries"} before Winter locks` : "Wrong passcode"); }
}
function startLockout(sec, attempts) {
  S.waitUntil = Date.now() + sec * 1000;
  $("keys").classList.add("disabled");
  $("lkTitle").textContent = "Winter is disabled";
  const tick = () => {
    const left = Math.ceil((S.waitUntil - Date.now()) / 1000);
    if (left <= 0) { $("keys").classList.remove("disabled"); $("lkTitle").textContent = "Enter Passcode"; lockMsg(); return; }
    const m = Math.floor(left / 60), s = left % 60;
    lockMsg(`try again in ${m ? m + " min " : ""}${s ? s + " s" : ""}`.trim() + (attempts ? ` · ${attempts} wrong attempts` : ""));
    setTimeout(tick, 1000);
  };
  tick();
}
function openApp(ok) {
  document.body.classList.add("unlocking");
  document.body.classList.remove("is-locked");
  $("app").setAttribute("aria-hidden", "false");
  if (ok) render(true);
  requestAnimationFrame(() => document.body.classList.add("shown"));
  setTimeout(() => { document.body.classList.remove("unlocking"); S.entry = ""; drawDots(); S.busy = false; }, 820);
  clearInterval(S.poll); S.poll = setInterval(() => refresh(false).then(ok2 => ok2 && render(false)), 60000);
  S.last = Date.now();
  snow.setDensity(0.45);
}
async function lock(reason) {
  const t = S.token; S.token = null; S.D = null;
  clearInterval(S.poll);
  if (t) fetch("/api/lock", {method:"POST", headers:{"X-Winter-Token":t}}).catch(() => {});
  document.body.classList.remove("shown", "unlocking");
  document.body.classList.add("is-locked");
  $("app").setAttribute("aria-hidden", "true");
  S.entry = ""; S.busy = false; drawDots(); lockMsg(reason || "");
  snow.setDensity(1);
  setTimeout(() => { if (!S.token) wipe(); }, 900);
}
function wipe() { ["books","cities","cap","open","log","decs","done","bt","dl","heroSpark"].forEach(id => { const e = $(id); if (e) e.innerHTML = ""; }); }

async function refresh(first) {
  if (!S.token) return false;
  try {
    const r = await api("/api/data");
    if (r.status === 401) { lock("Session ended"); return false; }
    if (r.status !== 200) return false;
    S.D = r.body; S.offset = r.body.now_ts - Date.now()/1000;
    return true;
  } catch (e) { return false; }
}

/* clocks */
function tickClocks() {
  const d = new Date();
  $("lkTime").textContent = d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", hour12:false});
  $("lkDate").textContent = d.toLocaleDateString([], {weekday:"long", day:"numeric", month:"long"});
  $("lsTime").textContent = d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", hour12:false});
  $("tbClock").textContent = "Berlin " + d.toLocaleTimeString("en-GB", {timeZone:"Europe/Berlin", hour:"2-digit", minute:"2-digit", second:"2-digit"});
  if (S.D && S.token) tickCountdowns();
}

/* auto-lock */
["pointerdown","keydown","wheel","touchstart","scroll"].forEach(ev => addEventListener(ev, () => { S.last = Date.now(); }, {passive:true}));
setInterval(() => { if (S.token && Date.now() - S.last > S.autolock * 60000) lock("Locked after inactivity"); }, 10000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && S.token && Date.now() - S.last > S.autolock * 60000) lock("Locked after inactivity");
  snow.pause(document.hidden);
});
addEventListener("keydown", e => {
  if (!document.body.classList.contains("is-locked")) return;
  if (/^[0-9]$/.test(e.key)) press(e.key, document.querySelector(`.key[data-n="${e.key}"]`));
  else if (e.key === "Backspace") del();
  else if (e.key === "Escape") { S.entry = ""; drawDots(); }
});

/* ================= TOOLTIP / TOAST ================= */
const tip = $("tip");
function showTip(html, x, y) {
  tip.innerHTML = html; tip.classList.add("on");
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let left = x + 14, top = y - h - 12;
  if (left + w > innerWidth - 8) left = x - w - 14;
  if (top < 8) top = y + 16;
  tip.style.left = left + "px"; tip.style.top = top + "px";
}
function hideTip() { tip.classList.remove("on"); }
function toast(t) { const e = $("toast"); e.textContent = t; e.classList.add("on"); clearTimeout(e._t); e._t = setTimeout(() => e.classList.remove("on"), 2200); }

/* ================= CHARTS ================= */
let gid = 0;
const NS = "http://www.w3.org/2000/svg";
function lineChart(el, pts, opt) {
  el.innerHTML = "";
  if (!pts || pts.length < 2) { el.innerHTML = `<div class="empty">${esc(opt.empty || "The curve appears after the first settled days.")}</div>`; return; }
  const W = Math.max(240, el.clientWidth || 300), H = opt.h || 120, L = opt.axes ? 46 : 4, R = 6, T = 8, B = opt.axes ? 20 : 6;
  const vals = pts.map(p => p.v).concat(opt.base != null ? [opt.base] : []);
  let mn = Math.min(...vals), mx = Math.max(...vals); const pad = (mx - mn) * 0.08 || 1; mn -= pad; mx += pad;
  const X = i => L + i * (W - L - R) / (pts.length - 1), Y = v => T + (mx - v) / (mx - mn) * (H - T - B);
  const color = opt.color || "var(--s1)", id = "g" + (++gid);
  const line = pts.map((p, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(p.v).toFixed(1)).join("");
  const area = line + `L${X(pts.length-1).toFixed(1)} ${H-B} L${X(0).toFixed(1)} ${H-B}Z`;
  let ax = "";
  if (opt.axes) {
    [mn + pad, (mn + mx) / 2, mx - pad].forEach(t => {
      ax += `<line x1="${L}" x2="${W-R}" y1="${Y(t)}" y2="${Y(t)}" stroke="rgba(170,205,255,.08)"/><text x="${L-6}" y="${Y(t)+3}" text-anchor="end">${t >= 1000 ? (t/1000).toFixed(2)+"k" : t.toFixed(0)}</text>`;
    });
    ax += `<text x="${L}" y="${H-4}">${esc(pts[0].d)}</text><text x="${W-R}" y="${H-4}" text-anchor="end">${esc(pts[pts.length-1].d)}</text>`;
  }
  const base = opt.base != null ? `<line x1="${L}" x2="${W-R}" y1="${Y(opt.base)}" y2="${Y(opt.base)}" stroke="rgba(170,205,255,.35)" stroke-dasharray="3 5"/>` : "";
  const anim = S.first && !reduce;
  el.innerHTML = `<div class="chart"><svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(opt.label || "equity curve")}">
    <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".28"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    ${ax}${base}<path d="${area}" fill="url(#${id})" class="${anim ? "fadein" : ""}"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" class="${anim ? "draw" : ""}"/>
    <line class="xh" y1="${T}" y2="${H-B}" stroke="rgba(234,242,255,.35)" stroke-width="1" visibility="hidden"/>
    <circle class="xd" r="4.5" fill="${color}" stroke="#0a1324" stroke-width="2" visibility="hidden"/>
    <rect x="${L}" y="0" width="${W-L-R}" height="${H}" fill="transparent" class="hit"/></svg></div>`;
  const svg = el.querySelector("svg"), p = svg.querySelector("path.draw");
  if (p) p.style.setProperty("--len", p.getTotalLength());
  const xh = svg.querySelector(".xh"), xd = svg.querySelector(".xd"), hit = svg.querySelector(".hit");
  const move = e => {
    const r = svg.getBoundingClientRect(), sx = (e.clientX - r.left) * W / r.width;
    const i = Math.max(0, Math.min(pts.length - 1, Math.round((sx - L) / ((W - L - R) / (pts.length - 1)))));
    xh.setAttribute("x1", X(i)); xh.setAttribute("x2", X(i)); xh.setAttribute("visibility", "visible");
    xd.setAttribute("cx", X(i)); xd.setAttribute("cy", Y(pts[i].v)); xd.setAttribute("visibility", "visible");
    const ch = opt.base != null ? pts[i].v - opt.base : null;
    showTip(`${esc(pts[i].d)}<br><b>${money(pts[i].v)}</b>${ch != null ? ` <span class="${ch >= 0 ? "up" : "down"}">${money(ch, true)}</span>` : ""}`, e.clientX, e.clientY);
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerdown", move);
  hit.addEventListener("pointerleave", () => { xh.setAttribute("visibility","hidden"); xd.setAttribute("visibility","hidden"); hideTip(); });
}
function ciCompare(f, b) {
  const vals = [0];
  [f, b].forEach(s => { if (s && s.ci) vals.push(...s.ci); if (s && s.mean != null) vals.push(s.mean); });
  const lo = Math.min(...vals) - .02, hi = Math.max(...vals) + .02, P = x => (x - lo) / (hi - lo) * 100;
  const row = (name, s, col) => s && s.n ? `<div class="row"><span>${name}</span><div class="track"><div class="z" style="left:${P(0)}%"></div>${s.ci ? `<div class="ci" style="left:${P(s.ci[0])}%;width:${P(s.ci[1]) - P(s.ci[0])}%;background:${col}"></div>` : ""}<div class="pt" style="left:${P(s.mean)}%;background:${col}"></div></div><span class="v">${cents(s.mean)}</span></div>`
                                         : `<div class="row"><span>${name}</span><div class="track"></div><span class="v faint">–</span></div>`;
  return `<div class="cmp"><div class="label">mean per contract · bar = 95% CI · line = 0</div>${row("forward", f, "var(--s1)")}${row("backtest", b, "var(--s2)")}
    <div class="legend"><span><i style="background:var(--s1)"></i>forward (live)</span><span><i style="background:var(--s2)"></i>backtest</span></div></div>`;
}
function monthBars(months) {
  if (!months || !months.length) return "";
  const mx = Math.max(0.01, ...months.map(m => Math.abs(m.mean)));
  return `<div class="label" style="margin-top:14px">mean per contract by month</div><div class="bars">${months.map((m, k) => {
    const h = Math.abs(m.mean) / mx * 100;
    return `<div class="col" data-tip="${esc(m.m)} · <b>${cents(m.mean)}</b> per contract · ${m.n} trades" style="--k:${k}">
      <div class="half pos">${m.mean > 0 ? `<div class="b" style="height:${h}%"></div>` : ""}</div>
      <div class="half neg">${m.mean < 0 ? `<div class="b" style="height:${h}%"></div>` : ""}</div>
      <span class="lbl">${esc(m.m.slice(5))}</span></div>`; }).join("")}</div>
    <div class="legend" style="margin-top:26px"><span><i style="background:var(--pos)"></i>profit</span><span><i style="background:var(--neg)"></i>loss</span></div>`;
}
document.addEventListener("pointermove", e => {
  const c = e.target.closest && e.target.closest("[data-tip]");
  if (c) showTip(c.dataset.tip, e.clientX, e.clientY);
  else if (!e.target.closest(".hit")) hideTip();
});

/* count-up */
function countUp(el, to, fmt) {
  if (!S.first || reduce) { el.innerHTML = fmt(to); return; }
  const from = to * 0.9, t0 = performance.now(), dur = 1300;
  const step = t => { const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 4);
    el.innerHTML = fmt(from + (to - from) * e); if (k < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}

/* ================= RENDER ================= */
function tbl(head, rows, right = [], scroll = false) {
  if (!rows.length) return `<div class="empty">Nothing yet.</div>`;
  return `<div class="tbl${scroll ? " scroll" : ""}"><table><thead><tr>${head.map((h, i) => `<th class="${right.includes(i) ? "r" : ""}">${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr>${r.map((c, i) => `<td class="${right.includes(i) ? "r m" : ""}">${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
const BOOKNAME = {taker:"Taker book", maker:"Maker book", demo:"Demo book"};
const BOOKCOL = {taker:"var(--s1)", maker:"var(--s2)", demo:"var(--s3)"};

function render(first) {
  const D = S.D; if (!D) return;
  S.first = !!first;
  const R = first ? " rise" : "";
  /* top bar */
  $("tbUpdated").textContent = "live · " + D.updated.slice(11);
  const tn = D.tunnel || {};
  $("tbTunnel").textContent = tn.url ? "🌐 " + tn.url.replace("https://", "") : tn.state === "missing" ? "tunnel off" : "tunnel " + (tn.state || "off");
  $("tbTunnel").dataset.url = tn.url || "";
  /* hero */
  const tk = D.fwd.taker || {n:0, pnl:0};
  countUp($("heroCap"), D.start + (tk.pnl || 0), v => money(v));
  $("heroDelta").innerHTML = `${arrow(tk.pnl)}${money(tk.pnl || 0, true)} (${((tk.pnl || 0) / D.start * 100).toFixed(1)}%)</span> <span class="muted small">realized</span>`;
  $("heroSub").innerHTML = tk.n ? `${tk.n} settled · ${tk.wins} wins vs ${tk.exp_wins} price-implied · ${tk.open} open · luck p ${tk.luck_p.toFixed(3)}`
                                : `${tk.open || 0} open · first results the morning after the first trade`;
  const n = tk.n || 0;
  $("ringN").textContent = n;
  requestAnimationFrame(() => { $("ringFg").style.strokeDashoffset = 314 * (1 - Math.min(1, n / 300)); });
  lineChart($("heroSpark"), tk.equity, {h:110, base:D.start, color:"var(--s1)", label:"Taker equity"});
  $("ruleTxt").innerHTML = `Day before, <b>${esc(D.rule.decision)} local</b>: buy YES on tomorrow's favorite if the ask is <b>${D.rule.min}–${D.rule.max}¢</b> and the spread <b>≤ ${D.rule.spread}¢</b>.
    Stake <b>${D.rule.stake}%</b> of the book (max <b>$${D.rule.max_stake}</b>), at most ${D.rule.max_daily}% per day. Taker fee ${D.rule.tfee}, maker fee ${D.rule.mfee}.`;
  /* books */
  $("books").innerHTML = Object.keys(D.fwd).map((k, i) => {
    const s = D.fwd[k], b = (D.bt && D.bt.books && D.bt.books[k]) || null;
    const fr = s.orders ? pct(s.filled_n / s.orders) : "–";
    return `<div class="glass card${R}" style="--i:${i + 3}">
      <div class="book-head"><span class="label" style="color:${BOOKCOL[k]}">● ${BOOKNAME[k] || k}</span><span class="tag ${verdictCls(s.verdict)}">${esc(s.verdict)}</span></div>
      ${k === "demo" ? `<div class="note">Real orders on the Kalshi DEMO exchange (fake money). Tests order handling, not the edge.${D.demo.balance != null ? " Demo balance " + money(D.demo.balance) + "." : ""}</div>` : ""}
      <div class="big">${money(D.start + (s.pnl || 0))}</div>
      <div class="book-chart" data-book="${k}"></div>
      <div class="kv"><span>Settled</span><span>${s.n || 0}${k === "maker" ? " · fill " + fr + (b && b.fill_rate != null ? " (bt " + pct(b.fill_rate) + ")" : "") : " · " + (s.open || 0) + " open"}</span>
        <span>Wins / price-implied</span><span>${s.n ? s.wins + " / " + s.exp_wins : "–"}</span>
        <span>Realized P&L</span><span>${arrow(s.pnl)}${money(s.pnl || 0, true)}</span></span>
        <span>Max drawdown</span><span>${s.n ? money(-s.mdd) : "–"}</span>
        <span>Fees</span><span>${s.n ? money(s.fees) : "–"}</span></div>
      ${ciCompare(s, b)}</div>`; }).join("");
  document.querySelectorAll(".book-chart").forEach(el => {
    const k = el.dataset.book; lineChart(el, D.fwd[k].equity, {h:70, base:D.start, color:BOOKCOL[k], label:BOOKNAME[k] + " equity", empty:"No settled trades yet."}); });
  /* cities */
  $("cities").innerHTML = D.sched.map((c, i) => `<div class="glass city${c.done ? " done" : ""}${R}" style="--i:${i + 6}" data-tz="${esc(c.tz)}" data-next="${c.next_ts}">
      ${c.done ? `<span class="badge up">✓</span>` : ""}<div class="cn">${esc(c.city)}</div>
      <div class="ct" data-clock>--:--</div>
      <div class="cs" data-status>${c.done ? "decided for " + esc(c.done) : ""}</div>
      <div class="bar"><i data-bar style="width:0"></i></div></div>`).join("");
  /* next decision */
  const nx = Math.min(...D.sched.map(c => c.next_ts));
  const who = D.sched.filter(c => c.next_ts === nx).map(c => c.city);
  $("nextCity").textContent = who.join(" · ");
  $("nextWhen").textContent = new Date(nx * 1000).toLocaleString("en-GB", {timeZone:"Europe/Berlin", weekday:"short", hour:"2-digit", minute:"2-digit"}) + " Berlin · 15:00 local";
  $("nextCount").dataset.ts = nx;
  tickCountdowns();
  /* capacity */
  const C = D.cap, f0 = x => x == null ? "–" : Math.round(x).toLocaleString("en-US");
  $("cap").innerHTML = !C || !C.n ? `<div class="glass card${R}"><div class="empty">Logged from the next TRADE decision on: the real YES ask ladder of every favorite, and how much of it the edge can absorb.</div></div>` :
    `<div class="grid two"><div class="glass card${R}"><div class="label">Per trade · median of ${C.n}</div><div class="kv">
      <span>Contracts at the ask</span><span>${f0(C.at_ask)}</span><span>Within +2¢</span><span>${f0(C.within_2c)}</span>
      <span>Absorbable (edge > 1¢)</span><span>${f0(C.cap_contracts)} · $${f0(C.cap_cost)}</span><span>Open interest</span><span>${f0(C.oi)}</span>
      <span>Your current size</span><span>~${f0(C.my_size)} contracts</span></div></div>
      <div class="glass card${R}"><div class="label">Per day at full capacity · ${C.days} days</div><div class="kv">
      <span>Capital at risk (avg)</span><span>$${f0(C.day_cost)}</span><span>Capital at risk (max day)</span><span>$${f0(C.day_cost_max)}</span>
      <span>Expected profit (avg)</span><span class="up">${money(C.day_profit)}</span><span>Assumed edge</span><span>${(C.edge * 100).toFixed(2)}¢ / contract</span></div>
      <div class="note">Upper bound: takes every visible contract while each one still carries more than 1¢ of edge. Real fills move the book.</div></div></div>`;
  /* positions, log */
  $("open").innerHTML = tbl(["Book","Target","City","Bracket","Status","Qty","Price"], D.open.map(r => [
    `<span style="color:${BOOKCOL[r.book] || "inherit"}">●</span> ${esc(r.book)}`, esc(r.date), esc(r.city), esc(r.bracket),
    r.status === "working" ? `<span class="pill" style="color:var(--warn)">bid until ${esc(r.expires)}</span>` : `<span class="pill" style="color:var(--ice)">filled</span>`,
    esc(r.contracts), (r.status === "working" ? (+r.limit * 100).toFixed(0) : (+r.price * 100).toFixed(1)) + "¢"]), [5, 6]);
  $("log").innerHTML = (D.log || []).map(l => { const m = l.match(/^(\S+ \S+) (.*)$/); if (!m) return esc(l);
    const hl = /TRADE|BUY|filled|PUBLIC|Settled/.test(m[2]); return `<span class="ts">${esc(m[1].slice(5, 16))}</span> ${hl ? `<span class="hl">${esc(m[2])}</span>` : esc(m[2])}`; }).join("\n") || "No activity yet.";
  /* decisions, done */
  $("decs").innerHTML = `<div class="label">Latest ${Math.min(40, D.decs.length)} decisions</div>` + tbl(["Decided","Target","City","Favorite","Bid","Ask","Action","Result"], D.decs.map(d => [
    esc(d.logged.slice(5)), esc(d.date), esc(d.city), esc(d.bracket), d.bid === "" ? "–" : (+d.bid * 100).toFixed(0) + "¢", d.ask === "" ? "–" : (+d.ask * 100).toFixed(0) + "¢",
    d.action === "TRADE" ? `<span class="pill" style="color:var(--ice)">TRADE</span>` : `<span class="muted">${esc(d.action)}</span>`,
    d.result ? (d.result === "yes" ? `<span class="up">✓ fav won</span>` : `<span class="down">✗ fav lost</span>`) : "–"]), [4, 5], true);
  $("done").innerHTML = `<div class="label">Latest ${Math.min(60, D.done.length)} settled</div>` + tbl(["Book","Target","City","Bracket","Qty","Price","Fee","Result","P&L"], D.done.map(r => [
    `<span style="color:${BOOKCOL[r.book] || "inherit"}">●</span> ${esc(r.book)}`, esc(r.date), esc(r.city), esc(r.bracket), esc(r.contracts), (+r.price * 100).toFixed(1) + "¢", "$" + (+r.fee).toFixed(2),
    r.win === "1" ? `<span class="up">✓ win</span>` : `<span class="down">✗ loss</span>`, `${arrow(+r.pnl)}${money(+r.pnl, true)}</span>`]), [4, 5, 6, 8], true);
  /* backtest */
  const B = D.bt;
  if (!B) { $("btMeta").innerHTML = ""; $("bt").innerHTML = `<div class="glass card"><div class="empty">No backtest yet. On the Pi: <span class="mono">./venv/bin/python winter.py backtest</span></div></div>`; }
  else {
    const acts = Object.entries(B.actions || {}).map(([k, v]) => `${esc(k)} ${v}`).join(" · ");
    $("btMeta").innerHTML = `${esc(B.start)} → ${esc(B.end)} · ${B.decisions} decisions (${acts}) · train &lt; ${esc(B.split)} ≤ test · generated ${esc(B.generated)}`;
    $("bt").innerHTML = ["taker", "maker"].map((k, i) => { const s = B.books[k];
      if (!s || !s.n) return `<div class="glass card${R}"><div class="label">${BOOKNAME[k]}</div><div class="empty">No fills.</div></div>`;
      return `<div class="glass card${R}" style="--i:${i}">
        <div class="book-head"><span class="label" style="color:${BOOKCOL[k]}">● ${BOOKNAME[k]} · backtest</span><span class="tag ${verdictCls(s.verdict)}">${esc(s.verdict)}</span></div>
        <div class="big">${arrow(s.pnl)}${money(s.pnl, true)}</span></div>
        <div class="bt-chart" data-book="${k}"></div>
        <div class="kv"><span>Trades${k === "maker" ? " (fill rate)" : ""}</span><span>${s.n}${k === "maker" && s.fill_rate != null ? " (" + pct(s.fill_rate) + ")" : ""}</span>
          <span>Wins / price-implied</span><span>${s.wins} / ${s.exp_wins}</span>
          <span>Mean per contract</span><span>${cents(s.mean)}</span>
          <span>95% CI (days resampled)</span><span>${s.ci ? cents(s.ci[0]) + " … " + cents(s.ci[1]) : "–"}</span>
          <span>t-stat</span><span>${s.t == null ? "–" : s.t.toFixed(2)}</span>
          <span>Train / test</span><span>${cents(s.train.mean)} / ${cents(s.test.mean)}</span>
          <span>Fees · max drawdown</span><span>${money(s.fees)} · ${money(-s.mdd)}</span></div>
        ${monthBars(s.months)}</div>`; }).join("");
    document.querySelectorAll(".bt-chart").forEach(el => { const k = el.dataset.book;
      lineChart(el, B.books[k].equity, {h:170, base:D.start, axes:true, color:BOOKCOL[k], label:BOOKNAME[k] + " backtest equity"}); });
  }
  /* downloads, footer */
  $("dl").innerHTML = ["forward_trades.csv","forward_decisions.csv","forward_depth.csv","backtest_trades.csv","backtest_decisions.csv"]
    .map(f => `<button type="button" class="dl" data-file="${f}">↓ ${f}</button>`).join("");
  $("foot").innerHTML = `${esc(D.version)} · auto-lock after ${D.autolock_min} min · data refreshes every minute`;
  S.first = false;
}

function tickCountdowns() {
  const now = nowSrv();
  const nc = $("nextCount"); if (nc.dataset.ts) nc.textContent = hms(+nc.dataset.ts - now);
  document.querySelectorAll(".city").forEach(c => {
    const tz = c.dataset.tz, nxt = +c.dataset.next;
    c.querySelector("[data-clock]").textContent = new Date().toLocaleTimeString("en-GB", {timeZone:tz, hour:"2-digit", minute:"2-digit"});
    const left = nxt - now;
    if (!c.classList.contains("done")) c.querySelector("[data-status]").textContent = left > 0 ? "decides in " + hms(left) : "deciding…";
    c.querySelector("[data-bar]").style.width = Math.max(0, Math.min(100, (1 - left / 86400) * 100)) + "%";
  });
}

/* downloads + tunnel copy */
document.addEventListener("click", async e => {
  const d = e.target.closest(".dl");
  if (d && S.token) {
    const r = await fetch("/api/file/" + d.dataset.file, {headers:{"X-Winter-Token":S.token}, cache:"no-store"});
    if (r.status !== 200) { toast(r.status === 404 ? "File not created yet" : "Download failed"); return; }
    const url = URL.createObjectURL(await r.blob()), a = document.createElement("a");
    a.href = url; a.download = d.dataset.file; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 2000);
  }
  if (e.target.closest("#tbTunnel")) {
    const u = $("tbTunnel").dataset.url; if (!u) return;
    try { await navigator.clipboard.writeText(u); toast("Public link copied"); } catch (err) { prompt("Public link", u); }
  }
});
$("lockBtn").addEventListener("click", () => lock(""));
$("lkDel").addEventListener("click", del);
let rz; addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { if (S.D && S.token) render(false); snow.resize(); }, 200); });

/* ================= SNOW ================= */
const snow = (() => {
  const cv = $("snow"), ctx = cv.getContext("2d"); let W, H, flakes = [], dens = 1, paused = false, dpr = Math.min(2, devicePixelRatio || 1);
  function resize() { W = innerWidth; H = innerHeight; cv.width = W * dpr; cv.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); fill(); }
  function make(top) { const r = Math.random() * 2.1 + .5; return {x:Math.random() * W, y:top ? -10 : Math.random() * H, r, v:r * .35 + .25, a:Math.random() * 6.28, o:Math.random() * .5 + .3}; }
  function fill() { const n = Math.min(240, Math.round(W * H / 8000 * dens)); while (flakes.length < n) flakes.push(make(false)); flakes.length = n; }
  function frame() {
    if (!paused) {
      ctx.clearRect(0, 0, W, H);
      for (const f of flakes) {
        f.y += f.v; f.a += .01; f.x += Math.sin(f.a) * .35;
        if (f.y > H + 5) Object.assign(f, make(true));
        ctx.globalAlpha = f.o; ctx.beginPath(); ctx.arc(f.x, f.y, f.r, 0, 6.283); ctx.fillStyle = "#eef6ff"; ctx.fill();
      }
    }
    requestAnimationFrame(frame);
  }
  if (!reduce) { resize(); requestAnimationFrame(frame); }
  return { setDensity(d) { dens = d; if (!reduce) fill(); }, pause(p) { paused = p; }, resize() { if (!reduce) resize(); } };
})();

/* ================= START ================= */
buildKeys(); drawDots(); tickClocks(); setInterval(tickClocks, 1000);
api("/api/status").then(r => {
  const b = r.body || {};
  if (b.digits) { S.digits = b.digits; drawDots(); }
  if (b.wait > 0) startLockout(b.wait, b.attempts);
}).catch(() => lockMsg("Winter is not reachable"));
})();

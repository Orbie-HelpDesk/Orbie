const $ = (id) => document.getElementById(id);
let filter = "";

// ---------- Header subtitle: the same page serves agents and admins ----------
(function setSubtitle() {
  const sub = $("dashSub");
  if (!sub) return;
  const role = window.aihasUser && window.aihasUser.role;
  sub.textContent = role === "admin" ? "Admin dashboard" : role === "agent" ? "Agent dashboard" : "Dashboard";
})();

// ---------- Connection status (same pill as the chat) ----------
fetch("/health")
  .then((res) => res.json())
  .then((data) => {
    $("led").classList.add("on");
    $("status").textContent =
      data.database === "connected" ? "Online · DB connected" : "Online · DB issue";
  })
  .catch(() => {
    $("status").textContent = "Offline";
  });

// ---------- Time ----------
// The API sends UTC with an offset (2026-11-21T03:13:40+00:00). The browser turns it into the
// viewer's local time. A string with no zone is read as UTC, never as local time.
function parseTime(value) {
  if (!value) return null;
  // Python sends microseconds. Some browsers only accept milliseconds, so cut the extra digits.
  const text = value.replace(/(\.\d{3})\d+/, "$1");
  const hasZone = /[zZ]$|[+-]\d\d:?\d\d$/.test(text);
  const d = new Date(hasZone ? text : text + "Z");
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtTime(value) {
  const d = parseTime(value);
  return d ? d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "-";
}

// The viewer's time zone (for example Asia/Singapore), so "today" and the daily chart follow
// their day. The server checks the name and falls back to UTC if the query is left out.
function zoneQuery() {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return zone ? "tz=" + encodeURIComponent(zone) : "";
  } catch (e) {
    return "";
  }
}

// ---------- Helpers ----------
function tag(kind) {
  const s = document.createElement("span");
  s.className = "tag " + kind;
  s.textContent = kind.replace("_", " ");
  return s;
}

function cell(content, cls) {
  const td = document.createElement("td");
  if (cls) td.className = cls;
  if (content instanceof Node) td.appendChild(content);
  else td.textContent = content;
  return td;
}

function actionButton(label, onClick, primary) {
  const b = document.createElement("button");
  b.className = "act" + (primary ? " primary" : "");
  b.type = "button";
  b.textContent = label;
  b.onclick = onClick;
  return b;
}

// A failed request becomes an Error that says which request failed, the status and the server's
// message, for example "Stats failed (403: Admins only)". Shown on the page and in the console.
async function apiFail(res, what) {
  let detail = "";
  try {
    const body = await res.json();
    if (typeof body.detail === "string") detail = body.detail;
  } catch (e) {}
  return new Error(what + " failed (" + res.status + (detail ? ": " + detail : "") + ")");
}

function showNote(id, message) {
  const note = $(id);
  if (!note) return;
  note.textContent = message || "";
  note.hidden = !message;
}

// ---------- Data ----------
async function loadStats() {
  const res = await fetch("/stats?" + zoneQuery());
  if (!res.ok) throw await apiFail(res, "Stats");
  const s = await res.json();
  showNote("s-note", "");

  $("k-total").textContent = s.total;
  $("k-open").textContent = s.open;
  $("k-progress").textContent = s.in_progress;
  $("k-resolved").textContent = s.resolved;
  $("k-today").textContent = s.today;
  $("k-avg").textContent =
    s.avg_resolution_hours === null ? "-" : s.avg_resolution_hours + " h";

  const bars = $("bars");
  bars.innerHTML = "";
  const max = Math.max(1, ...Object.values(s.by_priority));

  ["high", "medium", "low"].forEach((p) => {
    const n = s.by_priority[p] || 0;
    const row = document.createElement("div");
    row.className = "bar-row";
    row.innerHTML = `<span></span><div class="track"><div class="fill ${p}"></div></div><b>${n}</b>`;
    row.querySelector("span").textContent = p[0].toUpperCase() + p.slice(1);
    bars.appendChild(row);
    const fill = row.querySelector(".fill");
    requestAnimationFrame(() =>
      requestAnimationFrame(() => (fill.style.width = (n / max) * 100 + "%"))
    );
  });
}

function statsFailed(err) {
  console.error(err);
  showNote("s-note", "Could not load the ticket numbers. " + (err && err.message ? err.message : ""));
}

async function setStatus(id, status) {
  await fetch("/tickets/" + id, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  refresh();
}

async function loadTickets() {
  const url = filter ? "/tickets?status=" + filter : "/tickets";
  const res = await fetch(url);
  if (!res.ok) throw await apiFail(res, "Tickets");
  const list = await res.json();
  const body = $("rows");
  body.innerHTML = "";

  if (!list.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No tickets here yet.</td></tr>';
    return;
  }

  list.forEach((t) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(t.ref, "ref"),
      cell(t.external_ref || "-", t.external_ref ? "jira" : "jira none"),
      cell(t.summary),
      cell(tag(t.priority)),
      cell(tag(t.status)),
      cell(t.requester || ""),
      cell(fmtTime(t.created_at), "when")
    );

    const actions = document.createElement("div");
    actions.className = "row-actions";

    if (t.status === "open") {
      actions.appendChild(actionButton("Start", () => setStatus(t.id, "in_progress")));
    }
    if (t.status !== "resolved") {
      actions.appendChild(actionButton("Resolve", () => setStatus(t.id, "resolved"), true));
    } else {
      actions.appendChild(actionButton("Reopen", () => setStatus(t.id, "open")));
    }

    tr.appendChild(cell(actions));
    body.appendChild(tr);
  });
}

// ---------- Analytics ----------
let days = 30;
const SVG = "http://www.w3.org/2000/svg";

const pct = (v) => (v === null ? "-" : Math.round(v * 100) + "%");
const ms = (v) => (v === null ? "-" : v >= 1000 ? (v / 1000).toFixed(1) + " s" : v + " ms");

function svgEl(name, attrs, text) {
  const el = document.createElementNS(SVG, name);
  Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, v));
  if (text !== undefined) el.textContent = text;
  return el;
}

// Bar chart of questions per day, drawn as plain SVG (no library)
function drawDaily(series) {
  const host = $("a-chart");
  host.innerHTML = "";
  const W = 720, H = 190, left = 30, right = 6, top = 10, bottom = 24;
  const max = Math.max(1, ...series.map((p) => p.count));
  const plotW = W - left - right, plotH = H - top - bottom;
  const slot = plotW / series.length;
  const barW = Math.max(2, slot * 0.7);

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
  [0, 0.5, 1].forEach((f) => {
    const y = top + plotH * (1 - f);
    svg.appendChild(svgEl("line", { x1: left, x2: W - right, y1: y, y2: y, class: "grid" }));
    svg.appendChild(svgEl("text", { x: left - 6, y: y + 4, "text-anchor": "end" }, Math.round(max * f)));
  });

  series.forEach((p, i) => {
    const h = (p.count / max) * plotH;
    const rect = svgEl("rect", {
      x: left + i * slot + (slot - barW) / 2,
      y: top + plotH - h,
      width: barW,
      height: Math.max(h, p.count ? 1 : 0),
      rx: 2,
      class: "bar" + (p.count ? "" : " zero"),
    });
    rect.appendChild(svgEl("title", {}, p.day + ": " + p.count + (p.count === 1 ? " question" : " questions")));
    svg.appendChild(rect);
  });

  // a few date labels along the bottom: first, middle and last day
  [0, Math.floor((series.length - 1) / 2), series.length - 1].forEach((i, n) => {
    const anchor = n === 0 ? "start" : n === 2 ? "end" : "middle";
    const x = left + i * slot + (n === 0 ? 0 : n === 2 ? slot : slot / 2);
    svg.appendChild(svgEl("text", { x, y: H - 6, "text-anchor": anchor }, series[i].day.slice(5)));
  });
  host.appendChild(svg);
  host.setAttribute(
    "aria-label",
    "Questions per day, last " + series.length + " days, " + series.reduce((n, p) => n + p.count, 0) + " in total"
  );
}

// A short ranked list with a bar under each row. Text goes in with textContent.
function drawTop(id, rows, labelOf, emptyText) {
  const host = $(id);
  host.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "top-empty";
    empty.textContent = emptyText;
    host.appendChild(empty);
    return;
  }
  const max = Math.max(...rows.map((r) => r.count));
  rows.forEach((r) => {
    const row = document.createElement("div");
    row.className = "top-row";
    row.innerHTML =
      '<div class="top-head"><span class="top-label"></span><b></b></div>' +
      '<div class="track"><div class="fill medium"></div></div>';
    row.querySelector(".top-label").textContent = labelOf(r);
    row.querySelector("b").textContent = r.count;
    host.appendChild(row);
    const fill = row.querySelector(".fill");
    requestAnimationFrame(() =>
      requestAnimationFrame(() => (fill.style.width = (r.count / max) * 100 + "%"))
    );
  });
}

async function loadAnalytics() {
  showNote("a-note", "");
  const res = await fetch("/stats/analytics?days=" + days + "&" + zoneQuery());
  if (!res.ok) throw await apiFail(res, "Analytics");
  const a = await res.json();

  $("a-questions").textContent = a.totals.questions;
  $("a-deflection").textContent = pct(a.deflection_rate);
  $("a-median").textContent = ms(a.latency_ms.median);
  $("a-p95").textContent = ms(a.latency_ms.p95);
  $("a-thumbs").textContent = a.feedback.up_share === null ? "-" : pct(a.feedback.up_share);
  $("a-thumbs").title = a.feedback.up + " up, " + a.feedback.down + " down";

  drawDaily(a.questions_per_day);
  drawTop("a-articles", a.top_articles, (r) => r.source, "No cited articles in this period.");
  drawTop("a-escalated", a.top_escalated_questions, (r) => r.question, "Nothing was escalated in this period.");

  $("a-foot").textContent =
    "Last " + a.days + " days. Answered without escalation = answered / (answered + escalated). " +
    a.totals.escalated + " escalated, " + a.totals.actions_proposed + " actions proposed, " +
    a.totals.blocked + " blocked. Escalated questions are shown with personal data redacted, " +
    "but redaction is automatic and may miss some.";
}

function analyticsFailed(err) {
  console.error(err);
  showNote("a-note", "Could not load the analytics. " + (err && err.message ? err.message : ""));
}

function refresh() {
  loadAnalytics().catch(analyticsFailed);
  loadStats().catch(statsFailed);
  loadTickets().catch((err) => {
    console.error(err);
    $("rows").innerHTML =
      '<tr><td colspan="8" class="empty">Could not load tickets. ' +
      (err && err.message ? err.message.replace(/[<>&]/g, "") : "") +
      "</td></tr>";
  });
}

// ---------- Events ----------
$("filters").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    filter = b.dataset.s;
    $("filters")
      .querySelectorAll("button")
      .forEach((x) => x.classList.toggle("on", x === b));
    loadTickets().catch(() => {});
  };
});

$("periods").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    days = Number(b.dataset.d);
    $("periods")
      .querySelectorAll("button")
      .forEach((x) => x.classList.toggle("on", x === b));
    loadAnalytics().catch(analyticsFailed);
  };
});

$("refresh").onclick = refresh;

$("theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("theme", next);
  } catch (e) {}
};

refresh();
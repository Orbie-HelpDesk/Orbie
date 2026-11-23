const $ = (id) => document.getElementById(id);

// The actions the app writes (the server accepts others too, but these are the ones to pick from)
const ACTIONS = [
  "login", "login_failed", "logout", "ticket_status_change", "create_ticket", "reset_password",
  "conversation_read", "conversation_deleted", "password_set", "kb_create", "kb_update", "kb_delete",
  "messages_purged", "demo_seeded", "audit_viewed",
];

let page = 1;
let hasNext = false;
let busy = false;
let applied = {}; // the filters of the page on screen, so Newer and Older keep them

// ---------- Time, the way the dashboard does it ----------
// The API sends UTC with an offset. The browser shows it in the viewer's local time.
function parseTime(value) {
  if (!value) return null;
  const text = value.replace(/(\.\d{3})\d+/, "$1"); // some browsers only accept milliseconds
  const hasZone = /[zZ]$|[+-]\d\d:?\d\d$/.test(text);
  const d = new Date(hasZone ? text : text + "Z");
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtTime(value) {
  const d = parseTime(value);
  return d ? d.toLocaleString([], { dateStyle: "medium", timeStyle: "medium" }) : "-";
}

// The viewer's zone, so the From and To days are their days. Sent only when a date is chosen.
function browserZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch (e) {
    return "";
  }
}

// ---------- Helpers ----------
function say(text, isError) {
  const el = $("message");
  el.textContent = text || "";
  el.className = "au-message" + (isError ? " error" : "");
}

function tagClass(action) {
  if (action === "login_failed") return "au-failed";
  if (action === "login" || action === "logout" || action === "password_set") return "au-auth";
  if (action === "ticket_status_change") return "au-ticket";
  if (action === "create_ticket" || action === "reset_password") return "au-action";
  if (action.startsWith("conversation_") || action === "messages_purged") return "au-chat";
  if (action.startsWith("kb_")) return "au-kb";
  return "au-other";
}

function cell(text, cls) {
  const td = document.createElement("td");
  if (cls) td.className = cls;
  td.textContent = text;
  return td;
}

function readFilters() {
  const filters = {};
  const action = $("action").value;
  const actor = $("actor").value.trim();
  const from = $("dateFrom").value;
  const to = $("dateTo").value;
  if (action) filters.action = action;
  if (actor) filters.actor = actor;
  if (from) filters.from = from;
  if (to) filters.to = to;
  if ((from || to) && browserZone()) filters.tz = browserZone();
  return filters;
}

// ---------- Loading a page ----------
function render(items) {
  const body = $("rows");
  body.innerHTML = "";
  if (!items.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "empty";
    td.textContent = "Nothing matches these filters.";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  items.forEach((item) => {
    const tr = document.createElement("tr");
    const when = cell(fmtTime(item.time), "when");
    when.title = item.time; // the exact UTC time, for anyone who needs it
    const actor = cell(item.actor, item.actor === "(hidden)" ? "actor hidden-actor" : "actor");
    const actionTd = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = "tag " + tagClass(item.action);
    tag.textContent = item.action;
    actionTd.appendChild(tag);
    tr.append(when, actor, actionTd, cell(item.summary || "-", "detail" + (item.summary ? "" : " none")));
    body.appendChild(tr);
  });
}

function setPager() {
  $("newer").disabled = busy || page <= 1;
  $("older").disabled = busy || !hasNext;
  $("pageInfo").textContent = "Page " + page + " · 50 per page";
}

async function load(newPage, filters) {
  if (busy) return;
  busy = true;
  page = newPage;
  applied = filters;
  setPager();
  say("Loading...");
  const params = new URLSearchParams({ ...filters, page: String(page) });
  try {
    const res = await fetch("/audit?" + params.toString(), { cache: "no-store" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      let detail = typeof data.detail === "string" ? data.detail : "Could not load the audit log.";
      if (Array.isArray(data.detail)) detail = "Check the filters: one of the values is not allowed.";
      if (res.status === 429) detail = "Too many requests. Please wait a moment.";
      throw new Error(detail);
    }
    hasNext = data.has_next;
    render(data.items);
    say("");
  } catch (err) {
    $("rows").innerHTML = "";
    hasNext = false;
    say(err.message, true);
  }
  busy = false;
  setPager();
}

// ---------- Events ----------
$("filters").addEventListener("submit", (e) => {
  e.preventDefault();
  const from = $("dateFrom").value;
  const to = $("dateTo").value;
  if (from && to && from > to) {
    say("The From date must not be after the To date.", true);
    return;
  }
  load(1, readFilters());
});

$("clear").onclick = () => {
  $("filters").reset();
  load(1, {});
};
$("newer").onclick = () => page > 1 && load(page - 1, applied);
$("older").onclick = () => hasNext && load(page + 1, applied);
$("refresh").onclick = () => load(page, applied);
$("theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("theme", next);
  } catch (e) {}
};

ACTIONS.forEach((name) => {
  const option = document.createElement("option");
  option.value = name;
  option.textContent = name;
  $("action").appendChild(option);
});

load(1, {});

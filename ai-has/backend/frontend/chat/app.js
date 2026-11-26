const $ = (id) => document.getElementById(id);

const streamEl = $("stream");
const inputEl = $("input");
const sendBtn = $("send");
const formEl = $("form");
const ledEl = $("led");
const statusEl = $("status");
const historyEl = $("history");
const scrimEl = $("scrim");
const listEl = $("historyList");
const countEl = $("historyCount");
const footEl = $("historyFoot");
const clearBtn = $("clearHistory");
const mainOrbs = [$("headOrb"), $("heroOrb")];

// History is kept per user, so two people on one browser never see each other's chats
const STORE_KEY = "aihas.chats." + (window.aihasUser ? window.aihasUser.id : "anon");

const EYES =
  '<div class="eyes"><i class="eye"><b class="pupil"></b></i><i class="eye"><b class="pupil"></b></i></div>';

const ICONS = {
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>',
  ticket:
    '<path d="M3 9V6a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1v3a3 3 0 0 0 0 6v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-3a3 3 0 0 0 0-6z"/><path d="M13 5v14" stroke-dasharray="2 3"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  retry: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  thumb:
    '<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/>',
};

const PHASES = [
  "Understanding your question",
  "Searching the knowledge base",
  "Ranking the best sources",
  "Writing your answer",
];

const icon = (name) => `<svg class="icon" viewBox="0 0 24 24">${ICONS[name]}</svg>`;

let chats = loadChats();
let current = null;
let busy = false;

// ---------- Storage ----------
function loadChats() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY)) || [];
  } catch (e) {
    return [];
  }
}

function persist() {
  try {
    // Chats loaded from the server are not copied here. The browser copy is the fallback.
    localStorage.setItem(STORE_KEY, JSON.stringify(chats.filter((c) => !c.fromServer)));
  } catch (e) {}
  renderHistory();
}

// ---------- Helpers ----------
const scrollDown = () => {
  streamEl.scrollTop = streamEl.scrollHeight;
};

const fmtTime = (ts) =>
  new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function fmtWhen(ts) {
  const d = new Date(ts);
  return d.toDateString() === new Date().toDateString()
    ? fmtTime(ts)
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function groupLabel(ts) {
  const day = 86400000;
  const start = new Date().setHours(0, 0, 0, 0);
  if (ts >= start) return "Today";
  if (ts >= start - day) return "Yesterday";
  return "Earlier";
}

function setChatting(on) {
  document.body.classList.toggle("chatting", on);
}

function setThinking(on) {
  mainOrbs.forEach((o) => o && o.classList.toggle("think", on));
  if (window.field) window.field.setMode(on ? "think" : "idle");
}

function pulseOrb(cls) {
  mainOrbs.forEach((o) => {
    if (!o) return;
    o.classList.remove("happy", "alert");
    void o.offsetWidth;
    o.classList.add(cls);
    setTimeout(() => o.classList.remove(cls), 900);
  });
}

// ---------- Connection status ----------
fetch("/health")
  .then((res) => res.json())
  .then((data) => {
    ledEl.classList.add("on");
    statusEl.textContent =
      data.database === "connected" ? "Online · KB ready" : "Online · DB issue";
  })
  .catch(() => {
    statusEl.textContent = "Offline";
  });

// ---------- Answer rendering ----------
function parseAnswer(text) {
  return text
    .replace(/\*\*/g, "")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const m = l.match(/^\d+\.\s+(.*)/);
      return m ? { step: true, text: m[1] } : { step: false, text: l };
    });
}

function fillBubble(bubble, text, animate, done) {
  const items = parseAnswer(text);
  const total = items.reduce((n, it) => n + it.text.length, 0) || 1;
  const delay = Math.max(4, Math.min(16, 2000 / total));
  let ol = null;
  let i = 0;

  function next() {
    if (i >= items.length) {
      if (done) done();
      return;
    }
    const it = items[i++];
    let target;

    if (it.step) {
      if (!ol) {
        ol = document.createElement("ol");
        bubble.appendChild(ol);
      }
      const li = document.createElement("li");
      li.innerHTML =
        '<label><input type="checkbox"><span class="box"></span><span class="txt"></span></label>';
      ol.appendChild(li);
      target = li.querySelector(".txt");
    } else {
      ol = null;
      target = document.createElement("p");
      bubble.appendChild(target);
    }

    if (!animate) {
      target.textContent = it.text;
      next();
      return;
    }

    target.classList.add("caret");
    let n = 0;
    (function tick() {
      target.textContent = it.text.slice(0, ++n);
      scrollDown();
      if (n < it.text.length) {
        setTimeout(tick, delay);
      } else {
        target.classList.remove("caret");
        setTimeout(next, 80);
      }
    })();
  }
  next();
}

function buildTrace(data) {
  const best = Math.min(...data.distances);
  const pct = Math.max(0, Math.min(100, Math.round(((0.5 - best) / 0.25) * 100)));
  const low = data.escalate || pct < 25;

  const trace = document.createElement("div");
  trace.className = "trace";
  trace.innerHTML = `
    <div class="gauge${low ? " low" : ""}">
      <svg viewBox="0 0 44 44">
        <circle class="track" cx="22" cy="22" r="19"></circle>
        <circle class="fg" cx="22" cy="22" r="19"></circle>
      </svg>
      <b>${pct}%</b>
    </div>
    <div class="t-info">
      <small>${data.escalate ? "No confident match" : "Match confidence"}</small>
      <div class="chips"></div>
    </div>`;

  const chips = trace.querySelector(".chips");
  (data.citations || []).forEach((name) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = icon("file");
    chip.append(name);
    chips.appendChild(chip);
  });

  const fg = trace.querySelector(".fg");
  requestAnimationFrame(() =>
    requestAnimationFrame(() => {
      fg.style.strokeDashoffset = (119.4 * (1 - pct / 100)).toFixed(1);
    })
  );
  return trace;
}

// Thumbs up or down. The rating is saved on the server for the assistant message. Clicking the
// chosen thumb again clears it. If the message was not saved (no id), it only thanks the user.
function buildFeedback(record) {
  const data = record.data;
  const fb = document.createElement("div");
  fb.className = "fb";
  fb.innerHTML = `<span>Was this helpful?</span>
    <button type="button" class="up" aria-label="Helpful">${icon("thumb")}</button>
    <button type="button" class="down flip" aria-label="Not helpful">${icon("thumb")}</button>`;
  const label = fb.querySelector("span");
  const up = fb.querySelector(".up");
  const down = fb.querySelector(".down");

  const show = (value) => {
    up.classList.toggle("picked", value === 1);
    down.classList.toggle("picked", value === -1);
    label.textContent = value ? "Thanks for the feedback" : "Was this helpful?";
  };
  show(data.feedback || 0);

  const rate = async (value) => {
    const previous = data.feedback || 0;
    const next = previous === value ? 0 : value;
    data.feedback = next;
    show(next);
    persist();
    if (!data.message_id) return;
    try {
      const res = await fetch("/messages/" + data.message_id + "/feedback", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: next }),
      });
      if (!res.ok) throw new Error(res.status);
    } catch (err) {
      data.feedback = previous;
      show(previous);
      label.textContent = "Could not save your rating";
      persist();
    }
  };
  up.onclick = () => rate(1);
  down.onclick = () => rate(-1);
  return fb;
}

// Creates a real ticket in the database
function buildTicketButton(record) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ticket-btn";

  const markDone = (ref) => {
    btn.disabled = true;
    btn.innerHTML = icon("check");
    btn.append(`Ticket ${ref} created`);
  };

  if (record.ticket) {
    markDone(record.ticket);
    return btn;
  }

  btn.innerHTML = icon("ticket");
  btn.append("Create a ticket");
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = "Creating ticket...";
    const q = record.question || "";
    // The API accepts 3 to 500 characters
    const summary = (q.length >= 3 ? q : `Help request: ${q}`).slice(0, 500);
    try {
      const res = await fetch("/tickets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          summary,
          priority: "medium",
          // links the ticket to its saved conversation
          ...(current && current.serverId ? { conversation_id: current.serverId } : {}),
        }),
      });
      if (!res.ok) throw new Error(res.status);
      const ticket = await res.json();
      record.ticket = ticket.ref;
      persist();
      markDone(ticket.ref);
    } catch (err) {
      btn.disabled = false;
      btn.innerHTML = icon("ticket");
      btn.append("Failed, try again");
    }
  };
  return btn;
}

// ---------- Proposed action (needs the user's confirmation) ----------
const ACTION_TITLES = {
  create_ticket: "Create a ticket",
  request_password_reset: "Reset my password",
};

// The assistant only proposes. Nothing runs until Confirm is pressed.
function buildActionCard(record) {
  const proposal = record.data.proposed_action;
  const card = document.createElement("div");
  card.className = "action-card";

  const title = document.createElement("div");
  title.className = "ac-title";
  title.innerHTML = icon("ticket");
  title.append(ACTION_TITLES[proposal.name] || "Proposed action");

  const body = document.createElement("div");
  body.className = "ac-body";
  body.textContent = proposal.label;

  const row = document.createElement("div");
  row.className = "ac-row";
  const confirmBtn = document.createElement("button");
  confirmBtn.type = "button";
  confirmBtn.className = "ticket-btn ac-confirm";
  confirmBtn.append("Confirm");
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "ac-cancel";
  cancelBtn.append("Cancel");
  row.append(confirmBtn, cancelBtn);

  const note = document.createElement("div");
  note.className = "ac-note";
  note.setAttribute("role", "status");

  const showResult = (state, message) => {
    card.dataset.state = state;
    row.remove();
    note.textContent = message;
  };

  card.append(title, body, row, note);

  if (record.action) {
    showResult(record.action.state, record.action.message);
    return card;
  }

  const decide = async (confirm) => {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    note.textContent = confirm ? "Working on it..." : "Cancelling...";
    try {
      const res = await fetch("/actions/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action_id: proposal.id, confirm }),
      });
      if (res.status === 429) {
        note.textContent = "Too many requests. Please wait a moment and try again.";
        confirmBtn.disabled = false;
        cancelBtn.disabled = false;
        return;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        // 403, 404, 409 and 410 are final: the proposal cannot be used again
        record.action = { state: "closed", message: data.detail || "This action is no longer available." };
      } else {
        record.action = { state: data.status, message: data.message };
      }
      persist();
      showResult(record.action.state, record.action.message);
    } catch (err) {
      note.textContent = "Could not reach the server. Please try again.";
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  };
  confirmBtn.onclick = () => decide(true);
  cancelBtn.onclick = () => decide(false);
  return card;
}

// ---------- Messages ----------
function renderUser(m) {
  const msg = document.createElement("div");
  msg.className = "msg user";
  msg.innerHTML =
    '<div class="col"><div class="bubble"></div><div class="meta"></div></div>';
  msg.querySelector(".bubble").textContent = m.text;
  msg.querySelector(".meta").textContent = fmtTime(m.ts);
  streamEl.appendChild(msg);
  scrollDown();
}

function renderBot(m, animate, onDone) {
  const d = m.data;
  const alert = d.escalate || d.failed;
  const kind = alert ? "alert" : "bot";
  const blocked = d.guardrails && d.guardrails.blocked;

  const msg = document.createElement("div");
  msg.className = `msg ${kind}`;
  msg.innerHTML = `<div class="orb${alert ? " alert" : ""}" style="--s:34px">${EYES}</div>
    <div class="col"><div class="bubble"></div><div class="meta"></div></div>`;
  streamEl.appendChild(msg);

  const bubble = msg.querySelector(".bubble");
  msg.querySelector(".meta").textContent = `Orbie · ${fmtTime(m.ts)}`;

  fillBubble(bubble, d.answer, animate, () => {
    if (d.distances && d.distances.length) bubble.appendChild(buildTrace(d));
    if (d.failed) {
      bubble.appendChild(buildRetryButton(m, msg));
    } else if (d.proposed_action) {
      bubble.appendChild(buildActionCard(m));
    } else if (d.escalate) {
      bubble.appendChild(buildTicketButton(m));
    } else if (!blocked && d.citations && d.citations.length) {
      bubble.appendChild(buildFeedback(m));
    }
    scrollDown();
    if (onDone) onDone();
  });
  scrollDown();
}

// A failed answer (the AI service is busy, rate limit, network) gets a Try again button that sends
// the same question again. It stays disabled for the Retry-After seconds the server asked for.
function buildRetryButton(record, msgEl) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ticket-btn retry-btn";

  const waitLeft = () =>
    Math.max(0, Math.ceil((record.data.retryAfter || 0) - (Date.now() - record.ts) / 1000));

  const label = () => {
    const left = waitLeft();
    btn.disabled = left > 0;
    btn.innerHTML = icon("retry");
    btn.append(left > 0 ? `Try again in ${left}s` : "Try again");
    return left;
  };

  if (label() > 0) {
    const timer = setInterval(() => {
      if (!btn.isConnected || label() === 0) clearInterval(timer);
    }, 1000);
  }

  btn.onclick = () => {
    if (busy || btn.disabled) return;
    // Remove the failed answer, then ask the same question again. The question itself stays.
    current.messages = current.messages.filter((x) => x !== record);
    msgEl.remove();
    persist();
    ask(record.question, true);
  };
  return btn;
}

function showThinking() {
  const msg = document.createElement("div");
  msg.className = "msg bot";
  msg.innerHTML = `<div class="orb think" style="--s:34px">${EYES}</div>
    <div class="col"><div class="bubble"><div class="phases">
      ${PHASES.map((p) => `<div class="ph"><i></i>${p}</div>`).join("")}
    </div></div></div>`;
  streamEl.appendChild(msg);
  scrollDown();

  const items = [...msg.querySelectorAll(".ph")];
  let i = 0;
  items[0].classList.add("active");
  const timer = setInterval(() => {
    if (i < items.length - 1) {
      items[i].classList.replace("active", "done");
      i++;
      items[i].classList.add("active");
    }
  }, 1300);

  return {
    remove() {
      clearInterval(timer);
      msg.remove();
    },
  };
}

// ---------- Sending a question ----------
const BUSY_MESSAGE = "The AI service is busy right now. Please try again in a moment.";

// An answer record for a request that failed, with how many seconds to wait before trying again
function failedAnswer(message, res) {
  const wait = res ? parseInt(res.headers.get("Retry-After"), 10) : 0;
  return {
    answer: message,
    citations: [],
    escalate: false,
    distances: [],
    failed: true,
    retryAfter: Number.isFinite(wait) ? Math.min(Math.max(wait, 0), 60) : 0,
  };
}

async function ask(question, retry = false) {
  question = question.trim();
  if (!question || busy) return;
  busy = true;
  sendBtn.disabled = true;

  if (!current) {
    current = {
      id: Date.now().toString(36),
      title: question.slice(0, 48),
      ts: Date.now(),
      messages: [],
    };
    chats.unshift(current);
  }
  setChatting(true);

  if (!retry) {
    const userMsg = { role: "user", text: question, ts: Date.now() };
    current.messages.push(userMsg);
    current.ts = Date.now();
    renderUser(userMsg);
    persist();
  }

  setThinking(true);
  const thinking = showThinking();

  const sendChat = () =>
    fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: question,
        ...(current.serverId ? { conversation_id: current.serverId } : {}),
      }),
    });

  let data;
  try {
    let res = await sendChat();
    if (res.status === 404 && current.serverId) {
      // The saved conversation is gone (deleted elsewhere): carry on in a new one
      current.serverId = null;
      res = await sendChat();
    }
    if (res.status === 429) {
      data = failedAnswer("You're sending messages too quickly. Please wait a moment and try again.", res);
    } else if (res.status === 503) {
      // The AI service is down or busy. The server's own message is shown, with Try again.
      const body = await res.json().catch(() => ({}));
      data = failedAnswer(typeof body.detail === "string" ? body.detail : BUSY_MESSAGE, res);
    } else {
      if (!res.ok) throw new Error(res.status);
      data = await res.json();
    }
  } catch (err) {
    data = failedAnswer("Sorry, something went wrong. Please try again.");
  }

  thinking.remove();
  setThinking(false);

  if (data.conversation_id) current.serverId = data.conversation_id;
  const botMsg = { role: "bot", question, data, ts: Date.now() };
  current.messages.push(botMsg);
  persist();
  // Only a saved answer has a conversation_id. A failed one (503) is not saved, so nothing to list.
  if (data.conversation_id) loadServerList();
  pulseOrb(data.escalate || data.failed ? "alert" : "happy");

  renderBot(botMsg, true, () => {
    busy = false;
    sendBtn.disabled = false;
    inputEl.focus();
  });
}

// ---------- Server history ----------
// The server stores times without a zone, in UTC
function serverTime(value) {
  if (!value) return Date.now();
  const text = value.replace(/(\.\d{3})\d+/, "$1"); // some browsers only accept milliseconds
  const t = Date.parse(/[zZ]$|[+-]\d\d:?\d\d$/.test(text) ? text : text + "Z");
  return Number.isNaN(t) ? Date.now() : t;
}

// When logged in, the saved conversations from the server are the history. If that fails, the
// history kept in this browser is used instead.
let listRequest = 0;

async function loadServerList() {
  if (!window.aihasUser) return;
  const mine = ++listRequest;
  try {
    const res = await fetch("/conversations");
    if (!res.ok) throw new Error(res.status);
    const list = await res.json();
    if (mine !== listRequest) return; // a newer refresh is running, so this answer is out of date
    const open = current && current.serverId;
    chats = list.map((c) => ({
      id: "s" + c.id,
      serverId: c.id,
      fromServer: true,
      title: c.title,
      ts: serverTime(c.last_at),
      messages: null, // loaded when the chat is opened
    }));
    // A chat that is open right now keeps its messages
    if (open) {
      const same = chats.find((c) => c.serverId === open);
      if (same) {
        same.messages = current.messages;
        current = same;
      }
    }
    renderHistory();
  } catch (err) {
    // Network error, 401 or 5xx: keep the list that is already shown, with no error message
  }
}

// ---------- Chat history ----------
function openHistory() {
  historyEl.classList.add("open");
  scrimEl.classList.add("open");
  renderHistory();
  loadServerList();
}

function closeHistory() {
  historyEl.classList.remove("open");
  scrimEl.classList.remove("open");
  footEl.classList.remove("confirming");
}

function resetView() {
  current = null;
  streamEl.innerHTML = "";
  setChatting(false);
}

function newChat() {
  if (busy) return;
  resetView();
  closeHistory();
  inputEl.focus();
}

// A saved message from the server, shaped like the records the page renders
function fromServerMessage(m) {
  const ts = serverTime(m.created_at);
  if (m.role === "user") return { role: "user", text: m.content, ts };
  return {
    role: "bot",
    question: "",
    ts,
    data: {
      answer: m.content,
      citations: m.citations || [],
      escalate: !!m.escalated,
      distances: [], // only the confidence is saved, so history shows no gauge
      confidence: m.confidence,
      message_id: m.id,
      feedback: m.feedback || 0,
      proposed_action: null,
      guardrails: { blocked: !!m.blocked, reason: null, redacted: [] },
    },
  };
}

async function openChat(id) {
  if (busy) return;
  const chat = chats.find((c) => c.id === id);
  if (!chat) return;
  if (chat.fromServer && !chat.messages) {
    try {
      const res = await fetch("/conversations/" + chat.serverId);
      if (!res.ok) throw new Error(res.status);
      chat.messages = (await res.json()).messages.map(fromServerMessage);
    } catch (err) {
      return; // keep the current view, the list stays as it is
    }
  }
  current = chat;
  streamEl.innerHTML = "";
  chat.messages.forEach((m) =>
    m.role === "user" ? renderUser(m) : renderBot(m, false)
  );
  setChatting(chat.messages.length > 0);
  closeHistory();
  scrollDown();
}

async function deleteChat(id) {
  if (busy) return;
  const chat = chats.find((c) => c.id === id);
  if (chat && chat.serverId) {
    try {
      const res = await fetch("/conversations/" + chat.serverId, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!res.ok && res.status !== 404) throw new Error(res.status);
    } catch (err) {
      return; // not deleted on the server, so it stays in the list
    }
  }
  chats = chats.filter((c) => c.id !== id);
  if (current && current.id === id) resetView();
  persist();
  loadServerList();
}

function renderHistory() {
  countEl.textContent = chats.length;
  clearBtn.disabled = chats.length === 0;
  listEl.innerHTML = "";

  if (!chats.length) {
    listEl.innerHTML = `<div class="h-empty"><span class="h-ico">${icon("chat")}</span>
      <div>No saved chats yet.<br>Ask something and it will show up here.</div></div>`;
    return;
  }

  let lastGroup = "";
  [...chats]
    .sort((a, b) => b.ts - a.ts)
    .forEach((chat) => {
      const group = groupLabel(chat.ts);
      if (group !== lastGroup) {
        const label = document.createElement("div");
        label.className = "h-group";
        label.textContent = group;
        listEl.appendChild(label);
        lastGroup = group;
      }

      const item = document.createElement("div");
      item.className = "h-item" + (current && current.id === chat.id ? " active" : "");
      item.innerHTML = `<button type="button" class="h-open">
          <span class="h-ico">${icon("chat")}</span>
          <span class="h-txt"><span class="h-title"></span><span class="h-time"></span></span>
        </button>
        <button type="button" class="h-del" aria-label="Delete chat">${icon("trash")}</button>`;
      item.querySelector(".h-title").textContent = chat.title;
      item.querySelector(".h-time").textContent = fmtWhen(chat.ts);
      item.querySelector(".h-open").onclick = () => openChat(chat.id);
      item.querySelector(".h-del").onclick = () => deleteChat(chat.id);
      listEl.appendChild(item);
    });
}

// ---------- Events ----------
formEl.onsubmit = (e) => {
  e.preventDefault();
  const q = inputEl.value.trim();
  if (q) {
    inputEl.value = "";
    ask(q);
  }
};

document.querySelectorAll(".s-card").forEach((card) => {
  card.addEventListener("click", () => ask(card.dataset.q));
  card.addEventListener("pointermove", (e) => {
    const r = card.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    card.style.setProperty("--mx", x * 100 + "%");
    card.style.setProperty("--my", y * 100 + "%");
    card.style.setProperty("--ry", ((x - 0.5) * 8).toFixed(2) + "deg");
    card.style.setProperty("--rx", ((0.5 - y) * 8).toFixed(2) + "deg");
  });
  card.addEventListener("pointerleave", () => {
    card.style.setProperty("--rx", "0deg");
    card.style.setProperty("--ry", "0deg");
  });
});

$("historyBtn").onclick = openHistory;
$("closeHistory").onclick = closeHistory;
scrimEl.onclick = closeHistory;
$("newChat").onclick = newChat;
$("reset").onclick = newChat;

clearBtn.onclick = () => footEl.classList.add("confirming");
$("clearCancel").onclick = () => footEl.classList.remove("confirming");
$("clearConfirm").onclick = async () => {
  if (busy) return;
  footEl.classList.remove("confirming");
  const saved = chats.filter((c) => c.serverId);
  const results = await Promise.all(
    saved.map((c) =>
      fetch("/conversations/" + c.serverId, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      })
        .then((r) => r.ok || r.status === 404)
        .catch(() => false)
    )
  );
  // Keep the chats the server could not delete, so nothing silently comes back
  const failed = new Set(saved.filter((_, i) => !results[i]).map((c) => c.id));
  chats = chats.filter((c) => failed.has(c.id));
  resetView();
  persist();
  loadServerList();
};

$("theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("theme", next);
  } catch (e) {}
};

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeHistory();
  const tag = document.activeElement.tagName;
  if (e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA") {
    e.preventDefault();
    inputEl.focus();
  }
});

renderHistory();
loadServerList();
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

const STORE_KEY = "aihas.chats";

const EYES =
  '<div class="eyes"><i class="eye"><b class="pupil"></b></i><i class="eye"><b class="pupil"></b></i></div>';

const ICONS = {
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>',
  ticket:
    '<path d="M3 9V6a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1v3a3 3 0 0 0 0 6v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-3a3 3 0 0 0 0-6z"/><path d="M13 5v14" stroke-dasharray="2 3"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
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
    localStorage.setItem(STORE_KEY, JSON.stringify(chats));
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

function buildFeedback() {
  const fb = document.createElement("div");
  fb.className = "fb";
  fb.innerHTML = `<span>Was this helpful?</span>
    <button type="button" class="up" aria-label="Helpful">${icon("thumb")}</button>
    <button type="button" class="down flip" aria-label="Not helpful">${icon("thumb")}</button>`;

  fb.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      btn.classList.add("picked");
      fb.querySelectorAll("button").forEach((b) => (b.disabled = true));
      fb.querySelector("span").textContent = "Thanks for the feedback";
    };
  });
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
        body: JSON.stringify({ summary, priority: "medium" }),
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
  const kind = d.escalate ? "alert" : "bot";
  const blocked = d.guardrails && d.guardrails.blocked;

  const msg = document.createElement("div");
  msg.className = `msg ${kind}`;
  msg.innerHTML = `<div class="orb${d.escalate ? " alert" : ""}" style="--s:34px">${EYES}</div>
    <div class="col"><div class="bubble"></div><div class="meta"></div></div>`;
  streamEl.appendChild(msg);

  const bubble = msg.querySelector(".bubble");
  msg.querySelector(".meta").textContent = `Orbie · ${fmtTime(m.ts)}`;

  fillBubble(bubble, d.answer, animate, () => {
    if (d.distances && d.distances.length) bubble.appendChild(buildTrace(d));
    if (d.escalate) {
      bubble.appendChild(buildTicketButton(m));
    } else if (!blocked && d.citations && d.citations.length) {
      bubble.appendChild(buildFeedback());
    }
    scrollDown();
    if (onDone) onDone();
  });
  scrollDown();
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
async function ask(question) {
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

  const userMsg = { role: "user", text: question, ts: Date.now() };
  current.messages.push(userMsg);
  current.ts = Date.now();
  renderUser(userMsg);
  persist();

  setThinking(true);
  const thinking = showThinking();

  let data;
  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question }),
    });
    if (res.status === 429) {
      data = {
        answer: "You're sending messages too quickly. Please wait a moment and try again.",
        citations: [],
        escalate: false,
        distances: [],
      };
    } else {
      if (!res.ok) throw new Error(res.status);
      data = await res.json();
    }
  } catch (err) {
    data = {
      answer: "Sorry, something went wrong. Please try again.",
      citations: [],
      escalate: false,
      distances: [],
    };
  }

  thinking.remove();
  setThinking(false);

  const botMsg = { role: "bot", question, data, ts: Date.now() };
  current.messages.push(botMsg);
  persist();
  pulseOrb(data.escalate ? "alert" : "happy");

  renderBot(botMsg, true, () => {
    busy = false;
    sendBtn.disabled = false;
    inputEl.focus();
  });
}

// ---------- Chat history ----------
function openHistory() {
  historyEl.classList.add("open");
  scrimEl.classList.add("open");
  renderHistory();
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

function openChat(id) {
  if (busy) return;
  const chat = chats.find((c) => c.id === id);
  if (!chat) return;
  current = chat;
  streamEl.innerHTML = "";
  chat.messages.forEach((m) =>
    m.role === "user" ? renderUser(m) : renderBot(m, false)
  );
  setChatting(chat.messages.length > 0);
  closeHistory();
  scrollDown();
}

function deleteChat(id) {
  if (busy) return;
  chats = chats.filter((c) => c.id !== id);
  if (current && current.id === id) resetView();
  persist();
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
$("clearConfirm").onclick = () => {
  if (busy) return;
  chats = [];
  resetView();
  footEl.classList.remove("confirming");
  persist();
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
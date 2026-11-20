const $ = (id) => document.getElementById(id);

const streamEl = $("stream");
const inputEl = $("input");
const sendBtn = $("send");
const ledEl = $("led");
const statusEl = $("status");
const suggestEl = $("suggest");
const heroOrb = $("heroOrb");
const headOrb = $("headOrb");
const historyEl = $("history");
const scrimEl = $("scrim");
const listEl = $("historyList");

const ICONS = {
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>',
  ticket: '<path d="M3 9V6a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1v3a3 3 0 0 0 0 6v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-3a3 3 0 0 0 0-6z"/><path d="M13 5v14" stroke-dasharray="2 3"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  thumb: '<path d="M7 11v9H4v-9z"/><path d="M7 11l4-8a2 2 0 0 1 2 2v4h6a2 2 0 0 1 2 2.3l-1.3 7a2 2 0 0 1-2 1.7H7"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/>',
};

// What the pipeline is doing while we wait
const PHASES = [
  "Understanding your question",
  "Searching the knowledge base",
  "Ranking the best sources",
  "Writing your answer",
];

const iconSvg = (name) => `<svg class="icon" viewBox="0 0 24 24">${ICONS[name]}</svg>`;

const clock = (ts) =>
  new Date(ts || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const scrollDown = () => {
  streamEl.scrollTop = streamEl.scrollHeight;
};

// ---------- Saved chats (stored in this browser) ----------
const STORE_KEY = "aihas_chats_v1";
const MAX_CHATS = 30;
let chats = [];
let current = null; // the chat being shown, or null on the landing screen

try {
  chats = JSON.parse(localStorage.getItem(STORE_KEY) || "[]");
  if (!Array.isArray(chats)) chats = [];
} catch (e) {
  chats = [];
}

function save() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(chats));
  } catch (e) {}
}

function dayGroup(ts) {
  const d = new Date(ts);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return "Today";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return "Earlier";
}

function shortWhen(ts) {
  return dayGroup(ts) === "Earlier"
    ? new Date(ts).toLocaleDateString([], { month: "short", day: "numeric" })
    : clock(ts);
}

function renderHistory() {
  listEl.innerHTML = "";
  $("historyCount").textContent = chats.length;
  $("clearHistory").disabled = !chats.length;

  if (!chats.length) {
    const empty = document.createElement("div");
    empty.className = "h-empty";
    empty.innerHTML = '<span class="h-ico">' + iconSvg("chat") + "</span>";
    const msg = document.createElement("span");
    msg.textContent = "No chats yet. Ask something and it will show up here.";
    empty.appendChild(msg);
    listEl.appendChild(empty);
    return;
  }

  let lastGroup = "";
  chats.forEach((chat) => {
    const group = dayGroup(chat.ts);
    if (group !== lastGroup) {
      const label = document.createElement("div");
      label.className = "h-group";
      label.textContent = group;
      listEl.appendChild(label);
      lastGroup = group;
    }

    const item = document.createElement("div");
    item.className = "h-item" + (current && current.id === chat.id ? " active" : "");

    const open = document.createElement("button");
    open.type = "button";
    open.className = "h-open";

    const ico = document.createElement("span");
    ico.className = "h-ico";
    ico.innerHTML = iconSvg("chat");

    const txt = document.createElement("span");
    txt.className = "h-txt";
    const title = document.createElement("span");
    title.className = "h-title";
    title.textContent = chat.title;
    const time = document.createElement("span");
    time.className = "h-time";
    const q = chat.messages.filter((m) => m.role === "user").length;
    time.textContent = `${q} question${q === 1 ? "" : "s"} · ${shortWhen(chat.ts)}`;
    txt.append(title, time);

    open.append(ico, txt);
    open.onclick = () => openChat(chat.id);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "h-del";
    del.setAttribute("aria-label", "Delete chat");
    del.innerHTML = iconSvg("trash");
    del.onclick = (e) => {
      e.stopPropagation();
      deleteChat(chat.id);
    };

    item.append(open, del);
    listEl.appendChild(item);
  });
}

const openHistory = () => {
  renderHistory();
  historyEl.classList.add("open");
  scrimEl.classList.add("open");
};
const closeHistory = () => {
  historyEl.classList.remove("open");
  scrimEl.classList.remove("open");
  $("historyFoot").classList.remove("confirming");
};

function showLanding() {
  current = null;
  streamEl.innerHTML = "";
  document.body.classList.remove("chatting");
}

function newChat() {
  if (sendBtn.disabled) return;
  showLanding();
  renderHistory();
  closeHistory();
  inputEl.focus();
}

function openChat(id) {
  if (sendBtn.disabled) return;
  const chat = chats.find((c) => c.id === id);
  if (!chat) return;
  current = chat;
  streamEl.innerHTML = "";
  document.body.classList.add("chatting");
  chat.messages.forEach((m) => {
    if (m.role === "user") {
      addMsg("user", m.ts).bubble.textContent = m.text;
    } else {
      showBot(m.data, { instant: true, ts: m.ts });
    }
  });
  renderHistory();
  closeHistory();
  scrollDown();
}

function deleteChat(id) {
  if (sendBtn.disabled && current && current.id === id) return;
  chats = chats.filter((c) => c.id !== id);
  save();
  if (current && current.id === id) showLanding();
  renderHistory();
}

// ---------- Orb helpers ----------
function makeOrb(size) {
  const orb = document.createElement("div");
  orb.className = "orb";
  orb.style.setProperty("--s", size + "px");
  orb.innerHTML =
    '<div class="eyes"><i class="eye"><b class="pupil"></b></i><i class="eye"><b class="pupil"></b></i></div>';
  return orb;
}

function setThinking(on, extra) {
  [heroOrb, headOrb, extra].forEach((o) => o && o.classList.toggle("think", on));
  if (window.field) window.field.setMode(on ? "think" : "idle");
}

function react(name) {
  [heroOrb, headOrb].forEach((o) => {
    o.classList.remove("happy", "alert");
    void o.offsetWidth; // restart the animation
    o.classList.add(name);
    setTimeout(() => o.classList.remove(name), 800);
  });
}

// ---------- Theme ----------
$("theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("theme", next);
  } catch (e) {}
};

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

// ---------- Messages ----------
function addMsg(kind, ts) {
  const row = document.createElement("div");
  row.className = `msg ${kind}`;

  let avatar = null;
  if (kind !== "user") {
    avatar = makeOrb(34);
    row.appendChild(avatar);
  }

  const col = document.createElement("div");
  col.className = "col";

  const bubble = document.createElement("div");
  bubble.className = "bubble";

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${kind === "user" ? "You" : kind === "alert" ? "No match" : "Assistant"} · ${clock(ts)}`;

  col.append(bubble, meta);
  row.appendChild(col);
  streamEl.appendChild(row);
  scrollDown();
  return { row, bubble, avatar };
}

// Types the answer out; numbered lines become a tickable checklist.
// With opts.instant the text appears at once (used when reopening a saved chat).
function typeAnswer(bubble, text, onDone, opts) {
  const instant = !!(opts && opts.instant);
  const lines = text
    .replace(/\*\*/g, "")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  const total = lines.join("").length || 1;
  const delay = Math.max(3, Math.min(14, 2200 / total));
  let list = null;
  let i = 0;

  function nextLine() {
    if (i >= lines.length) {
      if (onDone) onDone();
      return;
    }
    const line = lines[i++];
    const numbered = line.match(/^\d+\.\s+(.*)/);
    let target;
    let content;

    if (numbered) {
      if (!list) {
        list = document.createElement("ol");
        const thisList = list;
        thisList.addEventListener("change", () => {
          const boxes = [...thisList.querySelectorAll("input")];
          if (boxes.length && boxes.every((b) => b.checked)) react("happy");
        });
        bubble.appendChild(list);
      }
      const li = document.createElement("li");
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      const box = document.createElement("span");
      box.className = "box";
      target = document.createElement("span");
      target.className = "txt";
      label.append(input, box, target);
      li.appendChild(label);
      list.appendChild(li);
      content = numbered[1];
    } else {
      list = null;
      target = document.createElement("p");
      bubble.appendChild(target);
      content = line;
    }

    if (instant) {
      target.textContent = content;
      nextLine();
      return;
    }

    target.classList.add("caret");
    let n = 0;
    (function tick() {
      target.textContent = content.slice(0, ++n);
      scrollDown();
      if (n < content.length) {
        setTimeout(tick, delay);
      } else {
        target.classList.remove("caret");
        setTimeout(nextLine, 90);
      }
    })();
  }
  nextLine();
}

// Relevance gauge + source chips
function addTrace(bubble, data) {
  const best = Math.min(...data.distances);
  const pct = Math.max(0, Math.min(100, Math.round(((0.5 - best) / 0.25) * 100)));

  const trace = document.createElement("div");
  trace.className = "trace";

  const gauge = document.createElement("div");
  gauge.className = "gauge" + (data.escalate ? " low" : "");
  gauge.innerHTML =
    '<svg viewBox="0 0 44 44"><circle class="track" cx="22" cy="22" r="19"/><circle class="fg" cx="22" cy="22" r="19"/></svg>';
  const num = document.createElement("b");
  num.textContent = data.escalate ? "low" : pct + "%";
  gauge.appendChild(num);

  const info = document.createElement("div");
  info.className = "t-info";
  const label = document.createElement("small");
  label.textContent = data.escalate ? "Below match threshold" : "Knowledge base match";
  info.appendChild(label);

  if (data.citations && data.citations.length) {
    const chips = document.createElement("div");
    chips.className = "chips";
    data.citations.forEach((name) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.innerHTML = iconSvg("file");
      chip.append(name);
      chips.appendChild(chip);
    });
    info.appendChild(chips);
  }

  trace.append(gauge, info);
  bubble.appendChild(trace);

  const fg = gauge.querySelector(".fg");
  setTimeout(() => {
    const shown = data.escalate ? 4 : pct;
    fg.style.strokeDashoffset = (119.4 * (1 - shown / 100)).toFixed(1);
  }, 80);
}

function addTicketButton(bubble) {
  const btn = document.createElement("button");
  btn.className = "ticket-btn";
  btn.type = "button";
  btn.innerHTML = iconSvg("ticket");
  btn.append("Create a ticket");
  btn.onclick = () => {
    btn.disabled = true;
    btn.innerHTML = iconSvg("check");
    btn.append("Ticket request noted (coming soon)");
  };
  bubble.appendChild(btn);
  scrollDown();
}

function addFeedback(bubble) {
  const fb = document.createElement("div");
  fb.className = "fb";
  const q = document.createElement("span");
  q.textContent = "Did this help?";

  const up = document.createElement("button");
  up.type = "button";
  up.setAttribute("aria-label", "Yes, helpful");
  up.innerHTML = iconSvg("thumb");

  const down = document.createElement("button");
  down.type = "button";
  down.className = "flip";
  down.setAttribute("aria-label", "No, not helpful");
  down.innerHTML = iconSvg("thumb");

  up.onclick = () => {
    up.classList.add("picked");
    down.remove();
    q.textContent = "Glad that helped!";
    react("happy");
  };
  down.onclick = () => {
    down.classList.add("picked");
    up.remove();
    q.textContent = "Sorry about that. Want a human to take a look?";
    addTicketButton(bubble);
  };

  fb.append(q, up, down);
  bubble.appendChild(fb);
  scrollDown();
}

function showBot(data, opts = {}) {
  const instant = !!opts.instant;
  const { bubble } = addMsg(data.escalate ? "alert" : "bot", opts.ts);
  if (!instant) react(data.escalate ? "alert" : "happy");
  typeAnswer(
    bubble,
    data.answer,
    () => {
      const hasTrace = data.distances && data.distances.length;
      if (hasTrace) addTrace(bubble, data);
      if (data.escalate) addTicketButton(bubble);
      else if (hasTrace && !instant) addFeedback(bubble);
      scrollDown();
    },
    { instant }
  );
}

// ---------- Sending a question ----------
async function ask(question) {
  document.body.classList.add("chatting");

  // start a new saved chat, or continue the one that is open
  if (!current) {
    current = {
      id: Date.now().toString(36),
      title: question.length > 48 ? question.slice(0, 47) + "…" : question,
      ts: Date.now(),
      messages: [],
    };
  }
  const chat = current;
  chat.messages.push({ role: "user", text: question, ts: Date.now() });
  chat.ts = Date.now();
  chats = chats.filter((c) => c !== chat);
  chats.unshift(chat);
  if (chats.length > MAX_CHATS) chats.length = MAX_CHATS;
  save();
  renderHistory();

  const user = addMsg("user");
  user.bubble.textContent = question;

  sendBtn.disabled = true;

  const { row, bubble, avatar } = addMsg("bot");
  setThinking(true, avatar);

  const phases = document.createElement("div");
  phases.className = "phases";
  const steps = PHASES.map((label) => {
    const s = document.createElement("div");
    s.className = "ph";
    s.innerHTML = "<i></i>";
    s.append(label);
    phases.appendChild(s);
    return s;
  });
  bubble.appendChild(phases);

  let p = 0;
  const paint = () => {
    steps.forEach((s, idx) => {
      s.classList.toggle("done", idx < p);
      s.classList.toggle("active", idx === p);
    });
    scrollDown();
  };
  paint();
  const timer = setInterval(() => {
    p = Math.min(p + 1, PHASES.length - 1);
    paint();
  }, 1400);

  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question }),
    });
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    clearInterval(timer);
    row.remove();
    chat.messages.push({ role: "bot", data, ts: Date.now() });
    save();
    showBot(data);
  } catch (err) {
    clearInterval(timer);
    row.remove();
    showBot({
      answer: "Sorry, something went wrong. Please try again.",
      citations: [],
      escalate: false,
    });
  } finally {
    sendBtn.disabled = false;
    setThinking(false);
    inputEl.focus();
  }
}

// ---------- Events ----------
$("form").onsubmit = (e) => {
  e.preventDefault();
  const question = inputEl.value.trim();
  if (question && !sendBtn.disabled) {
    inputEl.value = "";
    ask(question);
  }
};

$("reset").onclick = newChat;
$("newChat").onclick = newChat;
$("historyBtn").onclick = openHistory;
$("closeHistory").onclick = closeHistory;
scrimEl.onclick = closeHistory;

$("clearHistory").onclick = () => {
  if (sendBtn.disabled || !chats.length) return;
  $("historyFoot").classList.add("confirming");
};

$("clearCancel").onclick = () => $("historyFoot").classList.remove("confirming");

$("clearConfirm").onclick = () => {
  if (sendBtn.disabled) return;
  chats = [];
  save();
  showLanding();
  renderHistory();
  $("historyFoot").classList.remove("confirming");
};

// Suggestion cards: tilt + spotlight that follow the cursor
suggestEl.querySelectorAll(".s-card").forEach((card) => {
  card.onclick = () => {
    if (!sendBtn.disabled) ask(card.dataset.q);
  };

  card.addEventListener("pointermove", (e) => {
    const r = card.getBoundingClientRect();
    const x = e.clientX - r.left;
    const y = e.clientY - r.top;
    card.style.setProperty("--mx", x + "px");
    card.style.setProperty("--my", y + "px");
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    card.style.setProperty("--ry", ((x / r.width - 0.5) * 10).toFixed(2) + "deg");
    card.style.setProperty("--rx", ((0.5 - y / r.height) * 10).toFixed(2) + "deg");
  });

  card.addEventListener("pointerleave", () => {
    card.style.setProperty("--rx", "0deg");
    card.style.setProperty("--ry", "0deg");
  });
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeHistory();
  // Press "/" anywhere to jump to the input
  if (e.key === "/" && document.activeElement !== inputEl) {
    e.preventDefault();
    inputEl.focus();
  }
});

renderHistory();
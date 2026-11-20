const $ = (id) => document.getElementById(id);
let filter = "";

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

// ---------- Data ----------
async function loadStats() {
  const res = await fetch("/stats");
  const s = await res.json();

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
  const list = await res.json();
  const body = $("rows");
  body.innerHTML = "";

  if (!list.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">No tickets here yet.</td></tr>';
    return;
  }

  list.forEach((t) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(t.ref, "ref"),
      cell(t.summary),
      cell(tag(t.priority)),
      cell(tag(t.status)),
      cell(t.requester || ""),
      cell(
        new Date(t.created_at).toLocaleString([], {
          dateStyle: "medium",
          timeStyle: "short",
        }),
        "when"
      )
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

function refresh() {
  loadStats().catch(() => {});
  loadTickets().catch(() => {
    $("rows").innerHTML =
      '<tr><td colspan="7" class="empty">Could not load tickets.</td></tr>';
  });
}

// ---------- Events ----------
$("filters").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    filter = b.dataset.s;
    $("filters")
      .querySelectorAll("button")
      .forEach((x) => x.classList.toggle("on", x === b));
    loadTickets();
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
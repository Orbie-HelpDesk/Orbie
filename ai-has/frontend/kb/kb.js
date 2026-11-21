const $ = (id) => document.getElementById(id);

const NAME_RE = /^[a-z0-9][a-z0-9-]{0,80}\.md$/;
const JSON_HEADERS = { "Content-Type": "application/json" };

let info = { max_bytes: 60000, max_articles: 200, max_distance: 0.5, editable: true };
let articles = [];
let current = null; // { source, isNew, status }
let original = ""; // the text as it was when loaded or saved
let busy = false;
let pendingOpen = null; // an article the admin wants to open while there are unsaved changes

// ---------- Helpers ----------
function say(el, text, kind) {
  el.textContent = text || "";
  el.className = el.className.replace(/\b(ok|error)\b/g, "").trim() + (kind ? " " + kind : "");
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: JSON_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Something went wrong. Please try again.";
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return data;
}

const byteLength = (text) => new TextEncoder().encode(text).length;
const isDirty = () => current !== null && $("content").value !== original;

function setBusy(on) {
  busy = on;
  $("saveBtn").disabled = on || !info.editable;
  $("deleteBtn").disabled = on || !info.editable || (current && current.isNew);
  $("newBtn").disabled = on;
}

// ---------- The list ----------
function renderList() {
  const list = $("list");
  list.innerHTML = "";
  $("count").textContent = articles.length ? "(" + articles.length + ")" : "";
  if (!articles.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No articles yet.";
    list.appendChild(empty);
    return;
  }
  articles.forEach((a) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "kb-item" + (current && current.source === a.source ? " active" : "");
    item.setAttribute("role", "listitem");
    const title = document.createElement("b");
    title.textContent = a.title || a.source;
    const meta = document.createElement("small");
    const name = document.createElement("span");
    name.textContent = a.source;
    const tag = document.createElement("span");
    tag.className = "tag " + a.status;
    tag.textContent = a.status.replace("_", " ");
    const chunks = document.createElement("span");
    chunks.textContent = a.chunks + (a.chunks === 1 ? " chunk" : " chunks");
    meta.append(name, tag, chunks);
    item.append(title, meta);
    item.onclick = () => openArticle(a.source);
    list.appendChild(item);
  });
}

async function loadList() {
  articles = await api("GET", "/kb");
  renderList();
}

// ---------- The editor ----------
function showEditor() {
  $("editor").hidden = false;
  $("confirmDelete").hidden = true;
  $("confirmDiscard").hidden = true;
}

function updateMeter() {
  const used = byteLength($("content").value);
  const meter = $("bytes");
  meter.textContent = used.toLocaleString() + " of " + info.max_bytes.toLocaleString() + " bytes";
  meter.className = used > info.max_bytes ? "over" : "";
  $("dirty").hidden = !isDirty();
}

function setStatusTag() {
  const tag = $("statusTag");
  if (!current) {
    tag.hidden = true;
    return;
  }
  tag.hidden = false;
  tag.className = "tag " + (current.isNew ? "new" : current.status);
  tag.textContent = current.isNew ? "new" : current.status.replace("_", " ");
}

function fill(source, content, isNew, status) {
  current = { source, isNew, status };
  original = content;
  $("fileName").value = source;
  $("fileName").readOnly = !isNew;
  $("content").value = content;
  $("editorTitle").textContent = isNew ? "New article" : source;
  say($("message"), "");
  say($("testMessage"), "");
  $("results").innerHTML = "";
  showEditor();
  setStatusTag();
  updateMeter();
  setBusy(false);
  renderList();
}

async function openArticle(source, force) {
  if (busy) return;
  if (!force && isDirty()) {
    pendingOpen = source;
    $("confirmDelete").hidden = true;
    $("confirmDiscard").hidden = false;
    return;
  }
  const listed = articles.find((a) => a.source === source);
  try {
    const data = await api("GET", "/kb/" + encodeURIComponent(source));
    fill(source, data.content, false, listed ? listed.status : "indexed");
  } catch (err) {
    if (err.status === 404 && listed && listed.status === "file_missing") {
      // The index has this article but its file is gone. Saving new text creates the file again.
      fill(source, "# " + (listed.title || "Title") + "\n\n", false, "file_missing");
      say($("message"), "The file for this article is missing on disk. Write the text again and save to restore it, or delete it.", "error");
    } else {
      showEditor();
      say($("message"), err.message, "error");
    }
  }
}

function newArticle() {
  if (busy) return;
  if (isDirty()) {
    pendingOpen = "";
    $("confirmDiscard").hidden = false;
    return;
  }
  fill("", "# Title\n\nWrite the article here.\n", true, "new");
  $("fileName").focus();
}

async function save() {
  if (busy || !current) return;
  const name = $("fileName").value.trim();
  const content = $("content").value;
  if (!NAME_RE.test(name)) {
    say($("message"), "Use a file name with lowercase letters, digits and dashes, ending in .md.", "error");
    return;
  }
  if (byteLength(content) > info.max_bytes) {
    say($("message"), "The article is too long. The limit is " + info.max_bytes.toLocaleString() + " bytes.", "error");
    return;
  }
  setBusy(true);
  say($("message"), "Saving and re-indexing. This can take a few seconds.");
  try {
    const result = await api("PUT", "/kb/" + encodeURIComponent(name), { content });
    await loadList();
    const saved = await api("GET", "/kb/" + encodeURIComponent(name));
    fill(name, saved.content, false, "indexed");
    say(
      $("message"),
      (result.created ? "Created" : "Saved") + " and indexed in " + result.chunks + (result.chunks === 1 ? " chunk." : " chunks."),
      "ok"
    );
  } catch (err) {
    setBusy(false);
    say($("message"), err.message, "error");
  }
}

async function remove() {
  if (busy || !current || current.isNew) return;
  const name = current.source;
  setBusy(true);
  try {
    await api("DELETE", "/kb/" + encodeURIComponent(name), {});
    current = null;
    original = "";
    $("editor").hidden = true;
    $("editorTitle").textContent = "Pick an article or start a new one";
    setStatusTag();
    await loadList();
    say($("banner"), name + " was deleted.");
    $("banner").hidden = false;
  } catch (err) {
    setBusy(false);
    say($("message"), err.message, "error");
  }
  setBusy(false);
}

// ---------- Test retrieval ----------
function renderResults(data) {
  const box = $("results");
  box.innerHTML = "";

  const summary = document.createElement("div");
  const max = data.max_distance;
  if (data.best_distance === null) {
    summary.className = "kb-summary bad";
    summary.textContent = "Nothing came back. The index may be empty.";
  } else if (data.would_answer) {
    summary.className = "kb-summary good";
    summary.textContent =
      "Best distance " + data.best_distance.toFixed(2) + ", under the " + max.toFixed(2) + " limit, so the chat would answer.";
  } else {
    summary.className = "kb-summary bad";
    summary.textContent =
      "Best distance " + data.best_distance.toFixed(2) + ", over the " + max.toFixed(2) + " limit, so the chat would say it could not find an answer.";
  }
  if (current && !current.isNew) {
    summary.textContent += data.this_article_found
      ? " This article is among the results."
      : " This article is not among the results.";
  }
  box.appendChild(summary);

  const results = document.createElement("div");
  results.className = "kb-results";
  data.chunks.forEach((c) => {
    const card = document.createElement("div");
    card.className = "kb-chunk" + (c.from_this_article ? " mine" : "");
    const head = document.createElement("div");
    head.className = "kb-chunk-head";
    const name = document.createElement("b");
    name.textContent = c.source;
    const dist = document.createElement("span");
    dist.textContent = "distance " + c.distance.toFixed(3) + (c.from_this_article ? " · this article" : "");
    head.append(name, dist);

    const bar = document.createElement("div");
    bar.className = "kb-bar" + (c.distance > max ? " far" : "");
    const fill = document.createElement("i");
    fill.style.width = Math.max(4, Math.min(100, (1 - c.distance) * 100)) + "%";
    const mark = document.createElement("u");
    mark.style.left = (1 - max) * 100 + "%"; // the limit: a chunk whose bar reaches it is close enough
    bar.append(fill, mark);

    const snippet = document.createElement("div");
    snippet.className = "kb-snippet";
    snippet.textContent = c.snippet;
    card.append(head, bar, snippet);
    results.appendChild(card);
  });
  box.appendChild(results);
}

async function runTest(e) {
  e.preventDefault();
  const question = $("question").value.trim();
  if (question.length < 2) {
    say($("testMessage"), "Type a question first.", "error");
    return;
  }
  $("testBtn").disabled = true;
  say($("testMessage"), "Searching...");
  try {
    const body = { question };
    if (current && !current.isNew) body.source = current.source;
    const data = await api("POST", "/kb/test", body);
    say($("testMessage"), "");
    renderResults(data);
  } catch (err) {
    $("results").innerHTML = "";
    say($("testMessage"), err.message, "error");
  }
  $("testBtn").disabled = false;
}

// ---------- Events ----------
$("newBtn").onclick = newArticle;
$("refresh").onclick = () => loadList().catch((err) => say($("banner"), err.message) || ($("banner").hidden = false));
$("saveBtn").onclick = save;
$("content").addEventListener("input", updateMeter);
$("deleteBtn").onclick = () => {
  if (!current || current.isNew) return;
  $("confirmName").textContent = current.source;
  $("confirmDiscard").hidden = true;
  $("confirmDelete").hidden = false;
};
$("confirmNo").onclick = () => ($("confirmDelete").hidden = true);
$("confirmYes").onclick = () => {
  $("confirmDelete").hidden = true;
  remove();
};
$("discardNo").onclick = () => {
  pendingOpen = null;
  $("confirmDiscard").hidden = true;
};
$("discardYes").onclick = () => {
  const target = pendingOpen;
  pendingOpen = null;
  $("confirmDiscard").hidden = true;
  if (target === "") fill("", "# Title\n\nWrite the article here.\n", true, "new");
  else if (target) openArticle(target, true);
};
$("testForm").addEventListener("submit", runTest);
$("theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("theme", next);
  } catch (e) {}
};
window.addEventListener("beforeunload", (e) => {
  if (isDirty()) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// ---------- Start ----------
(async () => {
  try {
    info = await api("GET", "/kb/info");
  } catch (err) {
    // keep the defaults: the server checks every limit anyway
  }
  if (!info.editable) {
    say(
      $("banner"),
      "Changes are switched off for this database, so the page is read only. Only the team lead should enable KB_EDIT_ALLOW_REMOTE on a shared database."
    );
    $("banner").hidden = false;
    $("newBtn").disabled = true;
  }
  try {
    await loadList();
  } catch (err) {
    say($("banner"), err.message);
    $("banner").hidden = false;
  }
})();

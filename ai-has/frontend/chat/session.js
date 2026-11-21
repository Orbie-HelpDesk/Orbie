// Shared by the chat and the admin page. Loaded before the page script, which is named in
// data-then and is only started once we know who is logged in.
//  - asks /auth/me, and sends the visitor to the login page if there is no session
//  - fills the header with the name, the role and a Log out button
//  - any later 401 (for example an expired session) also goes to the login page
(() => {
  const current = document.currentScript;
  const then = current && current.dataset.then;

  const goToLogin = () => {
    const here = location.pathname + location.search;
    location.replace(here === "/" ? "/login" : "/login?next=" + encodeURIComponent(here));
  };

  const realFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const res = await realFetch(...args);
    const url = typeof args[0] === "string" ? args[0] : args[0].url;
    if (res.status === 401 && !url.includes("/auth/login")) goToLogin();
    return res;
  };

  const ICON_LOGOUT =
    '<svg class="icon" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/></svg>';

  function renderUser(user) {
    const slot = document.getElementById("userSlot");
    if (slot) {
      const chip = document.createElement("div");
      chip.className = "user-chip";
      chip.title = user.email;
      const name = document.createElement("span");
      name.className = "user-name";
      name.textContent = user.name;
      const role = document.createElement("span");
      role.className = "role-badge role-" + user.role;
      role.textContent = user.role;
      chip.append(name, role);

      const out = document.createElement("button");
      out.type = "button";
      out.className = "ib";
      out.id = "logout";
      out.title = "Log out";
      out.setAttribute("aria-label", "Log out");
      out.innerHTML = ICON_LOGOUT;
      out.onclick = async () => {
        out.disabled = true;
        try {
          await realFetch("/auth/logout", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          });
        } catch (e) {}
        location.replace("/login");
      };
      slot.append(chip, out);
    }
    const adminLink = document.getElementById("adminLink");
    if (adminLink && (user.role === "agent" || user.role === "admin")) adminLink.hidden = false;
  }

  function start() {
    if (!then) return;
    const s = document.createElement("script");
    s.src = then;
    document.body.appendChild(s);
  }

  realFetch("/auth/me", { cache: "no-store" })
    .then(async (res) => {
      if (res.status === 401) return goToLogin();
      if (res.ok) {
        window.aihasUser = await res.json();
        renderUser(window.aihasUser);
      }
      start();
    })
    .catch(start); // server not reachable: the page shows its own offline state
})();

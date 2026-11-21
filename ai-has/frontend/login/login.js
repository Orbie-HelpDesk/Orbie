const $ = (id) => document.getElementById(id);

// Only a path on this site is allowed, so a crafted link cannot send the user elsewhere after login
function safeNext(value) {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return "/";
  return value;
}
const next = safeNext(new URLSearchParams(location.search).get("next"));

const form = $("loginForm");
const errorEl = $("error");
const submitBtn = $("submit");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.textContent = "";
  const email = $("email").value.trim();
  const password = $("password").value;
  if (!email || !password) {
    errorEl.textContent = "Please enter your email and password.";
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = "Signing in...";
  try {
    const res = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (res.ok) {
      location.replace(next);
      return;
    }
    const data = await res.json().catch(() => ({}));
    errorEl.textContent =
      typeof data.detail === "string" ? data.detail : "Could not sign in. Please try again.";
    $("password").value = "";
    $("password").focus();
  } catch (err) {
    errorEl.textContent = "Could not reach the server. Please try again.";
  }
  submitBtn.disabled = false;
  submitBtn.textContent = "Sign in";
});

$("theme").onclick = () => {
  const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = nextTheme;
  try {
    localStorage.setItem("theme", nextTheme);
  } catch (e) {}
};

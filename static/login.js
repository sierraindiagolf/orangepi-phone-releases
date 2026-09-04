// Sign-in and SMS password reset. Deliberately standalone: app.js assumes an
// authenticated session and would redirect back here on its first poll.

const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  try {
    const res = await fetch(path, opts);
    let data;
    try { data = await res.json(); } catch { data = {}; }
    return { status: res.status, data };
  } catch {
    return { status: 0, data: { success: false, error: "backend unreachable" } };
  }
}

function msg(text, kind) {
  const el = $("l-msg");
  el.textContent = text || "";
  el.className = "loginmsg" + (kind ? " " + kind : "");
}

let firstRun = false;

async function refreshState() {
  const { data } = await api("GET", "/api/auth/state");
  if (data.authenticated) { location.href = "/"; return; }
  firstRun = data.configured === false;
  $("setup-note").classList.toggle("hidden", !firstRun);
  $("btn-login").textContent = firstRun ? "Set password and sign in" : "Sign in";
  // Nothing to reset to if no recovery number is stored, and the reset endpoint
  // deliberately will not tell us which it is once a password exists.
  $("link-forgot").classList.toggle("hidden", firstRun);
  if (firstRun) $("l-pass").setAttribute("autocomplete", "new-password");
  if (data.locked) msg("Too many failed attempts — locked briefly.", "bad");
}

async function doLogin() {
  const password = $("l-pass").value;
  if (!password) { msg("Enter the password.", "bad"); return; }
  msg("…");
  const { data } = await api("POST", "/api/auth/login", {
    username: $("l-user").value.trim(), password,
  });
  if (data.success) { location.href = "/"; return; }
  msg(data.error || "Sign in failed.", "bad");
}

async function requestReset() {
  msg("…");
  const { data } = await api("POST", "/api/auth/reset/request", {});
  if (!data.success) { msg(data.error || "Could not start a reset.", "bad"); return; }
  $("form-login").classList.add("hidden");
  $("form-reset").classList.remove("hidden");
  msg("");
}

async function doReset() {
  const { data } = await api("POST", "/api/auth/reset/confirm", {
    code: $("r-code").value.trim(),
    new_password: $("r-pass").value,
  });
  if (data.success) {
    $("form-reset").classList.add("hidden");
    $("form-login").classList.remove("hidden");
    msg("Password changed — sign in with it now.", "ok");
    return;
  }
  msg(data.error || "Reset failed.", "bad");
}

$("btn-login").onclick = doLogin;
$("btn-reset").onclick = doReset;
$("link-forgot").onclick = (e) => { e.preventDefault(); requestReset(); };
$("link-back").onclick = (e) => {
  e.preventDefault();
  $("form-reset").classList.add("hidden");
  $("form-login").classList.remove("hidden");
  msg("");
};
for (const id of ["l-pass", "l-user"]) {
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
}
$("r-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") doReset(); });

refreshState();

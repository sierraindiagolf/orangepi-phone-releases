"use strict";

// --- tiny helpers ----------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    // The backend restarts whenever the modem is reset — which this page can
    // trigger itself. Return a failed result rather than throwing, so the
    // pollers degrade quietly instead of filling the console with unhandled
    // rejections.
    return { status: 0, data: { success: false, error: "backend unreachable" } };
  }
  // The session expired, or the service was restarted with a new secret. Every
  // poller on this page would otherwise spin silently against a wall.
  if (res.status === 401) {
    location.href = "/login";
    return { status: 401, data: { success: false, error: "not signed in" } };
  }
  let data;
  try { data = await res.json(); } catch { data = { raw: await res.text() }; }
  return { status: res.status, data };
}

function say(el, message, isError) {
  const node = typeof el === "string" ? $(el) : el;
  node.textContent = message || "";
  node.className = "result" + (message ? (isError ? " err" : " ok") : "");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function shortTime(iso) {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  if (isNaN(d)) return iso;
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], {
        day: "2-digit", month: "2-digit",
        hour: "2-digit", minute: "2-digit",
      });
}

function label(number, name) {
  return name ? `${name} (${number})` : number;
}

// --- tabs ------------------------------------------------------------------

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("#tab-" + tab.dataset.tab).classList.add("active");
    const loaders = {
      dialer: () => { loadCalls(); loadAudio(); }, sms: loadThreads, ussd: loadUssdHistory,
      phonebook: loadContacts, at: loadAtHistory, api: loadSpec,
      carrier: () => { loadCarrier(); loadVoicemail(); loadNtp(); },
      access: () => { loadAccount(); loadTokens(); loadFirmwareCheck(); },
    };
    (loaders[tab.dataset.tab] || (() => {}))();
  });
});

// ===================== STATUS =====================

async function loadStatus() {
  const { data } = await api("GET", "/api/status");
  if (!data.success) return;
  const m = data.modem || {};
  $("#conn-dot").className = "dot" + (m.connected ? " on" : "");
  $("#brand-sub").textContent = [m.model, m.firmware].filter(Boolean).join(" · ")
    || "Quectel EC25-E";

  const bits = [];
  const add = (k, v) => v != null && v !== "" && bits.push(
    `<span class="item">${k} <b>${esc(v)}</b></span>`);
  add("Operator", m.operator);
  add("Tech", m.access_tech);
  if (m.network) add("Band", m.network.band);
  if (m.rssi_dbm != null) add("Signal", `${m.rssi_dbm} dBm (${m.signal_percent}%)`);
  add("SIM", m.sim);
  add("Reg", m.registration);
  if (data.lte && data.lte.ip) add("LTE IP", data.lte.ip);
  if (data.db) add("Msgs", data.db.sms);
  $("#statusstrip").innerHTML = bits.join("");

  const active = (m.activity && m.activity.code === 4) || m.active_call;
  const ringing = m.activity && m.activity.code === 3;
  const bar = $("#callbar");
  if (active || ringing) {
    bar.classList.remove("hidden");
    const num = m.active_call ? m.active_call.number : "";
    $("#callbar-text").textContent =
      (ringing ? "Incoming call " : "Call in progress ") + (num || "");
    $("#callbar-answer").classList.toggle("hidden", !ringing);
    $("#dtmf-box").classList.remove("hidden");
  } else {
    bar.classList.add("hidden");
    $("#dtmf-box").classList.add("hidden");
  }
}

// ===================== VOICE / SMS MODE =====================
//
// VoLTE and SMS now work at the same time (the SMS domain is pinned to CS at NV
// level), so switching is no longer part of normal use. The control is kept as an
// escape hatch: voice mode gives VoLTE calls, SMS mode falls back to CSFB.

let currentMode = null;
let modeBusy = false;

const MODE_TEXT = {
  voice: "VoLTE — calls stay on LTE and ring in 3-6s; SMS works too",
  sms: "IMS off — SMS works; calls fall back to CSFB and ring in 6.5-12s",
};

function renderMode(mode, ready) {
  currentMode = mode;
  const pill = $("#mode-pill");
  pill.textContent = mode
    ? (mode === "voice" ? (ready ? "Voice" : "Voice (IMS registering)") : "SMS")
    : "unknown";
  pill.className = "modepill " + (mode || "busy");
  pill.title = MODE_TEXT[mode] || "";
  $("#btn-mode-toggle").textContent =
    mode === "voice" ? "Switch to SMS" : "Switch to voice";
}

async function loadMode() {
  if (modeBusy) return;
  const { data } = await api("GET", "/api/modem/mode");
  if (data.success) renderMode(data.mode, data.ims_registered === 1);
}

function modeBanner(show, text) {
  $("#modebanner").classList.toggle("hidden", !show);
  if (text) $("#modebanner-text").textContent = text;
}

async function switchMode(target) {
  modeBusy = true;
  $("#btn-mode-toggle").disabled = true;
  renderMode(null);
  modeBanner(true, `Switching to ${target} mode — this resets the modem and takes about 40 seconds…`);
  const { data } = await api("POST", "/api/modem/mode", { mode: target });
  modeBusy = false;
  $("#btn-mode-toggle").disabled = false;
  modeBanner(false);
  if (!data.success) {
    modeBanner(true, "Mode switch failed: " + data.error);
    setTimeout(() => modeBanner(false), 6000);
  }
  loadMode();
  loadStatus();
}

$("#btn-mode-toggle").addEventListener("click", () => {
  if (modeBusy) return;
  switchMode(currentMode === "voice" ? "sms" : "voice");
});

let lastSeq = 0;
async function pollEvents() {
  try {
    const { data } = await api("GET", "/api/events?since=" + lastSeq);
    if (!data.success) return;
    lastSeq = data.latest;
    let refreshSms = false, refreshCalls = false;
    for (const ev of data.events || []) {
      if (ev.kind === "sms_imported" || ev.kind === "sms_stored") refreshSms = true;
      if (ev.kind === "call_ended" || ev.kind === "incoming_call") refreshCalls = true;
    }
    if (refreshSms) { loadThreads(); if (currentThread) openThread(currentThread); }
    if (refreshCalls) loadCalls();
  } catch { /* transient */ }
}

// ===================== DIALER =====================

$$(".keypad button").forEach((b) => {
  b.addEventListener("click", () => {
    const field = $("#dial-number");
    const key = b.dataset.key;
    if (key === "back") field.value = field.value.slice(0, -1);
    else if (key === "clear") field.value = "";
    else field.value += (key || b.textContent);
  });
});

$("#btn-dial").addEventListener("click", async () => {
  const number = $("#dial-number").value.trim();
  if (!number) return say("#dial-result", "Enter a number first", true);
  say("#dial-result", "Dialing…");
  const { data } = await api("POST", "/api/call/dial", {
    number,
    notes: $("#dial-notes").value || null,
    record: $("#opt-record").checked,
    play: $("#opt-play").value || null,
  });
  if (data.success) {
    say("#dial-result", `Calling ${data.number} (register #${data.call_id})`);
    loadStatus(); loadCalls();
  } else {
    say("#dial-result", data.error, true);
    loadCalls();
  }
});

$("#btn-hangup").addEventListener("click", hangup);
$("#callbar-hangup").addEventListener("click", hangup);
async function hangup() {
  const { data } = await api("POST", "/api/call/hangup");
  say("#dial-result", data.success
    ? `Call ended after ${data.duration_seconds}s` : data.error, !data.success);
  loadStatus(); loadCalls();
}

$("#callbar-answer").addEventListener("click", async () => {
  const { data } = await api("POST", "/api/call/answer");
  if (!data.success) say("#dial-result", data.error, true);
  loadStatus();
});

$("#btn-dtmf").addEventListener("click", async () => {
  const digits = $("#dtmf-digits").value.trim();
  if (!digits) return;
  const { data } = await api("POST", "/api/call/dtmf", { digits });
  say("#dial-result", data.success ? `Sent DTMF ${digits}` : data.error,
    !data.success);
});

// ===================== CALL AUDIO =====================

async function loadAudio() {
  const { data } = await api("GET", "/api/audio");
  if (!data.success) return;
  const pill = $("#audio-state");
  if (!data.device) {
    pill.textContent = "no sound card";
    pill.className = "modepill busy";
  } else if (data.recording) {
    pill.textContent = "recording " + (data.current_file || "");
    pill.className = "modepill sms";
  } else if (data.playing) {
    pill.textContent = "injecting " + (data.playing_file || "");
    pill.className = "modepill voice";
  } else {
    const m = data.modem || {};
    pill.textContent = m.routed_to_usb ? "ready" : "not routed to USB";
    pill.className = "modepill " + (m.routed_to_usb ? "voice" : "busy");
  }

  const { data: files } = await api("GET", "/api/audio/files");
  if (files.success) {
    // Only clips with actual audio are worth injecting.
    const opts = files.files
      .filter((f) => f.has_audio)
      .map((f) => `<option value="${esc(f.file)}">${esc(f.file)} (${f.seconds}s)</option>`)
      .join("");
    const keepA = $("#inject-file").value, keepB = $("#opt-play").value;
    $("#inject-file").innerHTML = opts || `<option value="">no clips available</option>`;
    $("#opt-play").innerHTML = `<option value="">No audio to inject</option>` + opts;
    if (keepA) $("#inject-file").value = keepA;
    if (keepB) $("#opt-play").value = keepB;
  }

  const { data: recs } = await api("GET", "/api/recordings");
  const body = $("#rec-table tbody");
  if (!recs.success || !recs.recordings.length) {
    body.innerHTML = `<tr><td colspan="4">No recordings yet.</td></tr>`;
    return;
  }
  body.innerHTML = recs.recordings.map((r) => `
    <tr>
      <td>${esc(r.file)}</td>
      <td>${r.seconds ?? "?"}s</td>
      <td>${r.has_audio
        ? `<span class="tag in">audio</span>`
        : `<span class="tag">silent</span>`} rms=${r.rms ?? "?"}</td>
      <td>
        <a class="btn small" href="${esc(r.url)}" download>Download</a>
        <button class="btn small danger" data-del="${esc(r.file)}">Del</button>
      </td>
    </tr>`).join("");
  $$("#rec-table [data-del]").forEach((b) => {
    b.addEventListener("click", async () => {
      if (!confirm("Delete " + b.dataset.del + "?")) return;
      await api("DELETE", "/api/recordings/" + encodeURIComponent(b.dataset.del));
      loadAudio();
    });
  });
}

$("#btn-rec-start").addEventListener("click", async () => {
  const { data } = await api("POST", "/api/audio/record", { tag: "manual" });
  say("#audio-result", data.success ? "Recording to " + data.path : data.error,
    !data.success);
  loadAudio();
});

$("#btn-rec-stop").addEventListener("click", async () => {
  const { data } = await api("DELETE", "/api/audio/record");
  say("#audio-result", data.success
    ? `Stopped: ${data.file} — ${data.seconds}s, rms ${data.rms}${data.has_audio ? "" : " (silent)"}`
    : data.error, !data.success);
  loadAudio();
});

$("#btn-inject").addEventListener("click", async () => {
  const file = $("#inject-file").value;
  if (!file) return say("#audio-result", "No clip selected", true);
  const { data } = await api("POST", "/api/audio/play", { file });
  say("#audio-result", data.success
    ? `Injecting ${data.file}${data.converted ? " (converted to 8 kHz)" : ""}`
    : data.error, !data.success);
  loadAudio();
});

$("#btn-inject-stop").addEventListener("click", async () => {
  const { data } = await api("DELETE", "/api/audio/play");
  say("#audio-result", data.success ? "Playback stopped" : data.error,
    !data.success);
  loadAudio();
});

async function loadCalls() {
  const params = new URLSearchParams({ limit: 200 });
  if ($("#calls-search").value) params.set("search", $("#calls-search").value);
  if ($("#calls-direction").value) params.set("direction", $("#calls-direction").value);
  const { data } = await api("GET", "/api/call/register?" + params);
  const body = $("#calls-table tbody");
  if (!data.success || !data.calls.length) {
    body.innerHTML = `<tr><td colspan="6" class="wrap">No calls logged.</td></tr>`;
    return;
  }
  body.innerHTML = data.calls.map((c) => `
    <tr>
      <td>${esc(shortTime(c.ts))}</td>
      <td>${esc(label(c.number, c.name))}</td>
      <td><span class="tag ${c.direction === "OUTGOING" ? "out" : "in"}">${
        c.direction === "OUTGOING" ? "OUT" : "IN"}</span></td>
      <td><span class="tag ${/FAIL|BUSY|NO /i.test(c.status) ? "bad" : ""}">${
        esc(c.status)}</span></td>
      <td>${c.duration ? c.duration + "s" : "—"}</td>
      <td class="wrap">${esc(c.notes || "")}</td>
    </tr>`).join("");
}

$("#btn-calls-refresh").addEventListener("click", loadCalls);
$("#calls-search").addEventListener("input", () => {
  clearTimeout(window._cs); window._cs = setTimeout(loadCalls, 300);
});
$("#calls-direction").addEventListener("change", loadCalls);
$("#btn-calls-clear").addEventListener("click", async () => {
  if (!confirm("Clear the entire call register?")) return;
  await api("DELETE", "/api/call/register");
  loadCalls();
});

// ===================== MESSAGES =====================

let currentThread = null;

async function loadThreads() {
  const { data } = await api("GET", "/api/sms/conversations");
  const list = $("#thread-list");
  const badge = $("#sms-badge");
  if (data.unread) {
    badge.textContent = data.unread;
    badge.classList.remove("hidden");
  } else badge.classList.add("hidden");

  if (!data.success || !data.conversations.length) {
    list.innerHTML = `<p class="empty" style="padding:16px">No conversations yet.</p>`;
    return;
  }
  list.innerHTML = data.conversations.map((c) => `
    <div class="thread ${c.number === currentThread ? "active" : ""}"
         data-number="${esc(c.number)}">
      <div class="who">
        <b>${esc(c.name || c.number)}</b>
        <span class="when">${esc(shortTime(c.last_ts))}</span>
      </div>
      <div class="prev">${c.last_direction === "OUT" ? "You: " : ""}${
        esc(c.last_body)}</div>
      ${c.unread ? `<span class="unread">${c.unread} new</span>` : ""}
    </div>`).join("");

  $$("#thread-list .thread").forEach((el) => {
    el.addEventListener("click", () => openThread(el.dataset.number));
  });
}

async function openThread(number) {
  currentThread = number;
  $$("#thread-list .thread").forEach((el) =>
    el.classList.toggle("active", el.dataset.number === number));

  const { data } = await api("GET",
    "/api/sms/conversation?number=" + encodeURIComponent(number));
  if (!data.success) return;

  $("#conv-title").textContent = data.name || data.number;
  $("#conv-sub").textContent = data.name ? data.number : "";
  $("#compose-to").value = data.number;

  const box = $("#bubbles");
  box.innerHTML = data.messages.length
    ? data.messages.map((m) => `
        <div class="bubble ${m.direction === "OUT" ? "out" : "in"} ${
          m.status === "FAILED" ? "failed" : ""}">${esc(m.body)}
          <span class="stamp">${esc(shortTime(m.ts))}${
            m.status === "FAILED" ? " · failed: " + esc(m.error || "") : ""}</span>
        </div>`).join("")
    : `<p class="empty">No messages yet with this number.</p>`;
  box.scrollTop = box.scrollHeight;
  loadThreads();
}

$("#btn-new-thread").addEventListener("click", () => {
  currentThread = null;
  $$("#thread-list .thread").forEach((el) => el.classList.remove("active"));
  $("#conv-title").textContent = "New message";
  $("#conv-sub").textContent = "";
  $("#compose-to").value = "";
  $("#compose-body").value = "";
  $("#bubbles").innerHTML =
    `<p class="empty">Enter a recipient below and send.</p>`;
  $("#compose-to").focus();
});

$("#btn-sms-sync").addEventListener("click", async () => {
  const { data } = await api("POST", "/api/sms/sync");
  say("#send-result", data.success
    ? `Imported ${data.imported} message(s) from the modem`
    : data.error, !data.success);
  loadThreads();
});

$("#compose-body").addEventListener("input", () => {
  const text = $("#compose-body").value;
  $("#compose-count").textContent = `${text.length} chars`;
  // Non-GSM-7 content has to go out as UCS2, which halves the per-part limit.
  const unicode = /[^\x00-\x7F£¥èéùìòÇ]/.test(text);
  $("#compose-enc").textContent = text
    ? (unicode ? "UCS2 (70 chars/part)" : "GSM-7 (160 chars/part)") : "";
});

$("#btn-send-sms").addEventListener("click", async () => {
  const to = $("#compose-to").value.trim();
  const message = $("#compose-body").value;
  if (!to) return say("#send-result", "Enter a recipient", true);
  if (!message) return say("#send-result", "Enter a message", true);

  const numbers = to.split(",").map((s) => s.trim()).filter(Boolean);
  const btn = $("#btn-send-sms");
  btn.disabled = true;

  // Sending from voice mode has to switch the modem and switch back, which
  // resets it twice — around 75 s in total. Say so rather than looking hung.
  say("#send-result", "Sending…");
  const willSwitch = false;
  const { data } = await api("POST", "/api/sms/send", { numbers, message });
  btn.disabled = false;
  modeBanner(false);
  if (willSwitch) loadMode();

  const results = data.results || [];
  const good = results.filter((r) => r.success).length;
  if (data.success) {
    say("#send-result", `Sent to ${good}/${results.length}`);
    $("#compose-body").value = "";
    $("#compose-body").dispatchEvent(new Event("input"));
  } else {
    say("#send-result", results.length
      ? results.map((r) => `${r.number}: ${r.error}`).join(" · ")
      : (data.error || "Send failed"), true);
  }
  loadThreads();
  if (numbers.length === 1) openThread(numbers[0].startsWith("+")
    ? numbers[0] : (results[0] && results[0].number) || numbers[0]);
});

$("#btn-conv-call").addEventListener("click", () => {
  if (!currentThread) return;
  $("#dial-number").value = currentThread;
  $$(".tab").find((t) => t.dataset.tab === "dialer").click();
});

$("#btn-conv-delete").addEventListener("click", async () => {
  if (!currentThread) return;
  if (!confirm("Delete this conversation?")) return;
  await api("DELETE",
    "/api/sms/conversation?number=" + encodeURIComponent(currentThread));
  currentThread = null;
  $("#bubbles").innerHTML = `<p class="empty">Conversation deleted.</p>`;
  $("#conv-title").textContent = "Select a conversation";
  loadThreads();
});

// ===================== USSD =====================

const USSD_PRESETS = ["*118#", "*100#", "*121#", "*133#", "*222#"];
$("#ussd-presets").innerHTML = USSD_PRESETS
  .map((c) => `<span class="chip" data-code="${c}">${c}</span>`).join("");
$$("#ussd-presets .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    $("#ussd-code").value = chip.dataset.code;
    $("#btn-ussd-send").click();
  });
});

$("#btn-ussd-send").addEventListener("click", async () => {
  const code = $("#ussd-code").value.trim();
  if (!code) return;
  const out = $("#ussd-output");
  out.textContent = `Sending ${code}…`;
  const { data } = await api("POST", "/api/ussd/send", { code });
  out.textContent = data.success
    ? `${code} → ${data.status_text}\n\n${data.text || "(empty response)"}`
    : `${code} failed:\n\n${data.error}`;
  loadUssdHistory();
});

$("#btn-ussd-cancel").addEventListener("click", async () => {
  const { data } = await api("POST", "/api/ussd/cancel");
  $("#ussd-output").textContent = data.success
    ? "Session cancelled." : data.error;
});

async function loadUssdHistory() {
  const { data } = await api("GET", "/api/ussd/history");
  const body = $("#ussd-table tbody");
  if (!data.success || !data.history.length) {
    body.innerHTML = `<tr><td colspan="3">No USSD requests yet.</td></tr>`;
    return;
  }
  body.innerHTML = data.history.map((h) => `
    <tr>
      <td>${esc(shortTime(h.ts))}</td>
      <td>${esc(h.code)}</td>
      <td class="wrap">${h.ok ? "" : '<span class="tag bad">error</span> '}${
        esc((h.response || "").slice(0, 120))}</td>
    </tr>`).join("");
}

// ===================== PHONEBOOK =====================

async function loadContacts() {
  const params = new URLSearchParams();
  if ($("#pb-search").value) params.set("search", $("#pb-search").value);
  const { data } = await api("GET", "/api/phonebook?" + params);
  const body = $("#pb-table tbody");
  if (!data.success || !data.contacts.length) {
    body.innerHTML = `<tr><td colspan="4">No contacts yet.</td></tr>`;
    return;
  }
  body.innerHTML = data.contacts.map((c) => `
    <tr>
      <td>${esc(c.name)}</td>
      <td>${esc(c.number)}</td>
      <td class="wrap">${esc(c.notes || "")}</td>
      <td>
        <button class="btn small" data-act="call" data-n="${esc(c.number)}">Call</button>
        <button class="btn small" data-act="sms" data-n="${esc(c.number)}">SMS</button>
        <button class="btn small" data-act="edit" data-id="${c.id}">Edit</button>
        <button class="btn small danger" data-act="del" data-id="${c.id}">Del</button>
      </td>
    </tr>`).join("");

  $$("#pb-table [data-act]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const act = btn.dataset.act;
      if (act === "call") {
        $("#dial-number").value = btn.dataset.n;
        $$(".tab").find((t) => t.dataset.tab === "dialer").click();
      } else if (act === "sms") {
        $$(".tab").find((t) => t.dataset.tab === "sms").click();
        openThread(btn.dataset.n);
        $("#compose-to").value = btn.dataset.n;
        $("#compose-body").focus();
      } else if (act === "edit") {
        const c = data.contacts.find((x) => x.id == btn.dataset.id);
        $("#pb-id").value = c.id;
        $("#pb-name").value = c.name;
        $("#pb-number").value = c.number;
        $("#pb-notes").value = c.notes || "";
        say("#pb-result", `Editing ${c.name}`);
      } else if (act === "del") {
        deleteContact(btn.dataset.id);
      }
    });
  });
}

async function deleteContact(id) {
  if (!confirm("Delete this contact?")) return;
  const { data } = await api("DELETE", "/api/phonebook/" + id);
  say("#pb-result", data.success ? "Contact deleted" : data.error, !data.success);
  loadContacts();
}

$("#btn-pb-save").addEventListener("click", async () => {
  const id = $("#pb-id").value;
  const payload = {
    name: $("#pb-name").value.trim(),
    number: $("#pb-number").value.trim(),
    notes: $("#pb-notes").value.trim() || null,
  };
  if (!payload.name || !payload.number)
    return say("#pb-result", "Name and number are both required", true);
  const { data } = id
    ? await api("PUT", "/api/phonebook/" + id, payload)
    : await api("POST", "/api/phonebook", payload);
  say("#pb-result", data.success ? "Saved" : data.error, !data.success);
  if (data.success) { resetContactForm(); loadContacts(); }
});

function resetContactForm() {
  ["#pb-id", "#pb-name", "#pb-number", "#pb-notes"].forEach((s) => $(s).value = "");
}
$("#btn-pb-reset").addEventListener("click", () => {
  resetContactForm(); say("#pb-result", "");
});
$("#btn-pb-refresh").addEventListener("click", loadContacts);
$("#pb-search").addEventListener("input", () => {
  clearTimeout(window._pb); window._pb = setTimeout(loadContacts, 300);
});
$("#btn-pb-import").addEventListener("click", async () => {
  say("#pb-result", "Reading the SIM…");
  const { data } = await api("POST", "/api/phonebook/import-sim");
  say("#pb-result", data.success
    ? `Found ${data.found} SIM entries, added ${data.added}`
    : data.error, !data.success);
  loadContacts();
});

// ===================== AT CONSOLE =====================

const AT_PRESETS = [
  "ATI", "AT+CSQ", "AT+COPS?", "AT+CEREG?", "AT+CPIN?", "AT+CLCC",
  "AT+CPAS", "AT+QNWINFO", 'AT+QCFG="band"', 'AT+QMBNCFG="list"',
  "AT+CSCA?", "AT+CGSMS?", "AT+CEER", "AT+CNUM",
];
$("#at-presets").innerHTML = AT_PRESETS
  .map((c) => `<span class="chip" data-cmd="${esc(c)}">${esc(c)}</span>`).join("");
$$("#at-presets .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    $("#at-command").value = chip.dataset.cmd;
    $("#btn-at-send").click();
  });
});

$("#at-command").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#btn-at-send").click();
});

$("#btn-at-send").addEventListener("click", async () => {
  const command = $("#at-command").value.trim();
  if (!command) return;
  const out = $("#at-output");
  out.textContent = `> ${command}\n\nrunning…`;
  const { data } = await api("POST", "/api/at", { command });
  out.textContent = `> ${data.command}\n\n${data.response}` +
    (data.success ? "" : "\n\n[command reported an error]");
  loadAtHistory();
});

async function loadAtHistory() {
  const { data } = await api("GET", "/api/at/history?limit=60");
  const body = $("#at-table tbody");
  if (!data.success || !data.history.length) {
    body.innerHTML = `<tr><td colspan="3">Nothing run yet.</td></tr>`;
    return;
  }
  body.innerHTML = data.history.map((h) => `
    <tr>
      <td>${esc(shortTime(h.ts))}</td>
      <td>${esc(h.command)}</td>
      <td>${h.ok ? "yes" : '<span class="tag bad">no</span>'}</td>
    </tr>`).join("");
  $$("#at-table tbody tr").forEach((tr, i) => {
    tr.addEventListener("click", () => {
      $("#at-command").value = data.history[i].command;
    });
  });
}

// ===================== CARRIER =====================

async function loadCarrier() {
  const { data } = await api("GET", "/api/carrier");
  if (!data.success) return;
  const s = data.settings || {};
  const set = (sel, v) => { if (v != null && v !== "") $(sel).value = String(v); };
  set("#car-domain", (s.sms_domain_pref || "").padStart(2, "0"));
  set("#car-ims", s.ims);
  set("#car-smsc", s.smsc);
  set("#car-cgsms", s.cgsms);
  set("#car-storage", s.storage);
  set("#car-scan", s.scan_mode);

  const n = data.network || {};
  const rows = [
    ["Operator", n.operator], ["Network", n.info], ["Registration", n.registration],
    ["IMSI", n.imsi], ["ICCID", n.iccid], ["Firmware", n.firmware],
    ["MBN profile", s.mbn_profile], ["MBN autosel", s.mbn_autosel],
    ["Bands", s.band], ["APN", s.apn],
    ["SMS domain", s.sms_domain_pref
      ? `${s.sms_domain_pref} — ${(s.sms_domain_pref_meaning || {})[s.sms_domain_pref] || "?"}`
      : null],
  ];
  $("#car-net tbody").innerHTML = rows
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `<tr><td style="color:var(--muted)">${esc(k)}</td><td class="wrap">${esc(v)}</td></tr>`)
    .join("");
}

async function saveCarrier(withReset) {
  const payload = {
    sms_domain_pref: $("#car-domain").value,
    ims: $("#car-ims").value,
    smsc: $("#car-smsc").value.trim(),
    cgsms: $("#car-cgsms").value,
    storage: $("#car-storage").value,
    scan_mode: $("#car-scan").value,
  };
  if (withReset) payload.reset = true;
  say("#car-result", withReset
    ? "Applying and resetting the modem — about 40 seconds…" : "Applying…");
  $("#btn-car-save").disabled = $("#btn-car-save-reset").disabled = true;
  const { data } = await api("POST", "/api/carrier", payload);
  $("#btn-car-save").disabled = $("#btn-car-save-reset").disabled = false;
  if (!data.success) return say("#car-result", data.error, true);
  const applied = Object.keys(data.applied || {}).join(", ") || "nothing";
  say("#car-result",
    `Applied: ${applied}${data.reset_required ? " — reset required before these take effect" : ""}`,
    Boolean(data.errors));
  loadCarrier(); loadStatus(); loadMode();
}

$("#btn-car-save").addEventListener("click", () => saveCarrier(false));
$("#btn-car-save-reset").addEventListener("click", () => {
  if (confirm("Reset the modem now? Calls and data drop for ~40 seconds.")) saveCarrier(true);
});
$("#btn-car-refresh").addEventListener("click", loadCarrier);

async function loadVoicemail() {
  const { data } = await api("GET", "/api/voicemail");
  if (!data.success) return;
  $("#vm-enabled").checked = Boolean(data.enabled);
  $("#vm-ring").value = data.ring_seconds;
  $("#vm-max").value = data.max_seconds;
  const pill = $("#vm-state");
  pill.textContent = data.enabled
    ? (data.greeting_available
        ? `answering after ${data.ring_seconds}s`
        : "on, greeting missing")
    : "off";
  pill.className = "modepill " + (data.enabled
    ? (data.greeting_available ? "voice" : "sms") : "busy");

  const { data: files } = await api("GET", "/api/audio/files");
  if (files.success) {
    $("#vm-greeting").innerHTML =
      `<option value="">— none —</option>` + files.files
        .filter((f) => f.has_audio)
        .map((f) => `<option value="${esc(f.file)}">${esc(f.file)} (${f.seconds}s)</option>`)
        .join("");
    if (data.greeting) $("#vm-greeting").value = data.greeting;
  }
}

$("#btn-vm-save").addEventListener("click", async () => {
  const { data } = await api("POST", "/api/voicemail", {
    enabled: $("#vm-enabled").checked,
    ring_seconds: parseInt($("#vm-ring").value, 10),
    max_seconds: parseInt($("#vm-max").value, 10),
    greeting: $("#vm-greeting").value || null,
  });
  say("#vm-result", data.success
    ? (data.enabled
        ? `Saved — answering after ${data.ring_seconds}s, recording up to ${data.max_seconds}s`
        : "Saved — voicemail disabled")
    : data.error, !data.success);
  loadVoicemail();
});

// ===================== TIME / NTP =====================

async function loadNtp() {
  const { data } = await api("GET", "/api/time");
  if (!data.success) return;
  const t = data.timesync || {};
  const configured = (data.configured_servers || []).join(" ");

  const rows = [
    ["Local time", data.local],
    ["Timezone", data.timezone],
    // The one row worth reading first. "no" means every date on this page,
    // including the prepaid reminder's, is computed from a clock that is wrong.
    ["Synchronized", data.synchronized ? "yes" : "no — clock not trustworthy yet"],
    ["Server in use", t.ServerName || t.ServerAddress || null],
    ["Last exchange", t.last_sync],
    ["Configured here", configured || "not set — using the distro defaults"],
  ];
  $("#ntp-state tbody").innerHTML = rows
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `<tr><td style="color:var(--muted)">${esc(k)}</td>`
      + `<td class="wrap">${esc(v)}</td></tr>`)
    .join("");

  // Only prefill when the field is untouched, so a reload triggered by Apply
  // does not overwrite what is being typed.
  if (!$("#ntp-servers").value.trim() && configured) {
    $("#ntp-servers").value = configured;
  }
  if (!data.helper_installed) {
    say("#ntp-result",
      "iris-set-ntp is not installed — run scripts/setup.sh, or Apply will fail",
      true);
  }
}

$("#btn-ntp-refresh").addEventListener("click", loadNtp);

$("#btn-ntp-save").addEventListener("click", async () => {
  const servers = $("#ntp-servers").value.trim();
  if (!servers) return say("#ntp-result", "Give at least one server", true);
  say("#ntp-result", "Applying and restarting timesyncd…");
  $("#btn-ntp-save").disabled = true;
  const { data } = await api("POST", "/api/time/ntp", { servers });
  $("#btn-ntp-save").disabled = false;
  say("#ntp-result", data.success
    ? `Now using ${(data.configured_servers || []).join(", ")}`
    : data.error, !data.success);
  loadNtp();
});

$("#btn-ntp-sync").addEventListener("click", async () => {
  say("#ntp-result", "Restarting timesyncd to force a poll…");
  $("#btn-ntp-sync").disabled = true;
  const { data } = await api("POST", "/api/time/sync", {});
  $("#btn-ntp-sync").disabled = false;
  if (!data.success) return say("#ntp-result", data.error, true);
  // The restart returns before the exchange completes, so the state table is
  // re-read shortly after rather than straight away — otherwise it reports the
  // pre-sync values and looks like nothing happened.
  say("#ntp-result", "Resync requested — checking…");
  setTimeout(async () => {
    await loadNtp();
    const { data: d2 } = await api("GET", "/api/time");
    say("#ntp-result", d2.synchronized
      ? `Synchronized — clock now ${d2.local}`
      : "Requested, but not synchronized yet — check the server is reachable",
      !d2.synchronized);
  }, 4000);
});

// ===================== API EXPLORER =====================

let specLoaded = false;
async function loadSpec() {
  if (specLoaded) return;
  const { data } = await api("GET", "/api/spec");
  if (!data.paths) return;
  specLoaded = true;

  const groups = {};
  for (const [path, methods] of Object.entries(data.paths)) {
    for (const [method, info] of Object.entries(methods)) {
      const tag = (info.tags && info.tags[0]) || "other";
      (groups[tag] = groups[tag] || []).push({
        method: method.toUpperCase(), path, info,
      });
    }
  }

  $("#api-list").innerHTML = Object.entries(groups).map(([tag, items]) => `
    <div class="apigroup">
      <div class="h">${esc(tag)}</div>
      ${items.map((it, i) => `
        <div class="apiitem" data-tag="${esc(tag)}" data-i="${i}">
          <span class="m ${it.method}">${it.method}</span>
          <span class="p">${esc(it.path)}</span>
          <span class="s">${esc(it.info.summary || "")}</span>
        </div>`).join("")}
    </div>`).join("");

  $$("#api-list .apiitem").forEach((el) => {
    el.addEventListener("click", () => {
      const item = groups[el.dataset.tag][el.dataset.i];
      $("#api-method").value = item.method;
      $("#api-path").value = item.path;
      const schema = item.info.requestBody?.content?.["application/json"]?.schema;
      if (schema && schema.properties) {
        const example = {};
        for (const [k, v] of Object.entries(schema.properties))
          example[k] = v.example ?? "";
        $("#api-body").value = JSON.stringify(example, null, 2);
      } else {
        $("#api-body").value = "";
      }
      $("#api-output").textContent = "No request sent yet.";
      $("#api-status").textContent = "";
    });
  });
}

$("#btn-api-send").addEventListener("click", async () => {
  const method = $("#api-method").value;
  let path = $("#api-path").value.trim();
  if (!path) return;
  if (path.includes("{id}")) {
    const id = prompt("Value for {id}:");
    if (id === null) return;
    path = path.replace("{id}", id);
  }
  let payload;
  const raw = $("#api-body").value.trim();
  if (raw && method !== "GET") {
    try { payload = JSON.parse(raw); }
    catch (e) {
      $("#api-output").textContent = "Request body is not valid JSON: " + e.message;
      return;
    }
  }
  $("#api-output").textContent = "Sending…";
  const started = performance.now();
  const { status, data } = await api(method, path, payload);
  const ms = Math.round(performance.now() - started);
  $("#api-status").textContent = `— HTTP ${status} in ${ms} ms`;
  $("#api-output").textContent = JSON.stringify(data, null, 2);
});

// ===================== ACCESS =====================

function ago(iso) {
  if (!iso) return "never";
  const then = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  return `${Math.floor(s / 86400)} d ago`;
}

async function loadAccount() {
  const { data } = await api("GET", "/api/auth/state");
  $("#acc-user").textContent = data.user || "—";
  const rec = await api("GET", "/api/auth/recovery");
  $("#acc-recovery").value = rec.data.number || "";
}

$("#btn-logout").addEventListener("click", async (e) => {
  e.preventDefault();
  await api("POST", "/api/auth/logout", {});
  location.href = "/login";
});

// --- board power -----------------------------------------------------------

// Feedback goes through the mode banner rather than a result line in a card:
// these buttons live in the top bar now, and the banner is full width and
// visible from whichever tab you happened to be on when you pressed them.
function powerBanner(text, isError) {
  $("#modebanner").classList.toggle("err", Boolean(isError));
  modeBanner(true, text);
}

async function power(action, prompt) {
  if (!confirm(prompt)) return;
  let { data } = await api("POST", "/api/system/power", { action });

  // 409 means a call is up. Ask once more naming that, rather than refusing
  // silently or overriding something the operator did not know about.
  if (!data.success && data.call_active) {
    if (!confirm("A call is in progress. " + action + " anyway and drop it?")) {
      return modeBanner(false);
    }
    ({ data } = await api("POST", "/api/system/power", { action, force: true }));
  }
  if (!data.success) return powerBanner(data.error, true);

  powerBanner(action === "reboot"
    ? "Rebooting. This page will fail to reach the board for about a minute."
    : "Shutting down. The board will not come back on its own.");
  // Every poller on this page is about to start failing against a board that is
  // deliberately gone; stop them so the UI goes quiet instead of flashing errors.
  if (window.stopPollers) window.stopPollers();
}

// Bound defensively. A browser holding a cached index.html from before these
// buttons existed would otherwise throw here on a null element, and every
// statement below — the pollers included — would never run. A stale cache should
// cost you two buttons, not the whole page.
$("#btn-sys-reboot")?.addEventListener("click", () => power("reboot",
  "Reboot the board? Calls, SMS and LTE drop for about a minute."));

$("#btn-sys-shutdown")?.addEventListener("click", () => power("shutdown",
  "Shut the board down? It cannot be switched back on remotely — "
  + "someone has to be physically at the board."));

$("#btn-acc-save").addEventListener("click", async () => {
  const { data } = await api("POST", "/api/auth/password", {
    current_password: $("#acc-current").value,
    new_password: $("#acc-new").value,
  });
  say("#acc-result", data.success ? "Password changed." : data.error, !data.success);
  if (data.success) { $("#acc-current").value = ""; $("#acc-new").value = ""; }
});

$("#btn-recovery-save").addEventListener("click", async () => {
  const { data } = await api("POST", "/api/auth/recovery", {
    number: $("#acc-recovery").value.trim(),
    current_password: $("#acc-recovery-pw").value,
  });
  say("#recovery-result",
      data.success ? "Recovery number saved." : data.error, !data.success);
  if (data.success) $("#acc-recovery-pw").value = "";
});

// --- tokens ---

let SCOPE_HELP = {};

function renderScopeChoices(scopes) {
  $("#tok-scopes").innerHTML = scopes.map((s) => `
    <label class="scopeopt">
      <input type="checkbox" value="${esc(s)}">
      <span><strong>${esc(s)}</strong>
        <span class="sub">${esc(SCOPE_HELP[s] || "")}</span></span>
    </label>`).join("");
  // `full` is not a scope alongside others — it is all of them, so ticking it
  // clears and disables the rest rather than silently overriding them later.
  const boxes = $$("#tok-scopes input");
  boxes.forEach((b) => b.addEventListener("change", () => {
    const full = boxes.find((x) => x.value === "full");
    if (full && full.checked) {
      boxes.forEach((x) => { if (x !== full) { x.checked = false; x.disabled = true; } });
    } else {
      boxes.forEach((x) => { x.disabled = false; });
    }
  }));
}

async function loadTokens() {
  const { data } = await api("GET", "/api/auth/tokens");
  if (!data.success) return;
  SCOPE_HELP = data.scope_help || {};
  if (!$("#tok-scopes").children.length) renderScopeChoices(data.scopes || []);

  const now = new Date().toISOString();
  $("#tok-rows").innerHTML = (data.tokens || []).map((t) => {
    const dead = !!t.revoked_at;
    const expired = !dead && t.expires_at && t.expires_at < now;
    const state = dead ? "revoked" : expired ? "expired" : "active";
    const actions = dead
      ? `<button class="btn small" data-purge="${t.id}">Delete</button>`
      : `<button class="btn small" data-regen="${t.id}">Regenerate</button>
         <button class="btn small danger" data-revoke="${t.id}">Revoke</button>`;
    return `<tr class="${dead || expired ? "dim" : ""}">
      <td>${esc(t.name)}<div class="sub">${state}</div></td>
      <td>${t.scopes.map((s) => `<span class="scopetag">${esc(s)}</span>`).join(" ")}</td>
      <td><code>${esc(t.prefix)}</code></td>
      <td>${esc(ago(t.last_used_at))}
          <div class="sub">${esc(t.last_used_ip || "")}</div></td>
      <td>${t.expires_at ? esc(shortTime(t.expires_at)) : "never"}</td>
      <td class="rowactions">${actions}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="sub">No tokens yet.</td></tr>`;
}

function revealToken(value, token) {
  $("#tok-reveal").classList.remove("hidden");
  $("#tok-value").textContent = value;
  $("#tok-hint").textContent =
    `${token.name} — scopes ${token.scopes.join(", ")}. `
    + `Send it as: Authorization: Bearer <token>`;
}

$("#btn-tok-create").addEventListener("click", async () => {
  const scopes = $$("#tok-scopes input").filter((b) => b.checked).map((b) => b.value);
  const expires = $("#tok-expires").value;
  const { data } = await api("POST", "/api/auth/tokens", {
    name: $("#tok-name").value.trim(),
    scopes,
    expires_days: expires ? Number(expires) : null,
  });
  if (!data.success) { say("#tok-result", data.error, true); return; }
  say("#tok-result", "");
  revealToken(data.value, data.token);
  $("#tok-name").value = "";
  $$("#tok-scopes input").forEach((b) => { b.checked = false; b.disabled = false; });
  loadTokens();
});

$("#btn-tok-copy")?.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#tok-value").textContent);
    say("#tok-result", "Copied to the clipboard.");
  } catch {
    say("#tok-result", "Could not copy — select the value and copy it manually.", true);
  }
});

$("#tok-rows")?.addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const { revoke, regen, purge } = btn.dataset;
  const row = btn.closest("tr");
  const name = row ? row.querySelector("td").childNodes[0].textContent.trim() : "";

  if (revoke) {
    // Revoking the gate token silently breaks the gate, so name it in the prompt.
    if (!confirm(`Revoke "${name}"? Anything using it stops working immediately.`)) return;
    await api("DELETE", `/api/auth/tokens/${revoke}`);
  } else if (regen) {
    if (!confirm(`Regenerate "${name}"? The current value stops working immediately.`)) return;
    const { data } = await api("POST", `/api/auth/tokens/${regen}/regenerate`, {});
    if (data.success) revealToken(data.value, data.token);
  } else if (purge) {
    await api("DELETE", `/api/auth/tokens/${purge}?purge=1`);
  }
  loadTokens();
});

// ===================== FIRMWARE / UPDATES =====================

let latestAvailableVersion = null;

async function loadFirmwareCheck() {
  const btn = $("#btn-firmware-check");
  const updateBtn = $("#btn-firmware-update");
  say("#firmware-result", "Checking GitHub for releases…");
  if (btn) btn.disabled = true;
  const { data } = await api("GET", "/api/firmware/check");
  if (btn) btn.disabled = false;

  if (!data.success) {
    say("#firmware-result", data.error || "Could not check updates", true);
    if (updateBtn) updateBtn.style.display = "none";
    return;
  }

  latestAvailableVersion = data.latest_version;

  const rows = [
    ["Current version", data.current_version],
    ["Latest release tag", data.latest_version],
    ["Update status", data.update_available ? "🎁 New update available" : "Up to date"],
    ["Release repository", data.repo],
  ];

  if (data.versions && data.versions.length) {
    rows.push(["Available tags", data.versions.join(", ")]);
  }

  const tbody = $("#firmware-state tbody");
  if (tbody) {
    tbody.innerHTML = rows
      .map(([k, v]) => `<tr><td style="color:var(--muted)">${esc(k)}</td>`
        + `<td class="wrap">${esc(v)}</td></tr>`)
      .join("");
  }

  if (data.update_available) {
    say("#firmware-result", `New update found: ${data.latest_version}`);
    if (updateBtn) {
      updateBtn.style.display = "inline-block";
      updateBtn.textContent = `Install update (${data.latest_version})`;
      updateBtn.disabled = false;
    }
  } else {
    say("#firmware-result", `Software is up to date (${data.current_version})`);
    if (updateBtn) updateBtn.style.display = "none";
  }
}

async function triggerFirmwareUpdate() {
  if (!latestAvailableVersion) return;
  if (!confirm(`Install update ${latestAvailableVersion} and restart phone service?`)) {
    return;
  }

  const checkBtn = $("#btn-firmware-check");
  const updateBtn = $("#btn-firmware-update");
  if (checkBtn) checkBtn.disabled = true;
  if (updateBtn) {
    updateBtn.disabled = true;
    updateBtn.textContent = "Installing update…";
  }

  say("#firmware-result", `Downloading and installing ${latestAvailableVersion}… please wait.`);

  const { data } = await api("POST", "/api/firmware/update", { version: latestAvailableVersion });
  if (!data.success) {
    say("#firmware-result", data.error || "Update failed", true);
    if (checkBtn) checkBtn.disabled = false;
    if (updateBtn) updateBtn.disabled = false;
    return;
  }

  say("#firmware-result", `Update installed successfully! Restarting service… Reconnecting…`);

  setTimeout(async () => {
    let attempts = 0;
    const interval = setInterval(async () => {
      attempts++;
      say("#firmware-result", `Reconnecting to phone service (attempt ${attempts})…`);
      try {
        const ping = await fetch("/api/status", { cache: "no-store" });
        if (ping.ok) {
          clearInterval(interval);
          say("#firmware-result", `Service is back online! Reloading page…`);
          setTimeout(() => location.reload(), 1000);
        }
      } catch (e) {
        // Still restarting
      }
      if (attempts > 30) {
        clearInterval(interval);
        say("#firmware-result", "Service taking longer than expected. Please refresh manually.", true);
      }
    }, 2000);
  }, 3000);
}

$("#btn-firmware-check")?.addEventListener("click", loadFirmwareCheck);
$("#btn-firmware-update")?.addEventListener("click", triggerFirmwareUpdate);

// ===================== boot =====================

loadStatus();
loadMode();
loadCalls();
loadAudio();
loadThreads();
// Handles are kept so the page can go quiet on purpose. Without this, a
// deliberate reboot or shutdown leaves four pollers hammering a board that is
// meant to be gone, and the UI fills with failures that look like a fault.
const POLLERS = [
  setInterval(loadStatus, 10000),
  setInterval(loadMode, 15000),
  setInterval(pollEvents, 4000),
  setInterval(() => {
    // Keep the audio panel live only while its tab is visible.
    if ($("#tab-dialer").classList.contains("active")) loadAudio();
  }, 8000),
];

window.stopPollers = () => POLLERS.forEach(clearInterval);

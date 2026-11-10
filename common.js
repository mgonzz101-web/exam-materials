/* Shared helpers for all pages (no frameworks, no build step). */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let CSRF = "";

async function api(path, opts = {}) {
  const method = opts.method || "GET";
  const o = { method, headers: {}, credentials: "same-origin" };
  if (opts.body !== undefined) {
    o.body = JSON.stringify(opts.body);
    o.headers["Content-Type"] = "application/json";
  }
  if (method !== "GET") o.headers["X-CSRF-Token"] = CSRF;
  let r;
  try {
    r = await fetch(path, o);
  } catch (e) {
    const err = new Error("No connection. Check your internet and try again.");
    err.network = true;
    throw err;
  }
  let data = {};
  try {
    data = await r.json();
  } catch (e) {}
  if (!r.ok) {
    if (r.status === 403 && /session expired/i.test(data.error || "") && !opts._retried) {
      await loadSession();
      return api(path, { ...opts, _retried: true });
    }
    const err = new Error(data.error || "Something went wrong. Please try again.");
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function loadSession() {
  const s = await api("/api/session");
  CSRF = s.csrf;
  return s;
}

function toast(msg, kind = "") {
  let box = $("#toast");
  if (!box) {
    box = document.createElement("div");
    box.id = "toast";
    box.setAttribute("aria-live", "polite");
    document.body.appendChild(box);
  }
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => t.remove(), kind === "bad" ? 6000 : 3000);
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}
function localDayStartISO(dateStr) {
  return new Date(dateStr + "T00:00:00").toISOString().replace(/\.\d+Z$/, "Z");
}
function localDayEndISO(dateStr) {
  return new Date(dateStr + "T23:59:59").toISOString().replace(/\.\d+Z$/, "Z");
}

function uuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  const b = new Uint8Array(16);
  (window.crypto || window.msCrypto).getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

const LS = {
  get(k, d) {
    try {
      const v = localStorage.getItem(k);
      return v ? JSON.parse(v) : d;
    } catch (e) {
      return d;
    }
  },
  set(k, v) {
    try {
      localStorage.setItem(k, JSON.stringify(v));
    } catch (e) {}
  },
  del(k) {
    try {
      localStorage.removeItem(k);
    } catch (e) {}
  },
};

const STATUS_LABEL = { in: "In Stock", low: "Low Stock", out: "Out of Stock" };
const badge = (status, extra = "") =>
  `<span class="badge ${status}">${STATUS_LABEL[status]}${extra ? " · " + esc(extra) : ""}</span>`;

/* Modal dialog with a form. onSubmit(form) may throw an Error -> shown inside the dialog. */
function modal({ title, body, submit = "Save", danger = false, wide = false, onMount, onSubmit }) {
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<form class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}" novalidate>
      <h2>${esc(title)}</h2><div class="modal-body">${body}</div>
      <p class="form-error" role="alert" hidden></p>
      <div class="modal-actions"><button type="button" class="btn ghost" data-close>${submit ? "Cancel" : "Close"}</button>
      ${submit ? `<button type="submit" class="btn ${danger ? "danger" : "primary"}">${esc(submit)}</button>` : ""}</div></form>`;
  document.body.appendChild(ov);
  const form = ov.firstElementChild;
  const err = $(".form-error", form);
  const onKey = (e) => e.key === "Escape" && close();
  const close = () => {
    ov.remove();
    document.removeEventListener("keydown", onKey);
  };
  document.addEventListener("keydown", onKey);
  ov.addEventListener("mousedown", (e) => e.target === ov && close());
  $("[data-close]", form).addEventListener("click", close);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!onSubmit) return close();
    const btn = $("[type=submit]", form);
    btn.disabled = true;
    err.hidden = true;
    try {
      await onSubmit(form);
      close();
    } catch (e) {
      err.textContent = e.message || "Something went wrong.";
      err.hidden = false;
      btn.disabled = false;
    }
  });
  if (onMount) onMount(form);
  const first = $("input:not([type=hidden]):not([readonly]), select, textarea", form);
  if (first) first.focus();
  return { close, form };
}

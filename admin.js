/* Owner dashboard. All permissions are enforced by the server; this file is only the UI. */
(() => {
  const app = $("#app");
  const S = { user: null, tab: "dashboard", items: [], inv: { q: "", status: "", archived: false } };
  const KIND = {
    initial: "Item created",
    checkout: "Checkout",
    checkout_reversal: "Checkout reversed",
    checkout_correction: "Checkout corrected",
    restock: "Stock added",
    removed: "Stock removed",
    count: "Physical count",
  };
  const ALERT_TXT = {
    sent: (a) => `Emailed ${fmtDate(a.at)}`,
    failed: () => "Email FAILED",
    not_configured: () => "Not emailed (email not set up)",
    pending: () => "Sending…",
  };
  const alertText = (i) => (i.last_alert_status ? (ALERT_TXT[i.last_alert_status] || (() => i.last_alert_status))({ at: i.last_alert_at }) : "—");
  const signed = (n) => (n > 0 ? "+" + n : String(n));

  /* ---------------- entry ---------------- */
  async function init() {
    let s;
    try {
      s = await loadSession();
    } catch (e) {
      app.innerHTML = `<div class="card notice bad">${esc(e.message)}</div>`;
      return;
    }
    S.user = s.user;
    if (s.needs_setup) {
      app.innerHTML = `<div class="card"><h2>Welcome</h2><p>No owner account exists yet.</p><a class="btn primary" href="/setup">Start first-time setup</a></div>`;
      return;
    }
    if (!s.user) return renderLogin();
    if (s.user.role !== "owner") {
      $("#hdr").innerHTML = `<a class="btn small" href="/">Checkout page</a><button class="btn small ghost" id="lo">Sign out</button>`;
      $("#lo").onclick = logout;
      app.innerHTML = `<div class="card notice bad"><b>This area is for owners only.</b><br>You’re signed in as ${esc(s.user.name)}, which can browse and check out materials but cannot see the dashboard.</div>`;
      return;
    }
    renderShell();
  }

  async function logout() {
    const r = await api("/api/logout", { method: "POST", body: {} });
    CSRF = r.csrf;
    location.href = "/admin";
  }

  function renderLogin() {
    $("#hdr").innerHTML = "";
    app.innerHTML = `<div class="card" style="max-width:460px;margin:30px auto"><h2>Owner sign in</h2>
      <form id="lf" novalidate><label for="le">Email</label><input id="le" type="email" autocomplete="username" inputmode="email">
      <label for="lp">Password</label><input id="lp" type="password" autocomplete="current-password">
      <p class="form-error" id="lerr" hidden role="alert"></p>
      <button class="btn primary block" style="margin-top:16px">Sign in</button></form>
      <p class="hint"><a href="/">← Back to the checkout page</a></p></div>`;
    $("#lf").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      $("#lerr").hidden = true;
      try {
        const r = await api("/api/login", { method: "POST", body: { email: $("#le").value, password: $("#lp").value } });
        CSRF = r.csrf;
        init();
      } catch (e) {
        $("#lerr").textContent = e.message;
        $("#lerr").hidden = false;
      }
    });
  }

  function renderShell() {
    $("#hdr").innerHTML = `<a class="btn small" href="/" target="_blank" rel="noopener">Checkout page ↗</a>
      <span class="small muted">${esc(S.user.name)}</span><button class="btn small ghost" id="lo">Sign out</button>`;
    $("#lo").onclick = logout;
    const tabs = [
      ["dashboard", "Dashboard"],
      ["checkouts", "Checkouts"],
      ["audit", "Audit log"],
      ["qr", "QR code"],
      ["settings", "Settings"],
    ];
    app.innerHTML = `<div class="tabs no-print" role="tablist">${tabs
      .map(([k, l]) => `<button class="tab" role="tab" data-tab="${k}" aria-selected="${k === S.tab}">${l}</button>`)
      .join("")}</div><div id="panel" style="margin-top:14px"></div>`;
    $$("[data-tab]").forEach((b) => (b.onclick = () => go(b.dataset.tab)));
    go(S.tab);
  }

  async function go(tab) {
    S.tab = tab;
    $$("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    const p = $("#panel");
    p.innerHTML = `<div class="card muted">Loading…</div>`;
    try {
      await { dashboard: renderDashboard, checkouts: renderCheckouts, audit: renderAudit, qr: renderQR, settings: renderSettings }[tab]();
    } catch (e) {
      if (e.status === 401 || e.status === 403) return init();
      p.innerHTML = `<div class="card notice bad">${esc(e.message)}</div>`;
    }
  }

  /* ---------------- dashboard ---------------- */
  async function renderDashboard() {
    const [dash, inv] = await Promise.all([api("/api/admin/dashboard"), api("/api/admin/items")]);
    S.items = inv.items;
    const c = dash.counts;
    const cl = dash.checklist;
    const steps = [
      [true, "Owner account created", ""],
      [cl.inventory, "Add your inventory", `<button class="btn small" id="clAdd">Add an item</button> <button class="btn small ghost" id="clSample">Load sample inventory</button>`],
      [cl.recipients, "Choose who gets low-stock emails", `<button class="btn small" data-goto="settings">Open Settings</button>`],
      [cl.email_service, "Connect an email service (needed for emails to actually send)", `<span class="small muted">Set on the server — README Step 4.</span>`],
      [cl.qr, "Download the QR code", `<button class="btn small" data-goto="qr">Open QR code</button>`],
    ];
    const allDone = steps.every((x) => x[0]);
    const nr = dash.needs_reordering;

    $("#panel").innerHTML = `
      ${allDone ? "" : `<div class="card"><h2>Getting started</h2><ul class="steps">${steps
        .map(([d, t, a], i) => `<li class="${d ? "done" : ""}"><span class="dot">${d ? "✓" : i + 1}</span><div><b>${t}</b><div class="row" style="margin-top:6px">${d ? "" : a}</div></div></li>`)
        .join("")}</ul></div>`}
      <div class="card reorder ${nr.length ? "" : "ok"}">
        <h2>${nr.length ? `⚠ Needs reordering (${nr.length})` : "✓ Nothing needs reordering"}</h2>
        ${nr.length ? `<div class="table-wrap"><table><thead><tr><th>Item</th><th class="num">Now</th><th class="num">Alert level</th><th>Status</th><th>Alert email</th><th></th></tr></thead><tbody>
          ${nr.map((i) => `<tr><td><b>${esc(i.exam)}</b> ${esc(i.edition ? "(" + i.edition + ")" : "")}<div class="small muted">${esc(i.type_name)}${i.form_name ? " — " + esc(i.form_name) : ""}</div></td>
            <td class="num qty">${i.quantity}</td><td class="num">${i.low_threshold}</td><td>${badge(i.status)}</td>
            <td class="small">${esc(alertText(i))}</td><td><button class="btn small primary" data-adj="${i.id}" data-mode="add">Restock</button></td></tr>`).join("")}
          </tbody></table></div>` : `<p class="muted">Every active item is above its low-stock level.</p>`}
      </div>
      <div class="tiles">
        <div class="tile"><span class="muted small">Active items</span><b>${dash.total}</b></div>
        <div class="tile in"><span class="muted small">In stock</span><b>${c.in}</b></div>
        <div class="tile low"><span class="muted small">Low stock</span><b>${c.low}</b></div>
        <div class="tile out"><span class="muted small">Out of stock</span><b>${c.out}</b></div>
      </div>
      <div class="card"><div class="row spread" style="margin-bottom:10px">
        <h2 style="margin:0">All inventory</h2>
        <div class="row"><button class="btn primary small" id="addItem">+ Add item</button>
        <a class="btn small" href="/api/admin/export/inventory.csv">Export CSV</a></div></div>
        <div class="row grow no-print"><input id="invQ" type="search" placeholder="Search exam, edition, form…" aria-label="Search inventory" value="${esc(S.inv.q)}">
        <select id="invSt" aria-label="Status filter"><option value="">All statuses</option><option value="low">Low stock</option><option value="out">Out of stock</option><option value="in">In stock</option></select>
        <div class="check" style="margin:0"><input type="checkbox" id="invArch" ${S.inv.archived ? "checked" : ""}><label for="invArch">Show archived</label></div></div>
        <div class="table-wrap" style="margin-top:8px"><table><thead><tr><th>Exam</th><th>Type / form</th><th class="num">Qty</th><th class="num">Alert level</th><th>Status</th><th>Location</th><th></th></tr></thead><tbody id="invBody"></tbody></table></div>
      </div>`;
    $("#invSt").value = S.inv.status;
    drawInventory();
    $("#invQ").oninput = (e) => ((S.inv.q = e.target.value), drawInventory());
    $("#invSt").onchange = (e) => ((S.inv.status = e.target.value), drawInventory());
    $("#invArch").onchange = (e) => ((S.inv.archived = e.target.checked), drawInventory());
    $("#addItem").onclick = () => itemModal();
    const a = $("#clAdd");
    if (a) a.onclick = () => itemModal();
    const sm = $("#clSample");
    if (sm)
      sm.onclick = async () => {
        const r = await api("/api/admin/sample-data", { method: "POST", body: {} });
        toast(`Added ${r.added} sample items`, "good");
        go("dashboard");
      };
    $$("[data-goto]").forEach((b) => (b.onclick = () => go(b.dataset.goto)));
    $$("[data-adj]").forEach((b) => (b.onclick = () => adjustModal(S.items.find((i) => i.id === Number(b.dataset.adj)), b.dataset.mode)));
  }

  function drawInventory() {
    const q = S.inv.q.trim().toLowerCase();
    const rows = S.items.filter((i) => {
      if (i.archived && !S.inv.archived) return false;
      if (S.inv.status && i.status !== S.inv.status) return false;
      if (!q) return true;
      return q.split(/\s+/).every((w) => `${i.exam} ${i.edition} ${i.type_name} ${i.form_name} ${i.location} ${i.notes}`.toLowerCase().includes(w));
    });
    $("#invBody").innerHTML = rows.length
      ? rows
          .map(
            (i) => `<tr class="${i.archived ? "archived" : ""}"><td><b>${esc(i.exam)}</b><div class="small muted">${esc(i.edition)}</div></td>
        <td>${esc(i.type_name)}<div class="small muted">${esc(i.form_name)}</div></td>
        <td class="num qty">${i.quantity}</td><td class="num">${i.low_threshold}</td>
        <td>${i.archived ? '<span class="badge neutral">Archived</span>' : badge(i.status)}</td>
        <td class="small">${esc(i.location)}${i.notes ? `<div class="muted">${esc(i.notes)}</div>` : ""}</td>
        <td><div class="row" style="flex-wrap:nowrap">
          <button class="btn small primary" data-a="adj" data-id="${i.id}">Adjust</button>
          <button class="btn small" data-a="edit" data-id="${i.id}">Edit</button>
          <button class="btn small ghost" data-a="arch" data-id="${i.id}">${i.archived ? "Restore" : "Archive"}</button></div></td></tr>`
          )
          .join("")
      : `<tr><td colspan="7" class="muted">No items match. ${S.items.length ? "" : "Use “+ Add item” or load the sample inventory from the getting-started box."}</td></tr>`;
    $$("#invBody [data-a]").forEach(
      (b) =>
        (b.onclick = async () => {
          const it = S.items.find((x) => x.id === Number(b.dataset.id));
          if (b.dataset.a === "adj") adjustModal(it);
          if (b.dataset.a === "edit") itemModal(it);
          if (b.dataset.a === "arch") {
            if (!it.archived && !confirm(`Archive “${it.exam} — ${it.type_name}”? It will disappear from the checkout page. You can restore it later.`)) return;
            try {
              await api(`/api/admin/items/${it.id}/archive`, { method: "POST", body: { archived: !it.archived } });
              toast(it.archived ? "Restored" : "Archived", "good");
              go("dashboard");
            } catch (e) {
              toast(e.message, "bad");
            }
          }
        })
    );
  }

  function itemModal(it) {
    const editing = !!it;
    modal({
      title: editing ? "Edit item" : "Add inventory item",
      submit: editing ? "Save changes" : "Add item",
      body: `<label for="x_exam">Exam name</label><input id="x_exam" value="${esc(it?.exam || "")}" placeholder="e.g. WISC-V">
        <label for="x_ed">Edition / version</label><input id="x_ed" value="${esc(it?.edition || "")}" placeholder="e.g. 5th Edition">
        <label for="x_type">Material type</label><select id="x_type"><option value="record_form">Record form</option><option value="response_booklet">Response booklet</option></select>
        <label for="x_fn">Specific form or booklet name <span class="muted">(optional)</span></label><input id="x_fn" value="${esc(it?.form_name || "")}" placeholder="e.g. Response Booklet 1">
        ${editing ? "" : `<label for="x_q">Starting quantity <span class="muted">(individual forms, not packs)</span></label><input id="x_q" inputmode="numeric" value="0">`}
        <label for="x_t">Low-stock alert level</label><input id="x_t" inputmode="numeric" value="${it ? it.low_threshold : 5}">
        <div class="hint">You’ll be alerted when the quantity reaches or drops below this number.</div>
        <label for="x_loc">Storage location <span class="muted">(optional)</span></label><input id="x_loc" value="${esc(it?.location || "")}">
        <label for="x_n">Notes <span class="muted">(optional)</span></label><textarea id="x_n">${esc(it?.notes || "")}</textarea>
        ${editing ? `<p class="hint">To change the quantity, use <b>Adjust</b> so the change is recorded with a reason.</p>` : ""}`,
      onMount(f) {
        $("#x_type", f).value = it?.material_type || "record_form";
      },
      async onSubmit(f) {
        const body = {
          exam: $("#x_exam", f).value,
          edition: $("#x_ed", f).value,
          material_type: $("#x_type", f).value,
          form_name: $("#x_fn", f).value,
          low_threshold: $("#x_t", f).value,
          location: $("#x_loc", f).value,
          notes: $("#x_n", f).value,
        };
        if (!editing) body.quantity = $("#x_q", f).value;
        await api(editing ? `/api/admin/items/${it.id}` : "/api/admin/items", { method: editing ? "PUT" : "POST", body });
        toast(editing ? "Saved" : "Item added", "good");
        go(S.tab);
      },
    });
  }

  function adjustModal(it, mode = "add") {
    const REASONS = { add: "New supplies received", remove: "Damaged / missing / discarded", set: "Physical inventory count" };
    modal({
      title: "Adjust stock",
      submit: "Save adjustment",
      body: `<p><b>${esc(it.exam)}</b> ${esc(it.edition)}<br><span class="muted">${esc(it.type_name)}${it.form_name ? " — " + esc(it.form_name) : ""}</span><br>Currently <b>${it.quantity}</b> on hand.</p>
        <div class="seg" role="radiogroup">
          <label><input type="radio" name="m" value="add"><span>Add stock <span class="muted small">(new supplies arrived)</span></span></label>
          <label><input type="radio" name="m" value="remove"><span>Remove stock <span class="muted small">(damaged, missing, discarded)</span></span></label>
          <label><input type="radio" name="m" value="set"><span>Set exact count <span class="muted small">(after a physical count)</span></span></label></div>
        <label for="a_amt" id="a_lbl">How many?</label><input id="a_amt" inputmode="numeric" value="">
        <label for="a_why">Reason <span class="muted">(required)</span></label><input id="a_why" maxlength="300" value="${esc(REASONS[mode])}">
        <div class="preview" id="a_prev" aria-live="polite"></div>`,
      onMount(f) {
        const radios = $$("[name=m]", f);
        radios.forEach((r) => (r.checked = r.value === mode));
        const cur = () => radios.find((r) => r.checked).value;
        const upd = () => {
          const m = cur(),
            v = parseInt($("#a_amt", f).value, 10);
          $("#a_lbl", f).textContent = m === "set" ? "The exact number counted" : "How many?";
          let txt = "";
          if (!isNaN(v)) {
            const nw = m === "add" ? it.quantity + v : m === "remove" ? it.quantity - v : v;
            txt = `New quantity: ${it.quantity} → ${nw}` + (nw < 0 ? " (can’t go below 0)" : nw <= it.low_threshold ? " — at or below the alert level" : "");
          }
          $("#a_prev", f).textContent = txt;
          $("#a_prev", f).hidden = !txt;
        };
        radios.forEach((r) =>
          r.addEventListener("change", () => {
            if ($("#a_why", f).value === REASONS.add || $("#a_why", f).value === REASONS.remove || $("#a_why", f).value === REASONS.set) $("#a_why", f).value = REASONS[cur()];
            upd();
          })
        );
        $("#a_amt", f).addEventListener("input", upd);
        upd();
      },
      async onSubmit(f) {
        const m = $("[name=m]:checked", f).value;
        const r = await api(`/api/admin/items/${it.id}/adjust`, { method: "POST", body: { mode: m, amount: $("#a_amt", f).value, reason: $("#a_why", f).value } });
        toast(`Saved — now ${r.item.quantity} on hand`, "good");
        go(S.tab);
      },
    });
  }

  /* ---------------- checkouts ---------------- */
  async function renderCheckouts() {
    $("#panel").innerHTML = `<div class="card"><div class="row spread"><h2 style="margin:0">Checkout history</h2>
      <a class="btn small" id="expCo" href="/api/admin/export/transactions.csv">Export CSV</a></div>
      <div class="row grow" style="margin-top:10px">
        <input id="fq" type="search" placeholder="Name or email" aria-label="Name or email">
        <input id="fexam" type="search" placeholder="Exam" aria-label="Exam">
        <input id="fitem" type="search" placeholder="Edition / form / type" aria-label="Item">
        <div><label for="ffrom" style="margin-top:0" class="small">From</label><input id="ffrom" type="date"></div>
        <div><label for="fto" style="margin-top:0" class="small">To</label><input id="fto" type="date"></div>
        <div><label for="fst" style="margin-top:0" class="small">Status</label><select id="fst"><option value="">All</option><option value="completed">Completed</option><option value="reversed">Reversed</option></select></div>
      </div></div><div id="coList"></div>`;
    const reload = () => loadCheckouts();
    ["fq", "fexam", "fitem"].forEach((id) => ($("#" + id).oninput = debounce(reload, 250)));
    ["ffrom", "fto", "fst"].forEach((id) => ($("#" + id).onchange = reload));
    loadCheckouts();
  }
  const debounce = (fn, ms) => {
    let t;
    return (...a) => (clearTimeout(t), (t = setTimeout(() => fn(...a), ms)));
  };

  function filterParams() {
    const p = new URLSearchParams();
    const set = (k, v) => v && p.set(k, v);
    set("q", $("#fq").value.trim());
    set("exam", $("#fexam").value.trim());
    set("item", $("#fitem").value.trim());
    set("status", $("#fst").value);
    if ($("#ffrom").value) p.set("from", localDayStartISO($("#ffrom").value));
    if ($("#fto").value) p.set("to", localDayEndISO($("#fto").value));
    return p;
  }

  async function loadCheckouts() {
    const box = $("#coList");
    try {
      const r = await api("/api/admin/checkouts?" + filterParams());
      if (!r.checkouts.length) {
        box.innerHTML = `<div class="card muted">No checkouts match.</div>`;
        return;
      }
      box.innerHTML = r.checkouts
        .map(
          (c) => `<div class="co ${c.status === "reversed" ? "rev" : ""}" data-id="${c.id}">
        <div class="row spread"><div><b>#${c.id} · ${esc(c.name)}</b> <span class="muted">${esc(c.email)}</span><div class="small muted">${esc(fmtDate(c.created_at))}</div></div>
        <div class="row">${c.status === "reversed" ? '<span class="badge neutral">Reversed</span>' : c.edited ? '<span class="badge low">Corrected</span>' : '<span class="badge in">Completed</span>'}
        <b>${c.total_units} item${c.total_units === 1 ? "" : "s"}</b></div></div>
        <ul>${c.lines.map((l) => `<li>${esc(l.exam)} ${esc(l.edition)} — ${esc(l.type_name)}${l.form_name ? ": " + esc(l.form_name) : ""} <b>× ${l.qty}</b>${l.qty !== l.original_qty ? ` <span class="muted small">(originally ${l.original_qty})</span>` : ""}</li>`).join("")}</ul>
        ${c.status === "reversed" ? `<div class="small muted">Reversed ${esc(fmtDate(c.reversed_at))} by ${esc(c.reversed_by || "")}: ${esc(c.reverse_reason || "")}</div>` : `<div class="row no-print"><button class="btn small" data-a="correct">Correct quantities</button><button class="btn small danger" data-a="reverse">Reverse checkout</button></div>`}</div>`
        )
        .join("");
      $$("#coList [data-a]").forEach((b) => {
        const c = r.checkouts.find((x) => x.id === Number(b.closest(".co").dataset.id));
        b.onclick = () => (b.dataset.a === "reverse" ? reverseModal(c) : correctModal(c));
      });
    } catch (e) {
      box.innerHTML = `<div class="card notice bad">${esc(e.message)}</div>`;
    }
  }

  function reverseModal(c) {
    modal({
      title: `Reverse checkout #${c.id}`,
      submit: "Reverse and restore stock",
      danger: true,
      body: `<p>This returns everything below to inventory <b>once</b> and marks the checkout as reversed. It can’t be undone.</p>
        <ul>${c.lines.map((l) => `<li>${esc(l.label)} <b>× ${l.qty}</b></li>`).join("")}</ul>
        <label for="rv">Reason <span class="muted">(required)</span></label><input id="rv" maxlength="300" placeholder="e.g. Taken by mistake">`,
      async onSubmit(f) {
        await api(`/api/admin/checkouts/${c.id}/reverse`, { method: "POST", body: { reason: $("#rv", f).value } });
        toast("Checkout reversed — stock restored", "good");
        go("checkouts");
      },
    });
  }

  function correctModal(c) {
    modal({
      title: `Correct checkout #${c.id}`,
      submit: "Save correction",
      body: `<p>Change what was actually taken. Stock is adjusted by the difference.</p>
        ${c.lines.map((l) => `<div class="cart-line"><div style="flex:1 1 220px">${esc(l.label)}<div class="small muted">Originally ${l.original_qty}</div></div>
          <input data-line="${l.id}" inputmode="numeric" value="${l.qty}" style="width:90px" aria-label="Quantity for ${esc(l.label)}"></div>`).join("")}
        <label for="cr">Reason <span class="muted">(required)</span></label><input id="cr" maxlength="300" placeholder="e.g. Took 3, not 5">
        <p class="hint">To return everything, use “Reverse checkout” instead.</p>`,
      async onSubmit(f) {
        const lines = $$("[data-line]", f).map((i) => ({ line_id: Number(i.dataset.line), qty: i.value }));
        await api(`/api/admin/checkouts/${c.id}/correct`, { method: "POST", body: { lines, reason: $("#cr", f).value } });
        toast("Correction saved", "good");
        go("checkouts");
      },
    });
  }

  /* ---------------- audit log ---------------- */
  async function renderAudit() {
    $("#panel").innerHTML = `<div class="card"><div class="row spread"><h2 style="margin:0">Stock audit log</h2>
      <a class="btn small" href="/api/admin/export/audit.csv">Export CSV</a></div>
      <p class="muted small">Every change to a quantity — checkouts, reversals, restocks, removals, and counts.</p>
      <div class="row grow"><input id="aq" type="search" placeholder="Search item, person, or reason" aria-label="Search audit log">
      <select id="ak" aria-label="Type"><option value="">All types</option>${Object.entries(KIND).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
      <div><label for="afrom" class="small" style="margin-top:0">From</label><input id="afrom" type="date"></div>
      <div><label for="ato" class="small" style="margin-top:0">To</label><input id="ato" type="date"></div></div>
      <div class="table-wrap" style="margin-top:10px"><table><thead><tr><th>When</th><th>Item</th><th class="num">Change</th><th class="num">Before → After</th><th>Type</th><th>Who</th><th>Reason / checkout</th></tr></thead><tbody id="auBody"></tbody></table></div></div>`;
    const load = async () => {
      const p = new URLSearchParams();
      if ($("#aq").value.trim()) p.set("q", $("#aq").value.trim());
      if ($("#ak").value) p.set("kind", $("#ak").value);
      if ($("#afrom").value) p.set("from", localDayStartISO($("#afrom").value));
      if ($("#ato").value) p.set("to", localDayEndISO($("#ato").value));
      const r = await api("/api/admin/movements?" + p);
      $("#auBody").innerHTML = r.movements.length
        ? r.movements
            .map(
              (m) => `<tr><td class="small">${esc(fmtDate(m.created_at))}</td><td>${esc(m.label)}</td>
          <td class="num"><b style="color:${m.delta < 0 ? "var(--bad)" : "var(--good)"}">${signed(m.delta)}</b></td><td class="num">${m.prev_qty} → ${m.new_qty}</td>
          <td>${esc(KIND[m.kind] || m.kind)}</td><td>${esc(m.actor_name)}</td><td class="small">${esc(m.reason)}${m.checkout_id ? ` <span class="muted">(checkout #${m.checkout_id})</span>` : ""}</td></tr>`
            )
            .join("")
        : `<tr><td colspan="7" class="muted">Nothing yet.</td></tr>`;
    };
    $("#aq").oninput = debounce(load, 250);
    ["ak", "afrom", "ato"].forEach((id) => ($("#" + id).onchange = load));
    load();
  }

  /* ---------------- QR ---------------- */
  async function renderQR() {
    const st = await api("/api/admin/settings");
    const url = st.effective_url + "/";
    const local = /localhost|127\.0\.0\.1|0\.0\.0\.0|\.local\b/i.test(url) || url.startsWith("http://");
    $("#panel").innerHTML = `<div class="card"><h2>Checkout QR code</h2>
      <p>Anyone who scans this opens the checkout page in their phone’s browser — nothing to install.</p>
      <div class="qr-box"><div><img src="/api/admin/qr.svg?t=${Date.now()}" alt="QR code for the checkout page" id="qrimg">
        <div class="row no-print" style="margin-top:10px"><a class="btn primary small" href="/api/admin/qr.png?download=1" id="dlpng">Download PNG</a>
        <a class="btn small" href="/api/admin/qr.svg?download=1">Download SVG</a><button class="btn small" onclick="window.print()">Print</button></div></div>
      <div style="flex:1 1 260px"><p class="muted small">This code points to:</p><p><b style="word-break:break-all">${esc(url)}</b></p>
        ${local ? `<div class="notice warn"><b>Heads up:</b> this address looks temporary or private (${esc(url)}). Phones elsewhere can’t reach it. Deploy the app, open the dashboard from its real web address (or set the <b>Public web address</b> in Settings), and download the QR code again.</div>` : `<div class="notice good">This is a normal public address, so the printed code will keep working as long as this address does.</div>`}
        <p class="hint"><b>Keeping it permanent:</b> the QR code is just this web address. If you ever move hosting, point the same address at the new site (a custom domain makes this easy) and printed codes keep working.</p></div></div></div>`;
    $("#dlpng").addEventListener("click", () => setTimeout(() => {}, 0));
    const img = $("#qrimg");
    img.onerror = () => {
      img.replaceWith(Object.assign(document.createElement("div"), { className: "notice bad", textContent: "The QR image couldn’t be generated. Check that the public web address in Settings is a normal web address (under 100 characters)." }));
    };
  }

  /* ---------------- settings ---------------- */
  async function renderSettings() {
    const [st, us] = await Promise.all([api("/api/admin/settings"), api("/api/admin/users")]);
    const em = st.email;
    $("#panel").innerHTML = `
      <div class="card"><h2>Low-stock alert emails</h2>
        ${em.configured ? `<div class="notice good">Email service connected (${esc(em.backend)}${em.from ? ", sending as " + esc(em.from) : ""}).</div>` :
          `<div class="notice warn"><b>No email service is connected on the server yet.</b> Low-stock items will still be flagged here, but no emails can be sent until you add the email settings — README Step 4.</div>`}
        <label for="rc">Send alerts to <span class="muted">(one or more email addresses, separated by commas)</span></label>
        <textarea id="rc" placeholder="you@school.org, colleague@school.org">${esc(st.alert_recipients.join(", "))}</textarea>
        <div class="row" style="margin-top:10px"><button class="btn primary" id="saveRc">Save recipients</button>
        <button class="btn" id="testMail">Send a test email</button><button class="btn" id="sumMail">Email me the current reorder list</button></div>
        <p class="hint">You get <b>one</b> email when an item reaches its alert level — not one per checkout. The alert re-arms after the item is restocked above its level.</p>
        <h3 style="margin:16px 0 6px;font-size:1rem">Recent alerts</h3>
        <div class="table-wrap"><table><thead><tr><th>When</th><th>Item</th><th class="num">Qty / level</th><th>Trigger</th><th>Email</th></tr></thead><tbody>
        ${st.alerts.length ? st.alerts.map((a) => `<tr><td class="small">${esc(fmtDate(a.created_at))}</td><td>${esc(a.label)}</td><td class="num">${a.quantity} / ${a.threshold}</td><td class="small">${esc(a.trigger)}</td>
          <td class="small">${a.status === "sent" ? `<span class="badge in">Sent</span> <span class="muted">to ${esc(a.recipients)}</span>` : a.status === "failed" ? `<span class="badge out">Failed</span> ${esc(a.error)}` : a.status === "pending" ? "Sending…" : `<span class="badge low">Not sent</span> ${esc(a.error)}`}</td></tr>`).join("") : `<tr><td colspan="5" class="muted">No alerts yet.</td></tr>`}
        </tbody></table></div></div>

      <div class="card"><h2>Who can check out</h2>
        <div class="check"><input type="checkbox" id="guest" ${st.allow_guest ? "checked" : ""}><label for="guest">Allow checkout without an account (people type their name and email)</label></div>
        <button class="btn" id="saveGuest">Save</button></div>

      <div class="card"><h2>Public web address</h2>
        <p class="muted small">Used for the QR code and links in emails. Leave blank to use the address you’re visiting now (${esc(st.effective_url)}).</p>
        <input id="pub" placeholder="https://materials.yourschool.org" value="${esc(st.public_url)}">
        <button class="btn" id="savePub" style="margin-top:10px">Save</button></div>

      <div class="card"><h2>Accounts</h2>
        <div class="table-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th></th></tr></thead><tbody>
        ${us.users.map((u) => `<tr><td>${esc(u.name)}</td><td>${esc(u.email)}</td><td>${u.role === "owner" ? '<span class="badge neutral">Owner</span>' : "User"}</td>
          <td>${u.id === S.user.id ? '<span class="muted small">you</span>' : `<button class="btn small ghost" data-act-user="${u.id}" data-to="${u.active ? 0 : 1}">${u.active ? "Deactivate" : "Reactivate"}</button>`}</td></tr>`).join("")}
        </tbody></table></div><button class="btn" id="addUser" style="margin-top:10px">+ Add an account</button></div>

      <div class="card"><h2>Your password</h2><button class="btn" id="chPw">Change my password</button></div>

      <div class="card"><h2>Exports &amp; demo data</h2><div class="row">
        <a class="btn" href="/api/admin/export/inventory.csv">Inventory CSV</a><a class="btn" href="/api/admin/export/transactions.csv">Checkout history CSV</a>
        <a class="btn" href="/api/admin/export/audit.csv">Audit log CSV</a><button class="btn ghost" id="sample">Load sample inventory</button></div></div>`;

    const save = async (body, msg) => {
      try {
        await api("/api/admin/settings", { method: "PUT", body });
        toast(msg, "good");
        go("settings");
      } catch (e) {
        toast(e.message, "bad");
      }
    };
    $("#saveRc").onclick = () => save({ alert_recipients: $("#rc").value }, "Recipients saved");
    $("#saveGuest").onclick = () => save({ allow_guest: $("#guest").checked }, "Saved");
    $("#savePub").onclick = () => save({ public_url: $("#pub").value }, "Saved");
    const act = (id, path, okMsg) => {
      $(id).onclick = async (ev) => {
        const b = ev.currentTarget;
        b.disabled = true;
        try {
          const r = await api(path, { method: "POST", body: {} });
          toast(typeof okMsg === "function" ? okMsg(r) : okMsg, "good");
          go("settings");
        } catch (e) {
          toast(e.message, "bad");
          b.disabled = false;
        }
      };
    };
    act("#testMail", "/api/admin/test-email", (r) => "Test email sent to " + r.sent_to.join(", "));
    act("#sumMail", "/api/admin/resend-low-stock", (r) => `Emailed a list of ${r.count} item(s)`);
    act("#sample", "/api/admin/sample-data", (r) => `Added ${r.added} sample items`);
    $$("[data-act-user]").forEach(
      (b) =>
        (b.onclick = async () => {
          try {
            await api(`/api/admin/users/${b.dataset.actUser}/active`, { method: "POST", body: { active: b.dataset.to === "1" } });
            go("settings");
          } catch (e) {
            toast(e.message, "bad");
          }
        })
    );
    $("#addUser").onclick = () =>
      modal({
        title: "Add an account",
        submit: "Create account",
        body: `<label for="un">Name</label><input id="un"><label for="ue">Email</label><input id="ue" type="email" inputmode="email">
          <label for="ur">Role</label><select id="ur"><option value="user">User — can browse and check out</option><option value="owner">Owner — full dashboard access</option></select>
          <label for="up">Temporary password</label><input id="up" type="text" autocomplete="off"><div class="hint">At least 8 characters (10 for owners). Ask them to change it after signing in.</div>`,
        async onSubmit(f) {
          await api("/api/admin/users", { method: "POST", body: { name: $("#un", f).value, email: $("#ue", f).value, role: $("#ur", f).value, password: $("#up", f).value } });
          toast("Account created", "good");
          go("settings");
        },
      });
    $("#chPw").onclick = () =>
      modal({
        title: "Change password",
        submit: "Change password",
        body: `<label for="cp">Current password</label><input id="cp" type="password" autocomplete="current-password">
          <label for="np">New password</label><input id="np" type="password" autocomplete="new-password"><div class="hint">At least 10 characters.</div>`,
        async onSubmit(f) {
          await api("/api/account/password", { method: "POST", body: { current: $("#cp", f).value, new: $("#np", f).value } });
          toast("Password changed", "good");
        },
      });
  }

  init();
})();

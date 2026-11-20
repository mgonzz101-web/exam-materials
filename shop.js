/* Customer-facing checkout page: browse -> cart -> checkout -> confirmation. */
(() => {
  const st = {
    session: null,
    items: [],
    byId: new Map(),
    cart: LS.get("cart_v1", {}), // { itemId: qty } — lives only in this browser until checkout succeeds
    kc: LS.get("kitcart_v1", {}), // { itemId: [kitId, ...] } — specific numbered test kits chosen
    pending: {}, // qty chosen on the stepper before "Add"
    q: "",
    type: "all",
    exam: "",
    edition: "",
    problems: null,
    submitting: false,
  };

  const KIT = "test_kit";
  const saveCart = () => {
    for (const k of Object.keys(st.kc)) if (!st.kc[k] || !st.kc[k].length) delete st.kc[k];
    LS.set("cart_v1", st.cart);
    LS.set("kitcart_v1", st.kc);
  };
  const cartIds = () => [...new Set([...Object.keys(st.cart), ...Object.keys(st.kc).filter((k) => st.kc[k].length)].map(Number))];
  const cartQty = (id) => (st.kc[id] && st.kc[id].length ? st.kc[id].length : st.cart[id] || 0);
  const cartUnits = () => cartIds().reduce((a, id) => a + cartQty(id), 0);
  const avail = (it) => (it ? it.quantity : 0);
  const kitNums = (it) => new Map((it.kits || []).map((k) => [k.id, k.number]));
  const contentsHtml = (it) =>
    it.contents && it.contents.length
      ? `<details class="small kit-contents"><summary>What's in this kit (${it.contents.length})</summary><ul>${it.contents.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></details>`
      : "";

  /* ---------- loading ---------- */
  async function loadItems() {
    const r = await api("/api/items");
    st.items = r.items;
    st.byId = new Map(r.items.map((i) => [i.id, i]));
  }

  function show(view) {
    for (const v of ["browse", "cart", "receipt", "return"]) $("#v-" + v).hidden = v !== view;
    st.view = view;
    updateBar();
    window.scrollTo(0, 0);
  }

  /* ---------- account area ---------- */
  function renderAcct() {
    const u = st.session.user;
    const el = $("#acct");
    if (u) {
      el.innerHTML = `${u.role === "owner" ? '<a class="btn small" href="/admin">Dashboard</a>' : ""}
        <span class="small muted">${esc(u.name)}</span><button class="btn small ghost" id="outBtn">Sign out</button>`;
      $("#outBtn").onclick = async () => {
        const r = await api("/api/logout", { method: "POST", body: {} });
        CSRF = r.csrf;
        st.session.user = null;
        renderAcct();
        fillWho();
        toast("Signed out");
      };
    } else {
      el.innerHTML = `<button class="btn small" id="inBtn">Sign in</button>`;
      $("#inBtn").onclick = openAuth;
    }
  }

  function openAuth() {
    let mode = "in";
    const m = modal({
      title: "Sign in",
      submit: "Continue",
      body: `<div class="chips" role="group"><button type="button" class="chip" data-m="in" aria-pressed="true">Sign in</button>
        <button type="button" class="chip" data-m="up" aria-pressed="false">Create account</button></div>
        <div id="nmWrap" hidden><label for="an">Your name</label><input id="an" autocomplete="name"></div>
        <label for="ae">Email</label><input id="ae" type="email" autocomplete="email" inputmode="email">
        <label for="ap">Password</label><input id="ap" type="password" autocomplete="current-password">
        <p class="hint" id="apHint" hidden>At least 8 characters.</p>
        <p class="hint">An account is optional — it just fills in your name and email.</p>`,
      onMount(f) {
        $$("[data-m]", f).forEach((b) =>
          b.addEventListener("click", () => {
            mode = b.dataset.m;
            $$("[data-m]", f).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
            $("#nmWrap", f).hidden = mode !== "up";
            $("#apHint", f).hidden = mode !== "up";
            $("#ap", f).autocomplete = mode === "up" ? "new-password" : "current-password";
          })
        );
      },
      async onSubmit(f) {
        const body = { email: $("#ae", f).value, password: $("#ap", f).value };
        if (mode === "up") body.name = $("#an", f).value;
        const r = await api(mode === "up" ? "/api/register" : "/api/login", { method: "POST", body });
        CSRF = r.csrf;
        st.session.user = r.user;
        renderAcct();
        fillWho();
        toast("Signed in as " + r.user.name, "good");
      },
    });
  }

  /* ---------- browse ---------- */
  function matches(it) {
    if (st.type !== "all" && it.material_type !== st.type) return false;
    if (st.exam && it.exam !== st.exam) return false;
    if (st.edition && it.edition !== st.edition) return false;
    const q = st.q.trim().toLowerCase();
    if (q) {
      const hay = `${it.exam} ${it.edition} ${it.type_name} ${it.form_name} ${it.location}`.toLowerCase();
      return q.split(/\s+/).every((w) => hay.includes(w));
    }
    return true;
  }

  function renderFilters() {
    const exams = [...new Set(st.items.map((i) => i.exam))];
    const eds = [...new Set(st.items.filter((i) => !st.exam || i.exam === st.exam).map((i) => i.edition).filter(Boolean))];
    if (st.exam && !exams.includes(st.exam)) st.exam = "";
    if (st.edition && !eds.includes(st.edition)) st.edition = "";
    $("#examSel").innerHTML =
      `<option value="">All exams</option>` + exams.map((e) => `<option ${e === st.exam ? "selected" : ""}>${esc(e)}</option>`).join("");
    $("#edSel").innerHTML =
      `<option value="">All editions</option>` + eds.map((e) => `<option ${e === st.edition ? "selected" : ""}>${esc(e)}</option>`).join("");
  }

  function renderList() {
    const rows = st.items.filter(matches);
    const box = $("#list");
    if (!st.items.length) {
      box.innerHTML = `<div class="card">No materials are listed yet. Please check back soon.</div>`;
      return;
    }
    if (!rows.length) {
      box.innerHTML = `<div class="card">Nothing matches your search. Try fewer words or clear the filters.</div>`;
      return;
    }
    let html = "",
      last = null;
    for (const it of rows) {
      if (it.exam !== last) {
        html += `<h2 class="exam-h">${esc(it.exam)}</h2>`;
        last = it.exam;
      }
      const isKit = it.material_type === KIT;
      const inCart = cartQty(it.id);
      const room = it.quantity - inCart;
      const want = Math.max(1, Math.min(st.pending[it.id] || 1, Math.max(room, 1)));
      const out = it.quantity <= 0;
      const stock = out
        ? badge("out")
        : it.status === "low"
        ? `<span class="badge low">Low stock · ${it.quantity} left</span>`
        : `<span class="badge in">In stock · ${it.quantity}</span>`;
      html += `<div class="item ${out ? "is-out" : ""}" data-id="${it.id}">
        <div class="item-main">
          <div class="item-title">${esc(it.type_name)}${it.edition ? " · " + esc(it.edition) : ""}</div>
          <div class="item-sub">${esc(it.form_name || "")}${it.form_name && it.location ? " · " : ""}${it.location ? "Stored: " + esc(it.location) : ""}</div>
          ${stock}${inCart ? ` <span class="badge neutral">${inCart} in cart</span>` : ""}
          ${isKit ? contentsHtml(it) : ""}
        </div>
        <div class="item-act">
          ${
            out
              ? `<span class="muted"><b>Out of stock</b></span>`
              : isKit
              ? `<div class="kitpick"><div class="small muted">Tap the kit number you're taking:</div><div class="kitgrid">${(it.kits || [])
                  .map((k) => `<button type="button" class="chip" data-kit="${k.id}" aria-pressed="${(st.kc[it.id] || []).includes(k.id)}">#${k.number}</button>`)
                  .join("")}</div></div>`
              : room <= 0
              ? `<span class="muted small">All available are in your cart</span>`
              : `<div class="stepper"><button type="button" data-act="dec" aria-label="Fewer">−</button>
                 <input data-qty inputmode="numeric" value="${want}" aria-label="Quantity for ${esc(it.exam)}">
                 <button type="button" data-act="inc" aria-label="More">+</button></div>
                 <button type="button" class="btn primary" data-act="add">Add</button>`
          }
        </div></div>`;
    }
    box.innerHTML = html;
  }

  $("#list").addEventListener("click", (e) => {
    const kb = e.target.closest("[data-kit]");
    if (kb) {
      const id = Number(kb.closest(".item").dataset.id);
      const kid = Number(kb.dataset.kit);
      const cur = st.kc[id] || [];
      st.kc[id] = cur.includes(kid) ? cur.filter((x) => x !== kid) : [...cur, kid];
      saveCart();
      renderList();
      updateBar();
      return;
    }
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const row = b.closest(".item");
    const id = Number(row.dataset.id);
    const it = st.byId.get(id);
    const room = it.quantity - (st.cart[id] || 0);
    const input = $("[data-qty]", row);
    let v = parseInt(input.value, 10) || 1;
    if (b.dataset.act === "dec") v = Math.max(1, v - 1);
    if (b.dataset.act === "inc") v = Math.min(room, v + 1);
    v = Math.max(1, Math.min(v, room));
    input.value = v;
    st.pending[id] = v;
    if (b.dataset.act === "add") {
      st.cart[id] = (st.cart[id] || 0) + v;
      st.pending[id] = 1;
      saveCart();
      toast(`Added ${v} × ${it.exam}`, "good");
      renderList();
      updateBar();
    }
  });
  $("#list").addEventListener("change", (e) => {
    if (!e.target.matches("[data-qty]")) return;
    const row = e.target.closest(".item");
    const id = Number(row.dataset.id);
    const it = st.byId.get(id);
    const room = it.quantity - (st.cart[id] || 0);
    const v = Math.max(1, Math.min(parseInt(e.target.value, 10) || 1, room));
    e.target.value = v;
    st.pending[id] = v;
  });

  $("#q").addEventListener("input", (e) => ((st.q = e.target.value), renderList()));
  $("#examSel").addEventListener("change", (e) => ((st.exam = e.target.value), (st.edition = ""), renderFilters(), renderList()));
  $("#edSel").addEventListener("change", (e) => ((st.edition = e.target.value), renderList()));
  $("#typeChips").addEventListener("click", (e) => {
    const b = e.target.closest("[data-type]");
    if (!b) return;
    st.type = b.dataset.type;
    $$("#typeChips .chip").forEach((c) => c.setAttribute("aria-pressed", String(c === b)));
    renderList();
  });

  function updateBar() {
    const n = cartUnits();
    const showBar = n > 0 && st.view === "browse";
    $("#bar").hidden = !showBar;
    document.body.classList.toggle("has-bar", showBar);
    const kinds = cartIds().length;
    $("#barCount").textContent = `${n} item${n === 1 ? "" : "s"} (${kinds} kind${kinds === 1 ? "" : "s"})`;
  }
  $("#barBtn").onclick = openCart;

  /* ---------- cart ---------- */
  function cartIssues() {
    const bad = {};
    for (const id of cartIds()) {
      const it = st.byId.get(id);
      if (!it) bad[id] = "No longer listed";
      else if (it.material_type === KIT) {
        const ok = kitNums(it);
        const gone = (st.kc[id] || []).filter((k) => !ok.has(k));
        if (gone.length) bad[id] = "Someone else already took a kit you picked";
      } else if (it.quantity <= 0) bad[id] = "Out of stock";
      else if (st.cart[id] > it.quantity) bad[id] = `Only ${it.quantity} available`;
    }
    return bad;
  }

  function fillWho() {
    const u = st.session.user;
    const saved = LS.get("who_v1", {});
    $("#nm").value = u ? u.name : saved.name || $("#nm").value || "";
    $("#em").value = u ? u.email : saved.email || $("#em").value || "";
    $("#nm").readOnly = $("#em").readOnly = !!u;
    $("#whoNote").textContent = u ? "Using your signed-in profile." : "";
    const blocked = !u && !st.session.allow_guest;
    $("#goBtn").disabled = blocked;
    if (blocked) $("#whoNote").innerHTML = "Please <b>sign in</b> (top right) to check out.";
  }

  function renderCart() {
    const ids = cartIds();
    const bad = cartIssues();
    const probMap = {};
    (st.problems || []).forEach((p) => (probMap[p.item_id] = p));
    if (!ids.length) {
      $("#cartCard").innerHTML = `<p>Your cart is empty.</p>`;
      $("#goBtn").disabled = true;
      return;
    }
    $("#cartCard").innerHTML = ids
      .map((id) => {
        const it = st.byId.get(id);
        const title = it ? `${esc(it.exam)} — ${esc(it.type_name)}` : `Item #${id}`;
        const sub = it ? [it.edition, it.form_name].filter(Boolean).map(esc).join(" · ") : "";
        const msg = bad[id] || (probMap[id] ? (probMap[id].available ? `Only ${probMap[id].available} available now` : "Not available") : "");
        const max = avail(it);
        if (it && it.material_type === KIT) {
          const nums = kitNums(it);
          const chips = (st.kc[id] || [])
            .map((k) => `<button type="button" class="chip" data-kitrm="${k}" aria-label="Remove kit ${nums.get(k) || ""}" ${nums.has(k) ? "" : 'style="opacity:.5;text-decoration:line-through"'}>#${nums.get(k) || "?"} ✕</button>`)
            .join("");
          return `<div class="cart-line ${msg ? "bad" : ""}" data-id="${id}">
            <div style="flex:1 1 200px"><b>${title}</b><div class="item-sub">${sub}</div>${msg ? `<b style="color:var(--bad)">${esc(msg)}</b>` : ""}
            <div class="kitgrid" style="margin-top:6px">${chips}</div></div>
            <div class="item-act"><button type="button" class="btn small ghost" data-act="rm">Remove</button></div></div>`;
        }
        return `<div class="cart-line ${msg ? "bad" : ""}" data-id="${id}">
          <div style="flex:1 1 200px"><b>${title}</b><div class="item-sub">${sub}</div>${msg ? `<b style="color:var(--bad)">${esc(msg)}</b>` : ""}</div>
          <div class="item-act">
            <div class="stepper"><button type="button" data-act="dec" aria-label="Fewer">−</button>
            <input data-qty inputmode="numeric" value="${st.cart[id]}" aria-label="Quantity">
            <button type="button" data-act="inc" aria-label="More" ${st.cart[id] >= max ? "disabled" : ""}>+</button></div>
            <button type="button" class="btn small ghost" data-act="rm">Remove</button>
          </div></div>`;
      })
      .join("");
    const issues = Object.keys(bad).length;
    const needsSignIn = !st.session.user && !st.session.allow_guest;
    $("#goBtn").disabled = st.submitting || issues > 0 || needsSignIn;
    renderProblems(issues);
  }

  function renderProblems(issues) {
    const p = st.problems;
    let html = "";
    if (p && p.length) {
      html = `<div class="notice bad"><b>Nothing was taken.</b> Some items changed while you were checking out:<ul>` +
        p.map((x) => `<li>${esc(x.label)} — ${x.reason === "kit_unavailable" ? "someone else just took this kit" : x.available ? `only ${x.available} left (you asked for ${x.requested})` : "not available"}</li>`).join("") +
        `</ul><button type="button" class="btn small primary" id="fixCart">Adjust my cart to what’s available</button></div>`;
    } else if (issues) {
      html = `<div class="notice warn">Some items need attention (highlighted above) before you can check out.
        <br><button type="button" class="btn small primary" id="fixCart" style="margin-top:8px">Adjust my cart to what’s available</button></div>`;
    }
    $("#problems").innerHTML = html;
    const fix = $("#fixCart");
    if (fix)
      fix.onclick = () => {
        for (const id of cartIds()) {
          const it = st.byId.get(id);
          if (it && it.material_type === KIT) {
            const ok = kitNums(it);
            st.kc[id] = (st.kc[id] || []).filter((k) => ok.has(k));
            delete st.cart[id];
          } else if (!it || it.quantity <= 0) delete st.cart[id];
          else st.cart[id] = Math.min(st.cart[id], it.quantity);
        }
        st.problems = null;
        saveCart();
        renderCart();
        toast("Cart updated");
      };
  }

  async function openCart() {
    $("#cartErr").hidden = true;
    st.problems = null;
    try {
      await loadItems();
    } catch (e) {}
    fillWho();
    renderCart();
    show("cart");
  }

  $("#backBrowse").onclick = async () => {
    show("browse");
    renderFilters();
    renderList();
  };

  $("#cartCard").addEventListener("click", (e) => {
    const kr = e.target.closest("[data-kitrm]");
    if (kr) {
      const id = Number(kr.closest(".cart-line").dataset.id);
      st.kc[id] = (st.kc[id] || []).filter((k) => k !== Number(kr.dataset.kitrm));
      st.problems = null;
      saveCart();
      renderCart();
      updateBar();
      return;
    }
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const id = Number(b.closest(".cart-line").dataset.id);
    const it = st.byId.get(id);
    if (b.dataset.act === "rm") {
      delete st.cart[id];
      delete st.kc[id];
    }
    if (b.dataset.act === "dec") st.cart[id] = Math.max(1, st.cart[id] - 1);
    if (b.dataset.act === "inc") st.cart[id] = Math.min(avail(it), st.cart[id] + 1);
    st.problems = null;
    saveCart();
    renderCart();
    updateBar();
  });
  $("#cartCard").addEventListener("change", (e) => {
    if (!e.target.matches("[data-qty]")) return;
    const id = Number(e.target.closest(".cart-line").dataset.id);
    const v = parseInt(e.target.value, 10);
    if (!v || v < 1) delete st.cart[id];
    else st.cart[id] = v; // too-high values get flagged by cartIssues()
    st.problems = null;
    saveCart();
    renderCart();
  });

  /* ---------- checkout ---------- */
  $("#goBtn").addEventListener("click", async () => {
    if (st.submitting) return; // ignores double taps
    const err = $("#cartErr");
    err.hidden = true;
    const name = $("#nm").value.trim(),
      email = $("#em").value.trim();
    if (!name) return fail("Please enter your name.");
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return fail("Please enter a valid email address.");
    const lines = cartIds().map((id) =>
      st.kc[id] && st.kc[id].length ? { item_id: id, kit_ids: st.kc[id] } : { item_id: id, qty: st.cart[id] }
    );
    if (!lines.length) return fail("Your cart is empty.");

    function fail(m) {
      err.textContent = m;
      err.hidden = false;
    }

    // One token per checkout attempt. If the network drops and the person taps again,
    // the same token is re-sent and the server will never deduct twice.
    let token = LS.get("co_token", null);
    if (!token) {
      token = uuid();
      LS.set("co_token", token);
    }
    st.submitting = true;
    const btn = $("#goBtn");
    btn.disabled = true;
    btn.textContent = "Completing…";
    try {
      const r = await api("/api/checkout", { method: "POST", body: { token, name, email, lines } });
      LS.del("co_token");
      LS.set("who_v1", { name, email });
      st.cart = {};
      st.kc = {};
      saveCart();
      st.problems = null;
      location.hash = "receipt=" + r.receipt.token;
      showReceipt(r.receipt, r.duplicate);
    } catch (e) {
      if (e.status === 409 && e.data && e.data.problems) {
        st.problems = e.data.problems;
        try {
          await loadItems();
        } catch (_) {}
        renderCart(); // the explanation box lists exactly what changed
      } else {
        fail(e.message);
      }
    } finally {
      st.submitting = false;
      btn.textContent = "Complete Checkout";
      if (st.view === "cart") renderCart();
    }
  });

  function showReceipt(r, duplicate) {
    const status = r.status === "reversed" ? `<div class="notice warn">This checkout was reversed by an administrator and the materials were returned to inventory.</div>` : "";
    $("#receiptCard").innerHTML = `<div class="tick" aria-hidden="true">✓</div>
      <h2>${r.status === "reversed" ? "Checkout reversed" : "Checkout complete"}</h2>
      <p class="muted">Receipt #${r.id} · ${esc(fmtDate(r.created_at))}<br>Taken by ${esc(r.name)}</p>
      ${duplicate ? `<div class="notice">This checkout was already recorded — nothing was taken twice.</div>` : ""}
      ${status}
      <ul class="receipt-list">${r.lines
        .map((l) => `<li><span>${esc(l.exam)}<br><span class="muted small">${esc([l.edition, l.type_name, l.form_name].filter(Boolean).join(" · "))}</span>${l.kits && l.kits.length ? `<br><b>Kit ${l.kits.map((k) => "#" + k.number).join(", ")}</b>` : ""}</span><b>× ${l.qty}</b></li>`)
        .join("")}</ul>
      <p><b>${r.total_units} total</b></p>
      ${r.lines.some((l) => l.material_type === "test_kit") ? `<div class="notice"><b>Test kits need to come back.</b> When you're done, open this page again, tap <b>Returning a test kit</b>, and tell us if anything is missing.</div>` : ""}
      <button class="btn primary block" id="again">Start a new checkout</button>`;
    $("#again").onclick = async () => {
      history.replaceState(null, "", location.pathname);
      await loadItems();
      renderFilters();
      renderList();
      show("browse");
    };
    show("receipt");
  }

  /* ---------- returning test kits ---------- */
  function openReturn() {
    const u = st.session.user;
    $("#retEmail").value = u ? u.email : (LS.get("who_v1", {}).email || $("#retEmail").value || "");
    $("#retEmail").readOnly = !!u;
    $("#retList").innerHTML = "";
    $("#retDone").hidden = true;
    $("#retErr").hidden = true;
    show("return");
  }
  $("#toReturn").onclick = openReturn;
  $("#retBack").onclick = async () => {
    show("browse");
    try { await loadItems(); } catch (e) {}
    renderFilters();
    renderList();
  };

  async function findKits() {
    const err = $("#retErr");
    err.hidden = true;
    $("#retDone").hidden = true;
    try {
      const r = await api("/api/loans/lookup", { method: "POST", body: { email: $("#retEmail").value.trim() } });
      drawKits(r.loans);
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  }
  $("#retFind").onclick = findKits;
  $("#retEmail").addEventListener("keydown", (e) => e.key === "Enter" && findKits());

  function drawKits(loans) {
    const box = $("#retList");
    if (!loans.length) {
      box.innerHTML = `<div class="card">No test kits are checked out under that email. Double-check the address you used at checkout.</div>`;
      return;
    }
    box.innerHTML = `<div class="card"><h2>Kits you still have</h2>
      <p class="muted small">Untick any kit you're keeping longer. If something is missing or damaged, tap <b>Report a problem</b> on that kit.</p>${loans
      .map(
        (l) => `<div class="retkit" data-kit="${l.kit_id}">
          <label class="retkit-head"><input type="checkbox" data-ret checked> <span><b>${esc(l.label)}</b><br><span class="item-sub">Taken ${esc(fmtDate(l.created_at))}</span></span></label>
          <button type="button" class="btn small ghost" data-prob>Report a problem</button>
          <div class="retkit-prob" hidden>
            ${l.contents && l.contents.length ? `<div class="small"><b>Tick anything that is missing or damaged:</b></div>
            ${l.contents.map((c, i) => `<label class="chk"><input type="checkbox" data-miss value="${esc(c)}"> <span>${esc(c)}</span></label>`).join("")}` : ""}
            <label class="small" style="margin-top:8px">Anything else we should know? (optional)</label>
            <input data-note maxlength="300" placeholder="e.g. cover is cracked">
          </div></div>`
      )
      .join("")}
      <p id="retErr2" class="form-error" role="alert" hidden></p>
      <button class="btn primary block" id="retGo" style="margin-top:14px">Return these kits</button></div>`;
  }

  $("#retList").addEventListener("click", (e) => {
    const pb = e.target.closest("[data-prob]");
    if (pb) {
      const panel = pb.parentElement.querySelector(".retkit-prob");
      panel.hidden = !panel.hidden;
      pb.textContent = panel.hidden ? "Report a problem" : "Hide problem report";
    }
  });
  $("#retList").addEventListener("click", async (e) => {
    if (e.target.id !== "retGo") return;
    const btn = e.target;
    const err = $("#retErr2");
    err.hidden = true;
    const kits = $$("#retList .retkit")
      .filter((r) => $("[data-ret]", r).checked)
      .map((r) => ({
        kit_id: Number(r.dataset.kit),
        missing: $$("[data-miss]:checked", r).map((c) => c.value),
        note: $("[data-note]", r).value.trim(),
      }));
    if (!kits.length) {
      err.textContent = "Choose at least one kit to return.";
      err.hidden = false;
      return;
    }
    let token = LS.get("ret_token", null);
    if (!token) { token = uuid(); LS.set("ret_token", token); }
    btn.disabled = true;
    btn.textContent = "Returning…";
    try {
      const r = await api("/api/return", { method: "POST", body: { token, email: $("#retEmail").value.trim(), kits } });
      LS.del("ret_token");
      $("#retList").innerHTML = "";
      const d = $("#retDone");
      d.hidden = false;
      const flagged = (r.returned || []).some((x) => x.status !== "available");
      d.innerHTML = `<div class="tick" aria-hidden="true">✓</div><h2>Thank you — returned!</h2>
        ${r.duplicate ? `<p class="muted">This return was already recorded.</p>` : `<ul class="receipt-list">${r.returned.map((x) => `<li><span>${esc(x.label)}</span><b>${x.status === "available" ? "OK" : "Reported"}</b></li>`).join("")}</ul>`}
        ${flagged ? `<div class="notice">Thanks for letting us know about the problem. The owner has been notified and will check that kit.</div>` : ""}
        <button class="btn primary block" id="retAgain">Done</button>`;
      $("#retAgain").onclick = () => $("#retBack").click();
    } catch (e2) {
      err.textContent = e2.message;
      err.hidden = false;
      btn.disabled = false;
      btn.textContent = "Return these kits";
      if (e2.status === 409 || e2.status === 404) { LS.del("ret_token"); findKits(); }
    }
  });

  /* ---------- start ---------- */
  async function init() {
    try {
      st.session = await loadSession();
      renderAcct();
      const h = location.hash.match(/^#receipt=([A-Za-z0-9-]+)$/);
      if (h) {
        try {
          const r = await api("/api/receipt/" + h[1]);
          return showReceipt(r.receipt, false);
        } catch (e) {
          history.replaceState(null, "", location.pathname);
        }
      }
      await loadItems();
      renderFilters();
      renderList();
      show("browse");
    } catch (e) {
      $("#list").innerHTML = `<div class="card notice bad">${esc(e.message)} <button class="btn small" onclick="location.reload()">Try again</button></div>`;
    }
  }
  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState === "visible" && st.view === "browse" && st.session) {
      try {
        await loadItems();
        renderFilters();
        renderList();
      } catch (e) {}
    }
  });
  init();
})();

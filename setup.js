/* Step 1 of first-time setup: create the owner account. The rest (inventory, alert emails,
   QR code) is a guided checklist on the owner dashboard. */
(async () => {
  const app = $("#app");
  let s;
  try {
    s = await loadSession();
  } catch (e) {
    app.innerHTML = `<p class="notice bad">${esc(e.message)}</p>`;
    return;
  }
  if (!s.needs_setup) {
    app.innerHTML = `<h2>Already set up</h2><p>An owner account already exists.</p><a class="btn primary" href="/admin">Go to the owner dashboard</a>`;
    return;
  }
  if (!s.setup_key_configured) {
    app.innerHTML = `<h2>One thing first</h2>
      <p class="notice warn">For safety, setup is locked until you add a <b>SETUP_KEY</b> setting on your server.
      Anyone who finds this page could otherwise claim the owner account.</p>
      <p>Add an environment variable named <code>SETUP_KEY</code> (any long random phrase), redeploy, then reload this page.
      The README (Step 3) shows exactly where.</p>`;
    return;
  }
  app.innerHTML = `<h2>Step 1 of 4 — Create the owner account</h2>
    <p class="muted">This account can manage inventory and see every checkout. You’ll add inventory, alert emails and the QR code next.</p>
    <form id="f" novalidate>
      <label for="k">Setup key</label>
      <input id="k" type="password" autocomplete="off">
      <div class="hint">The SETUP_KEY value you set on the server.</div>
      <label for="n">Your name</label><input id="n" autocomplete="name">
      <label for="e">Your email</label><input id="e" type="email" autocomplete="email" inputmode="email">
      <label for="p">Password</label><input id="p" type="password" autocomplete="new-password">
      <div class="hint">At least 10 characters.</div>
      <label for="p2">Confirm password</label><input id="p2" type="password" autocomplete="new-password">
      <p class="form-error" id="err" role="alert" hidden></p>
      <button class="btn primary block" style="margin-top:16px" id="go">Create owner account</button>
    </form>`;
  $("#f").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = $("#err");
    err.hidden = true;
    if ($("#p").value !== $("#p2").value) {
      err.textContent = "The two passwords don’t match.";
      err.hidden = false;
      return;
    }
    $("#go").disabled = true;
    try {
      const r = await api("/api/setup", {
        method: "POST",
        body: { setup_key: $("#k").value, name: $("#n").value, email: $("#e").value, password: $("#p").value },
      });
      CSRF = r.csrf;
      location.href = "/admin";
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
      $("#go").disabled = false;
    }
  });
})();

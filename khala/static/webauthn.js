// Passkey button: WebAuthn options from the server → browser creates or uses a passkey → result back → redirect.
// Only on sign-in, two-step and security pages. No passkey support: button stays hidden; email code and TOTP still work.
(function () {
  "use strict";

  function toBuf(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var bin = atob(s), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }

  function fromBuf(buf) {
    var bytes = new Uint8Array(buf), bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function prepare(o, create) {
    o.challenge = toBuf(o.challenge);
    if (create) o.user.id = toBuf(o.user.id);
    (o.excludeCredentials || []).concat(o.allowCredentials || []).forEach(function (c) { c.id = toBuf(c.id); });
    return o;
  }

  function serialize(c) {
    var r = c.response, out = {
      id: c.id, rawId: fromBuf(c.rawId), type: c.type,
      response: { clientDataJSON: fromBuf(r.clientDataJSON) },
      clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {}
    };
    if (r.attestationObject) {
      out.response.attestationObject = fromBuf(r.attestationObject);
      if (r.getTransports) out.response.transports = r.getTransports();
    } else {
      out.response.authenticatorData = fromBuf(r.authenticatorData);
      out.response.signature = fromBuf(r.signature);
      if (r.userHandle) out.response.userHandle = fromBuf(r.userHandle);
    }
    return out;
  }

  async function post(url, body) {
    var r = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    var j = {};
    try { j = await r.json(); } catch (e) { /* non-JSON error page */ }
    if (!r.ok) throw new Error(j.error || "The server refused the request.");
    return j;
  }

  async function run(btn) {
    var form = btn.closest("form"), status = form.querySelector("[data-passkey-status]"), fields = {};
    form.querySelectorAll("input[type=hidden], input[data-passkey-field]").forEach(function (i) {
      fields[i.name] = i.value;
    });
    fields.mode = btn.dataset.mode || "";
    if (status) status.textContent = "";
    btn.disabled = true;
    try {
      var create = btn.dataset.passkey === "register";
      var opts = await post(btn.dataset.options, fields);
      var cred = create
        ? await navigator.credentials.create({ publicKey: prepare(opts, true) })
        : await navigator.credentials.get({ publicKey: prepare(opts, false) });
      var done = await post(btn.dataset.verify, Object.assign({}, fields, { credential: serialize(cred) }));
      window.location.assign(done.redirect);
    } catch (e) {
      if (status) status.textContent = e.name === "NotAllowedError" ? "Cancelled or timed out." : e.message;
      btn.disabled = false;
    }
  }

  if (window.PublicKeyCredential && navigator.credentials) {
    document.querySelectorAll("[data-passkey-only]").forEach(function (el) { el.hidden = false; });
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-passkey]");
    if (!btn) return;
    e.preventDefault();
    run(btn);
  });
})();

// The callback page for agents with no callback of their own: the authorization response arrives in the fragment
// (never sent to the server), is shown as the callback URL to hand back to the agent, then dropped from the address bar.
(function () {
  "use strict";
  var hash = location.hash.replace(/^#/, "");
  var params = new URLSearchParams(hash);
  var out = document.getElementById("callback-url"), box = document.getElementById("callback-result");
  var none = document.getElementById("callback-none"), copy = document.getElementById("callback-copy");
  history.replaceState(null, "", location.pathname);
  if (!params.has("code") && !params.has("error")) return;
  out.value = location.origin + location.pathname + "?" + hash;
  if (params.has("error")) document.getElementById("callback-error").hidden = false;
  none.hidden = true;
  box.hidden = false;
  copy.addEventListener("click", function () {
    out.select();
    var done = function () { copy.textContent = "Copied"; };
    if (navigator.clipboard) navigator.clipboard.writeText(out.value).then(done, function () {});
    else if (document.execCommand("copy")) done();
  });
})();

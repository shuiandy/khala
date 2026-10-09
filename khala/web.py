"""Web admin, mounted under /app, in the same process and on the same domain as MCP.

Sign-in reuses the email verification code; sessions are stored apart from OAuth tokens, and only a hash of the
session ID is kept. Every request that changes data is a form POST, checked against a synchronizer CSRF token and
Origin. In phase one records are read-only, and bodies are shown as escaped plain text, not rendered Markdown.
"""
import asyncio
import csv
import hmac
import io
import json
import re
import time
from urllib.parse import quote, urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from . import catalog, clock, rules, undo
from .access import RANK, Principal
from .hooks import LOG as PUSH_LOG
from .inbox import InboxError
from .store import Conflict
from .device import DAYS as DEVICE_DAYS
from .db import REVIEW_MODES, ROLES, SCOPE_ID, digest, new_secret
from .auth import AuthError, otpauth_uri, qr_svg
from .login import check_code, client_ip, json_error, read_json, send_code
from .login import same_origin as request_from_site
from .ui import CALLBACK_JS, brand_icon, render, script, stylesheet

SESSION_COOKIE = "ms_session"
LOGIN_COOKIE = "ms_login"
SESSION_IDLE = 12 * 3600
SESSION_MAX = 7 * 86400
REAUTH_WINDOW = 10 * 60

ENROL_OPEN = ("/app/security", "/app/reauth", "/app/logout")
MESSAGES = {
    "scope-created": "Scope created.", "scope-saved": "Scope settings saved.", "member-added": "Access granted.",
    "member-updated": "Member updated.", "member-removed": "Access removed.", "invite-sent": "Invitation sent.",
    "invite-cancelled": "Invitation cancelled.", "archived": "Scope archived.", "unarchived": "Scope restored.",
    "device-approved": "Approved. The command line on your computer picks up its token now.",
    "device-denied": "Denied. The command line gets nothing.",
    "enrol": "This server requires a passkey or an authenticator app. Add one to continue.",
    "auto-load-on": "Agents now load this scope in every session.", "auto-load-off": "This scope is no longer auto-loaded.",
    "transferred": "Ownership transferred.", "left": "You left the scope.", "agent-saved": "Agent updated.",
    "agent-revoked": "Agent revoked. Its next request will be refused.", "account-updated": "Account updated.",
    "mail-sent": "Test email sent.", "mail-failed": "Could not send the test email; check the SMTP settings.",
    "welcome": "Welcome. Your account is ready.", "reauthed": "Confirmed. Try the action again.",
    "passkey-added": "Passkey added.", "passkey-removed": "Passkey removed.", "passkey-renamed": "Passkey renamed.",
    "totp-enabled": "Authenticator app is on.", "totp-removed": "Authenticator app is off.",
    "approved": "Approved. The record now has the change.", "rejected": "Proposal rejected.",
    "stale": "The record changed after this proposal was made, so it was not applied. The notes can be merged again.",
    "undone": "Change undone.", "resolved": "Conflict resolved.", "saved": "Saved.",
    "candidate-rejected": "Rejected. The record was removed; its history keeps it.",
    "deprecated": "Marked outdated.", "deleted": "Record deleted. It stays in the history.",
}


def safe_next(value, default="/app"):
    """Only accept on-site /app paths, to prevent open redirects."""
    if isinstance(value, str) and value.startswith("/app") and not value.startswith("//") and "\\" not in value:
        return value
    return default


def ceiling_summary(raw):
    if not raw:
        return "Everything the account can reach"
    c = json.loads(raw)
    if not c:
        return "Nothing"
    return ", ".join("%s (%s)" % (k, "read & write" if v == "rw" else "read") for k, v in sorted(c.items()))


def diff_lines(patch):
    out = []
    for line in patch.splitlines():
        if line.startswith(("diff --git", "index ", "--- ", "+++ ")):
            continue
        kind = "hunk" if line.startswith("@@") else "add" if line.startswith("+") else \
            "del" if line.startswith("-") else ""
        out.append((kind, line))
    return out


def make_routes(server, cfg, db, store, memory, mailer, flow, inbox, provider, devices=None):
    secure = cfg.issuer.startswith("https://")
    factors = flow.factors
    origin = "{0.scheme}://{0.netloc}".format(urlparse(cfg.issuer))

    def route(path, methods=("GET",)):
        return server.custom_route(path, methods=list(methods), include_in_schema=False)

    # ---------- Sessions ----------
    def load_session(request):
        sid = request.cookies.get(SESSION_COOKIE)
        if not sid:
            return None
        row = db.one("SELECT * FROM web_sessions WHERE hash=?", digest(sid))
        now = time.time()
        if not row or row["expires_at"] < now or row["last_seen_at"] < now - SESSION_IDLE:
            if row:
                db.q("DELETE FROM web_sessions WHERE hash=?", row["hash"])
            return None
        account = db.account(row["account_id"])
        if not account or account["status"] != "active":
            return None
        if row["last_seen_at"] < now - 60:
            db.q("UPDATE web_sessions SET last_seen_at=? WHERE hash=?", now, row["hash"])
        return row, account

    def start_session(response, account_id):
        sid = new_secret(32)
        now = time.time()
        db.q("INSERT INTO web_sessions(hash, account_id, csrf, created_at, last_seen_at, expires_at, reauth_at) "
             "VALUES (?,?,?,?,?,?,?)", digest(sid), account_id, new_secret(24), now, now, now + SESSION_MAX, now)
        response.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_MAX, httponly=True, secure=secure, samesite="lax",
                            path="/")
        response.delete_cookie(LOGIN_COOKIE, path="/app")

    def same_origin(request):
        return request_from_site(request, origin)

    class Ctx:
        def __init__(self, request, session, account, form):
            self.request, self.session, self.form = request, session, form
            self.me = Principal(db, account)
            self.ip = client_ip(request)

        def render(self, template, status=200, **kw):
            nav = self.request.url.path.split("/")[2] if self.request.url.path.count("/") >= 2 else ""
            msg = MESSAGES.get(self.request.query_params.get("msg", ""), "")
            return render(template, status=status, me=self.me, csrf=self.session["csrf"], nav=nav, flash=msg, **kw)

        def audit(self, action, target="", detail=""):
            db.audit(action, self.me.id, None, target=target, detail=detail, ip=self.ip)

        def recently_confirmed(self):
            return self.session["reauth_at"] > time.time() - REAUTH_WINDOW

    def page(handler):
        async def wrapped(request: Request):
            s = load_session(request)
            if not s:
                if request.method == "GET":
                    return RedirectResponse("/app/login?next=" + quote(request.url.path + (
                        "?" + request.url.query if request.url.query else "")), status_code=303)
                return render("app_message.html", status=401, title="Signed out",
                              message="Your session ended. Sign in again.", me=None)
            session, account = s
            # instance policy: without a strong factor, only the pages that add one (and sign-out) are open
            if flow.needs_enrolment(account["id"]) and not request.url.path.startswith(ENROL_OPEN):
                if request.method == "GET":
                    return RedirectResponse("/app/security?msg=enrol", status_code=303)
                return render("app_message.html", status=403, title="Add a passkey first", back="/app/security",
                              message=MESSAGES["enrol"], me=None)
            form = None
            if request.method == "POST":
                form = await request.form()
                site_ok = same_origin(request)
                token_ok = hmac.compare_digest(form.get("csrf", ""), session["csrf"])
                if not site_ok or not token_ok:
                    # log only request-header facts, never the token, to help answer "why was this rejected"
                    db.audit("web.request_refused", account["id"], target=request.url.path,
                             detail={"origin": request.headers.get("origin", ""),
                                     "fetch_site": request.headers.get("sec-fetch-site", ""),
                                     "referer": bool(request.headers.get("referer")), "csrf_ok": token_ok},
                             ip=client_ip(request))
                    return Ctx(request, session, account, None).render(
                        "app_message.html", status=403, title="Request refused", back=request.url.path.rsplit("/", 1)[0] or "/app",
                        message="This form expired or came from another site. Reload the page and try again.")
            memory.records()
            return await handler(Ctx(request, session, account, form))
        return wrapped

    def back(path, msg=None):
        return RedirectResponse(path + ("?msg=" + msg if msg else ""), status_code=303)

    def need_reauth(ctx, where):
        return RedirectResponse("/app/reauth?next=" + quote(safe_next(where)), status_code=303)

    def not_found(ctx):
        return ctx.render("app_message.html", status=404, title="Not found",
                          message="That page does not exist or you cannot see it.")

    def confirm(ctx, title, message, cancel, danger=True, button="Confirm"):
        """Second confirmation for dangerous actions: resubmit the original form fields with confirm=1."""
        return ctx.render("app_confirm.html", title=title, message=message, action=ctx.request.url.path,
                          fields=[(k, v) for k, v in ctx.form.items() if k not in ("csrf", "confirm")],
                          danger=danger, button=button, cancel=cancel)

    # ---------- Static files and sign-in ----------
    @route("/app/static/app.css")
    async def css(request: Request):
        return stylesheet()

    @route("/app/static/khala-icon.png")
    async def icon(request: Request):
        return brand_icon()

    @route("/")
    async def root(request: Request):
        return RedirectResponse("/app", status_code=303)

    @route("/app/static/webauthn.js")
    async def js(request: Request):
        return script()

    @route("/app/static/callback.js")
    async def callback_js(request: Request):
        return script(CALLBACK_JS)

    def login_page(step, nonce, nxt, email="", error="", account=None):
        return render("login_steps.html", csp="app_js", step=step, action="/app/login",
                      fields=[("nonce", nonce), ("next", nxt)], email=email, error=error,
                      passkey_options="/app/login/passkey/options", passkey_verify="/app/login/passkey/verify",
                      has_passkey=bool(account and factors.passkeys(account["id"])),
                      has_totp=bool(account and factors.has_totp(account["id"])))

    def finish_login(response, account, method, ip):
        db.audit("login.ok", account["id"], target="web", detail=method, ip=ip)
        start_session(response, account["id"])
        return response

    @route("/app/login", ("GET", "POST"))
    async def login(request: Request):
        nxt = safe_next(request.query_params.get("next"))
        if request.method == "GET":
            if load_session(request):
                return RedirectResponse(nxt, status_code=303)
            nonce = new_secret(18)
            resp = login_page("email", nonce, nxt, email=request.query_params.get("email", ""))
            resp.set_cookie(LOGIN_COOKIE, nonce, max_age=1800, httponly=True, secure=secure, samesite="strict",
                            path="/app")
            return resp
        form = await request.form()
        nonce, nxt = form.get("nonce", ""), safe_next(form.get("next"))
        email = (form.get("email") or "").strip().lower()
        if not nonce or not hmac.compare_digest(nonce, request.cookies.get(LOGIN_COOKIE, "")):
            return RedirectResponse("/app/login?next=" + quote(nxt), status_code=303)
        key, ip, action = "web:" + nonce, client_ip(request), form.get("action")
        if action == "send":
            err = await flow.send(key, email, ip, invites=True)
            return login_page("email", nonce, nxt, email, err) if err else login_page("code", nonce, nxt, email)
        if action in ("totp", "recovery"):
            account, result = flow.second(key, action, form.get("code"), ip, "web")
            if not account:
                half = flow.half(key)
                return login_page("second", nonce, nxt, email, result, half) if half else \
                    login_page("email", nonce, nxt, "", result)
            return finish_login(RedirectResponse(nxt, status_code=303), account, result, ip)
        result, account = flow.code(key, email, form.get("code"), ip, "web")
        if result == "ok" and not account and not db.account_by_email(email) and db.open_invite_for(email):
            account = db.account(accept_invites_for_new(email, ip))
            nxt = "/app?msg=welcome"
        if result == "wrong":
            return login_page("code", nonce, nxt, email, "Wrong code.")
        if result not in ("ok", "second") or not account:
            return login_page("email", nonce, nxt, email, "That code expired or was used up. Request a new one.")
        if result == "second":
            return login_page("second", nonce, nxt, email, account=account)
        return finish_login(RedirectResponse(nxt, status_code=303), account, "email", ip)

    def login_bound(request, body):
        nonce = body.get("nonce", "")
        return bool(nonce) and hmac.compare_digest(nonce, request.cookies.get(LOGIN_COOKIE, "")) and same_origin(request)

    @route("/app/login/passkey/options", ("POST",))
    async def login_passkey_options(request: Request):
        body = await read_json(request)
        if body is None or not login_bound(request, body):
            return json_error("This page expired. Reload and try again.", 403)
        try:
            return JSONResponse(flow.passkey_options("web:" + body["nonce"], body.get("mode")),
                                headers={"Cache-Control": "no-store"})
        except AuthError as exc:
            return json_error(str(exc))

    @route("/app/login/passkey/verify", ("POST",))
    async def login_passkey_verify(request: Request):
        body = await read_json(request)
        if body is None or not login_bound(request, body):
            return json_error("This page expired. Reload and try again.", 403)
        ip = client_ip(request)
        try:
            account, method = flow.passkey_verify("web:" + body["nonce"], body.get("credential") or {}, ip, "web")
        except AuthError as exc:
            return json_error(str(exc))
        nxt = safe_next(body.get("next"))
        return finish_login(JSONResponse({"redirect": nxt}), account, method, ip)

    def accept_invites_for_new(email, ip):
        """First sign-in from an invited email: create the account and accept every invitation sent to that address.
        Receiving the verification code proves the address belongs to them."""
        account_id = db.create_account(email, email.split("@", 1)[0])
        db.audit("account.created", account_id, target=email, detail="accepted invitation", ip=ip)
        for inv in db.q("SELECT * FROM invites WHERE email=? AND accepted_at IS NULL AND expires_at>?",
                        email, time.time()):
            if not db.changed("UPDATE invites SET accepted_at=? WHERE token_hash=? AND accepted_at IS NULL",
                              time.time(), inv["token_hash"]):
                continue                                    # accepted by a concurrent request
            s = db.scope(inv["scope_id"]) if inv["scope_id"] else None
            if s and not s["archived_at"] and s["shareable"] and not s["auto_load"] and inv["role"] in ROLES:
                db.set_grant(s["id"], account_id, inv["role"], inv["created_by"])
                db.audit("grant.set", inv["created_by"], target=s["id"],
                         detail={"account": email, "role": inv["role"], "via": "invitation"}, ip=ip)
        return account_id

    @route("/app/logout", ("POST",))
    @page
    async def logout(ctx):
        db.q("DELETE FROM web_sessions WHERE hash=?", ctx.session["hash"])
        resp = RedirectResponse("/app/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @route("/app/reauth", ("GET", "POST"))
    @page
    async def reauth(ctx):
        """Re-verify before sensitive actions: once a strong factor is registered, only a passkey / TOTP / recovery
        code counts; otherwise the email code."""
        nxt = safe_next((ctx.form or {}).get("next") or ctx.request.query_params.get("next"))
        me = ctx.me.id
        has_passkey, has_totp = bool(factors.passkeys(me)), factors.has_totp(me)
        strong = has_passkey or has_totp

        def page(step="start", error=""):
            return ctx.render("web_reauth.html", csp="app_js", step=step, next=nxt, error=error, strong=strong,
                              has_passkey=has_passkey, has_totp=has_totp)

        def confirmed(method):
            db.q("UPDATE web_sessions SET reauth_at=? WHERE hash=?", time.time(), ctx.session["hash"])
            ctx.audit("session.reauth", detail=method)
            return RedirectResponse(nxt + ("&" if "?" in nxt else "?") + "msg=reauthed", status_code=303)

        if ctx.form is None:
            return page()
        action = ctx.form.get("action")
        if strong:
            ok = (factors.check_totp(me, ctx.form.get("code")) if action == "totp" else
                  factors.use_recovery(me, ctx.form.get("code")) if action == "recovery" else False)
            return confirmed(action) if ok else page(error="That code did not work.")
        key = "reauth:" + ctx.session["hash"]
        if action == "send":
            err = await send_code(db, mailer, key, ctx.me.email, ctx.ip, True)
            return page("start" if err else "code", err or "")
        ok, why = check_code(db, key, ctx.me.email, ctx.form.get("code"))
        if not ok:
            return page("code" if why == "wrong" else "start",
                        "Wrong code." if why == "wrong" else "That code expired. Request a new one.")
        return confirmed("email")

    def json_page(handler):
        """JSON endpoint for passkeys: requires a web session, same origin, and the CSRF token in the request body."""
        async def wrapped(request: Request):
            s = load_session(request)
            body = await read_json(request)
            if not s or body is None or not same_origin(request) or \
                    not hmac.compare_digest(str(body.get("csrf", "")), s[0]["csrf"]):
                return json_error("This page expired. Reload and try again.", 403)
            return await handler(Ctx(request, s[0], s[1], body), body)
        return wrapped

    @route("/app/reauth/passkey/options", ("POST",))
    @json_page
    async def reauth_passkey_options(ctx, body):
        if not factors.passkeys(ctx.me.id):
            return json_error("No passkey is registered.")
        return JSONResponse(factors.login_options("reauth:" + ctx.session["hash"], ctx.me.id),
                            headers={"Cache-Control": "no-store"})

    @route("/app/reauth/passkey/verify", ("POST",))
    @json_page
    async def reauth_passkey_verify(ctx, body):
        try:
            account_id, key = factors.authenticate("reauth:" + ctx.session["hash"], body.get("credential") or {})
        except AuthError as exc:
            return json_error(str(exc))
        if account_id != ctx.me.id:
            return json_error("That passkey belongs to another account.")
        db.q("UPDATE web_sessions SET reauth_at=? WHERE hash=?", time.time(), ctx.session["hash"])
        ctx.audit("session.reauth", detail="passkey:" + key["name"])
        nxt = safe_next(body.get("next"))
        return JSONResponse({"redirect": nxt + ("&" if "?" in nxt else "?") + "msg=reauthed"})

    # ---------- Security settings ----------
    def security_page(ctx, error="", totp_secret=None, codes=None):
        me = ctx.me.id
        logins = db.q("SELECT at, detail, ip_prefix FROM audit_events WHERE account_id=? AND action='login.ok' "
                      "ORDER BY at DESC LIMIT 10", me)
        totp_qr = None
        if totp_secret:
            totp_qr = qr_svg(otpauth_uri(totp_secret, ctx.me.email, "%s (%s)" % (cfg.instance_name, urlparse(cfg.issuer).hostname)))
        ever = db.one("SELECT COUNT(*) n FROM recovery_codes WHERE account_id=?", me)["n"]
        return ctx.render("app_security.html", csp="app_js", passkeys=factors.passkeys(me),
                          has_totp=factors.has_totp(me), strong=factors.has_strong(me),
                          recovery_left=factors.recovery_left(me), recovery_ever=bool(ever),
                          confirmed=ctx.recently_confirmed(), logins=logins, error=error,
                          totp_secret=totp_secret, totp_qr=totp_qr, codes=codes)

    def gate(ctx):
        """Changing security settings needs identity confirmed in the last 10 minutes; a fresh sign-in counts."""
        return None if ctx.recently_confirmed() else need_reauth(ctx, "/app/security")

    @route("/app/security")
    @page
    async def security(ctx):
        return security_page(ctx)

    @route("/app/security/passkey/options", ("POST",))
    @json_page
    async def passkey_register_options(ctx, body):
        if not ctx.recently_confirmed():
            return json_error("Confirm it’s you first, then add the passkey.", 403)
        return JSONResponse(factors.registration_options("register:" + ctx.session["hash"], ctx.me.account),
                            headers={"Cache-Control": "no-store"})

    @route("/app/security/passkey/verify", ("POST",))
    @json_page
    async def passkey_register_verify(ctx, body):
        if not ctx.recently_confirmed():
            return json_error("Confirm it’s you first, then add the passkey.", 403)
        try:
            pid = factors.register("register:" + ctx.session["hash"], ctx.me.account, body.get("credential") or {},
                                   body.get("name") or "")
        except AuthError as exc:
            return json_error(str(exc))
        ctx.audit("auth.passkey_added", "passkey:%d" % pid, {"name": (body.get("name") or "")[:60]})
        return JSONResponse({"redirect": "/app/security?msg=passkey-added"})

    @route("/app/security/passkeys/{passkey}", ("POST",))
    @page
    async def passkey_edit(ctx):
        row = db.one("SELECT * FROM passkeys WHERE id=? AND account_id=?", ctx.request.path_params["passkey"], ctx.me.id)
        if not row:
            return not_found(ctx)
        if ctx.form.get("action") == "remove":
            denied = gate(ctx)
            if denied:
                return denied
            if not ctx.form.get("confirm"):
                last = factors.has_strong(ctx.me.id) and len(factors.passkeys(ctx.me.id)) == 1 and \
                    not factors.has_totp(ctx.me.id)
                return confirm(ctx, "Remove passkey", "Remove “%s”?%s" % (row["name"], " It is your last second factor: "
                               "after this, an email code alone signs you in again." if last else ""), "/app/security",
                               button="Remove")
            db.q("DELETE FROM passkeys WHERE id=?", row["id"])
            ctx.audit("auth.passkey_removed", "passkey:%d" % row["id"], {"name": row["name"]})
            return back("/app/security", "passkey-removed")
        name = (ctx.form.get("name") or "").strip()[:60]
        if name:
            db.q("UPDATE passkeys SET name=? WHERE id=?", name, row["id"])
        return back("/app/security", "passkey-renamed")

    @route("/app/security/totp", ("POST",))
    @page
    async def totp(ctx):
        denied = gate(ctx)
        if denied:
            return denied
        action, me = ctx.form.get("action"), ctx.me.id
        if action == "start":
            if factors.has_totp(me):
                return back("/app/security")
            return security_page(ctx, totp_secret=factors.start_totp(me))
        if action == "confirm":
            if factors.confirm_totp(me, ctx.form.get("code")):
                ctx.audit("auth.totp_enabled")
                return back("/app/security", "totp-enabled")
            secret = factors.pending_totp_secret(me)
            return security_page(ctx, "That code did not match. Check the time on your phone and try again.",
                                 totp_secret=secret)
        if action == "remove":
            if not ctx.form.get("confirm"):
                return confirm(ctx, "Turn off authenticator app", "Stop accepting codes from your authenticator app?",
                               "/app/security", button="Turn off")
            factors.remove_totp(me)
            ctx.audit("auth.totp_removed")
            return back("/app/security", "totp-removed")
        return back("/app/security")

    @route("/app/security/recovery", ("POST",))
    @page
    async def recovery(ctx):
        denied = gate(ctx)
        if denied:
            return denied
        if not factors.has_strong(ctx.me.id):
            return back("/app/security")
        codes = factors.new_recovery(ctx.me.id)
        ctx.audit("auth.recovery_generated")
        return security_page(ctx, codes=codes)

    @route("/app/invite/{token}")
    async def invite_landing(request: Request):
        inv = db.invite(request.path_params["token"])
        s = load_session(request)
        if s and not inv:
            return RedirectResponse("/app", status_code=303)
        scope = db.scope(inv["scope_id"]) if inv and inv["scope_id"] else None
        inviter = db.account(inv["created_by"]) if inv else None
        return render("web_invite.html", csp="app", invite=inv, scope=scope, inviter=inviter, me=None,
                      login="/app/login?email=" + quote(inv["email"]) if inv else "/app/login")

    # ---------- Overview ----------
    @route("/app")
    @page
    async def overview(ctx):
        me = ctx.me
        recs = memory.records()
        counts = {}
        for r in recs.values():
            counts[r.meta["scope"]] = counts.get(r.meta["scope"], 0) + 1
        scopes = [(s, role, counts.get(s["id"], 0)) for s, role in me.visible_scopes()]
        review = [r for r in recs.values() if r.meta["status"] == "proposed" and me.at_least(r.meta["scope"], "maintainer")
                  and me.can_read(r.meta["scope"])]
        due = [r for r in recs.values() if r.meta["status"] == "active" and me.can_read(r.meta["scope"])
               and rules.review_due(r.meta)]
        recent = []
        for c in store.recent(time.time() - 7 * 86400, limit=60):
            files = [f for f in c["files"] if memory.version_visible(me, c, f)]
            if files:
                recent.append(dict(c, files=files))
        agents = db.agents_for(me.id)
        # a refresh token used again after it was replaced: someone may hold a copy (oauth.Provider._reused)
        reused = db.q("SELECT COALESCE(g.name, e.target) name, COUNT(*) n, MAX(e.at) last FROM audit_events e "
                      "LEFT JOIN agents g ON g.id=e.agent_id WHERE e.action='token.reuse_detected' AND e.at>? "
                      "AND e.account_id=? GROUP BY e.agent_id, e.target", time.time() - 7 * 86400, me.id)
        limited = db.q("SELECT g.name, COUNT(*) n, MAX(e.at) last FROM audit_events e JOIN agents g ON g.id=e.agent_id "
                       "WHERE e.action='agent.rate_limited' AND e.at>? AND g.account_id=? GROUP BY g.id",
                       time.time() - 86400, me.id)
        unnamed = [a for a in agents if a["revoked_at"] is None and a["name"] in ("Unnamed agent", "")]
        return ctx.render("app_overview.html", scopes=scopes, review=sorted(review, key=lambda r: r.name)[:20],
                          review_total=len(review), due=sorted(due, key=lambda r: r.meta["verified"])[:20],
                          due_total=len(due), recent=recent[:25], unnamed=unnamed,
                          health=health() if me.is_admin else None, strong=factors.has_strong(me.id),
                          queue=review_counts(me), limited=limited, reused=reused)

    def health():
        head = store.head()
        last = (cfg.state_dir / "backup-last")
        backed = last.read_text().strip() if last.exists() else ""
        state = cfg.state_dir / "state-backup-last"
        try:
            state_time = float(state.read_text().split()[0]) if state.exists() else None
        except (ValueError, IndexError):
            state_time = None
        return {"head": head[:10], "head_time": store.head_time(), "backup_configured": last.exists(),
                "backup_current": backed == head, "backup_time": last.stat().st_mtime if last.exists() else None,
                "state_time": state_time, "state_fresh": bool(state_time and state_time > time.time() - 36 * 3600)}

    # ---------- Scopes ----------
    @route("/app/scopes", ("GET", "POST"))
    @page
    async def scopes(ctx):
        error = ""
        if ctx.form is not None:
            sid = (ctx.form.get("id") or "").strip().lower()
            title = (ctx.form.get("title") or "").strip()[:120]
            if not SCOPE_ID.fullmatch(sid):
                error = "Scope IDs use lowercase letters, digits, - and _ (up to 48 characters)."
            elif db.scope(sid):
                error = "That scope ID is taken."
            else:
                db.create_scope(sid, ctx.me.id, title)
                ctx.audit("scope.created", sid)
                return back("/app/scopes/" + sid, "scope-created")
        recs = memory.records()
        counts = {}
        for r in recs.values():
            counts[r.meta["scope"]] = counts.get(r.meta["scope"], 0) + 1
        owners = {a["id"]: a for a in db.accounts()}
        rows = [(s, role, counts.get(s["id"], 0), owners.get(s["owner_id"])) for s, role in ctx.me.visible_scopes()]
        return ctx.render("app_scopes.html", rows=rows, error=error, form=ctx.form or {})

    def scope_or_404(ctx):
        sid = ctx.request.path_params["scope"]
        s = db.scope(sid)
        if not s or ctx.me.role(sid) is None:
            return None, None
        return s, ctx.me.role(sid)

    @route("/app/scopes/{scope}")
    @page
    async def scope_detail(ctx):
        s, role = scope_or_404(ctx)
        if not s:
            return not_found(ctx)
        recs = [r for r in memory.records().values() if r.meta["scope"] == s["id"]]
        manage = RANK[role] >= RANK["maintainer"]
        return ctx.render("app_scope.html", s=s, role=role, records=len(recs), owner=db.account(s["owner_id"]),
                          members=db.members(s["id"]) if manage else [],
                          invites=db.pending_invites(s["id"]) if manage else [], manage=manage,
                          review_modes=REVIEW_MODES, roles=ROLES)

    @route("/app/scopes/{scope}/settings", ("POST",))
    @page
    async def scope_settings(ctx):
        s, role = scope_or_404(ctx)
        if not s or RANK[role] < RANK["maintainer"]:
            return not_found(ctx)
        f = ctx.form
        fields = {"title": (f.get("title") or "").strip()[:120], "description": (f.get("description") or "").strip()[:2000],
                  "paths": "\n".join(p.strip() for p in (f.get("paths") or "").splitlines() if p.strip())[:2000]}
        if role == "owner" and f.get("review_mode") in REVIEW_MODES:
            fields["review_mode"] = f.get("review_mode")
        if role == "owner" and f.get("consolidation") in ("review", "auto"):
            fields["consolidation"] = f.get("consolidation")
        db.update_scope(s["id"], **fields)
        ctx.audit("scope.updated", s["id"], {k: v for k, v in fields.items() if s[k] != v})
        return back("/app/scopes/" + s["id"], "scope-saved")

    def sensitive_count(scope_id):
        return sum(1 for r in memory.records().values() if r.meta["scope"] == scope_id and r.meta.get("sensitivity"))

    @route("/app/scopes/{scope}/members", ("POST",))
    @page
    async def add_member(ctx):
        s, role = scope_or_404(ctx)
        here = "/app/scopes/%s" % (s["id"] if s else "")
        if not s or RANK[role] < RANK["maintainer"]:
            return not_found(ctx)
        f = ctx.form
        email, new_role = (f.get("email") or "").strip().lower(), f.get("role")
        if s["auto_load"] or not s["shareable"] or s["archived_at"]:
            return ctx.render("app_message.html", status=400, title="Cannot share",
                              message="Auto-loaded and archived scopes cannot be shared.", back=here)
        if new_role not in ROLES or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return ctx.render("app_message.html", status=400, title="Check the form",
                              message="Enter an email address and pick a role.", back=here)
        if new_role == "maintainer" and role != "owner":
            return ctx.render("app_message.html", status=403, title="Owner only",
                              message="Only the scope owner can appoint maintainers.", back=here)
        if new_role == "maintainer" and not ctx.recently_confirmed():
            return need_reauth(ctx, here)
        if not f.get("confirm"):
            n = sensitive_count(s["id"])
            msg = "Give %s %s access to “%s”? They will be able to read every record in it, and anything they read " \
                  "stays with them even if you remove access later." % (email, new_role, s["id"])
            if n:
                msg += " %d record%s in this scope %s marked sensitive." % (n, "s" if n != 1 else "",
                                                                            "are" if n != 1 else "is")
            return confirm(ctx, "Share this scope", msg, here, danger=bool(n), button="Share")
        target = db.account_by_email(email)
        if target:
            if target["id"] == s["owner_id"]:
                return back(here)
            db.set_grant(s["id"], target["id"], new_role, ctx.me.id)
            ctx.audit("grant.set", s["id"], {"account": target["email"], "role": new_role})
            await notify(target["email"], "You now have access to “%s”" % s["id"],
                   "%s gave you %s access to the memory scope “%s”.\n\nSign in: %s/app/scopes/%s\n"
                   % (ctx.me.name, new_role, s["id"], cfg.issuer, s["id"]))
            return back(here, "member-added")
        token = db.create_invite(email, s["id"], new_role, ctx.me.id)
        ctx.audit("invite.sent", s["id"], {"email": email, "role": new_role})
        await notify(email, "%s invited you to shared memory" % ctx.me.name,
               "%s invited you to the memory scope “%s” as %s.\n\nAccept: %s/app/invite/%s\n\n"
               "The link expires in 7 days. If you were not expecting this, ignore this email.\n"
               % (ctx.me.name, s["id"], new_role, cfg.issuer, token))
        return back(here, "invite-sent")

    async def notify(to, subject, body):
        """Send email on a thread: SMTP may take hundreds of milliseconds and must not hold the event loop,
        making other requests wait."""
        try:
            await asyncio.to_thread(mailer.send_text, to, subject, body)
        except Exception:
            db.audit("mail.failed", target=to, detail=subject)

    @route("/app/scopes/{scope}/members/{account}", ("POST",))
    @page
    async def change_member(ctx):
        s, role = scope_or_404(ctx)
        if not s or RANK[role] < RANK["maintainer"]:
            return not_found(ctx)
        here = "/app/scopes/" + s["id"]
        try:
            target_id = int(ctx.request.path_params["account"])
        except ValueError:
            return not_found(ctx)
        current = db.grants_for_account(target_id).get(s["id"])
        if current is None:
            return back(here)
        action, new_role = ctx.form.get("action"), ctx.form.get("role")
        touches_maintainer = current == "maintainer" or new_role == "maintainer"
        if touches_maintainer and role != "owner":
            return ctx.render("app_message.html", status=403, title="Owner only",
                              message="Only the scope owner can appoint or remove maintainers.", back=here)
        if touches_maintainer and not ctx.recently_confirmed():
            return need_reauth(ctx, here)
        target = db.account(target_id)
        if action == "remove":
            if not ctx.form.get("confirm"):
                return confirm(ctx, "Remove access", "Remove %s from “%s”? Their agents lose access on their next "
                               "request. Anything they already read stays with them." % (target["email"], s["id"]),
                               here, button="Remove access")
            db.remove_grant(s["id"], target_id)
            ctx.audit("grant.removed", s["id"], {"account": target["email"]})
            return back(here, "member-removed")
        if new_role in ROLES:
            db.set_grant(s["id"], target_id, new_role, ctx.me.id)
            ctx.audit("grant.set", s["id"], {"account": target["email"], "role": new_role})
        return back(here, "member-updated")

    @route("/app/scopes/{scope}/invites/{hash}/cancel", ("POST",))
    @page
    async def cancel_invite(ctx):
        s, role = scope_or_404(ctx)
        if not s or RANK[role] < RANK["maintainer"]:
            return not_found(ctx)
        h = ctx.request.path_params["hash"]
        inv = db.one("SELECT * FROM invites WHERE token_hash=? AND scope_id=?", h, s["id"])
        if inv:
            db.cancel_invite(h)
            ctx.audit("invite.cancelled", s["id"], {"email": inv["email"]})
        return back("/app/scopes/" + s["id"], "invite-cancelled")

    @route("/app/scopes/{scope}/auto-load", ("POST",))
    @page
    async def scope_auto_load(ctx):
        s, role = scope_or_404(ctx)
        if not s or role != "owner":
            return not_found(ctx)
        here = "/app/scopes/" + s["id"]
        if not ctx.recently_confirmed():
            return need_reauth(ctx, here)
        on = not s["auto_load"]
        if on and (s["archived_at"] or db.members(s["id"]) or db.pending_invites(s["id"])):
            return ctx.render("app_message.html", status=400, title="Cannot auto-load this scope",
                              message="An auto-loaded scope is never shared. Restore it if it is archived, remove "
                              "its members and cancel open invitations, then try again.", back=here)
        if not ctx.form.get("confirm"):
            if on:
                return confirm(ctx, "Load in every session", "Agents will load “%s” at the start of every session. "
                               "It can no longer be shared, and records agents add to it start as proposed until "
                               "you approve them." % s["id"], here, danger=False, button="Turn on")
            return confirm(ctx, "Stop auto-loading", "Agents will stop loading “%s” in every session, and it can be "
                           "shared again." % s["id"], here, danger=False, button="Turn off")
        if not db.set_auto_load(s["id"], on):
            return ctx.render("app_message.html", status=409, title="Not changed",
                              message="The scope changed in the meantime. Look at it again.", back=here)
        ctx.audit("scope.auto_load", s["id"], {"on": on})
        return back(here, "auto-load-on" if on else "auto-load-off")

    @route("/app/scopes/{scope}/archive", ("POST",))
    @page
    async def archive_scope(ctx):
        s, role = scope_or_404(ctx)
        if not s or role != "owner":
            return not_found(ctx)
        here = "/app/scopes/" + s["id"]
        if not ctx.recently_confirmed():
            return need_reauth(ctx, here)
        if s["archived_at"]:
            db.update_scope(s["id"], archived_at=None)
            ctx.audit("scope.unarchived", s["id"])
            return back(here, "unarchived")
        if s["auto_load"]:
            return ctx.render("app_message.html", status=400, title="Cannot archive",
                              message="The auto-loaded scope cannot be archived.", back=here)
        if not ctx.form.get("confirm"):
            return confirm(ctx, "Archive scope", "Archive “%s”? Its records stay in the repository, but no agent and "
                           "no member can see them until you restore it." % s["id"], here, button="Archive")
        db.update_scope(s["id"], archived_at=time.time())
        ctx.audit("scope.archived", s["id"])
        return back(here, "archived")

    @route("/app/scopes/{scope}/transfer", ("POST",))
    @page
    async def transfer_scope(ctx):
        s, role = scope_or_404(ctx)
        if not s or role != "owner":
            return not_found(ctx)
        here = "/app/scopes/" + s["id"]
        if not ctx.recently_confirmed():
            return need_reauth(ctx, here)
        target = db.active_account_by_email(ctx.form.get("email"))
        if s["auto_load"] or not target or target["id"] == ctx.me.id:
            return ctx.render("app_message.html", status=400, title="Cannot transfer",
                              message="Pick another active account. The auto-loaded scope cannot be transferred.",
                              back=here)
        if not ctx.form.get("confirm"):
            return confirm(ctx, "Transfer ownership", "Make %s the owner of “%s”? You stay on as a maintainer."
                           % (target["email"], s["id"]), here, button="Transfer")
        with db.tx():
            db.conn.execute("UPDATE scopes SET owner_id=? WHERE id=?", (target["id"], s["id"]))
            db.conn.execute("DELETE FROM grants WHERE scope_id=? AND account_id=?", (s["id"], target["id"]))
            db.conn.execute("INSERT OR REPLACE INTO grants(scope_id, account_id, role, granted_by, created_at) "
                            "VALUES (?,?,?,?,?)", (s["id"], ctx.me.id, "maintainer", target["id"], time.time()))
        ctx.audit("scope.transferred", s["id"], {"to": target["email"]})
        return back(here, "transferred")

    @route("/app/scopes/{scope}/leave", ("POST",))
    @page
    async def leave_scope(ctx):
        s, role = scope_or_404(ctx)
        if not s or role == "owner":
            return not_found(ctx)
        db.remove_grant(s["id"], ctx.me.id)
        ctx.audit("grant.left", s["id"])
        return back("/app/scopes", "left")

    # ---------- Records (read-only) ----------
    @route("/app/records")
    @page
    async def records(ctx):
        qp = ctx.request.query_params
        scope, rtype, status, q, due = (qp.get("scope", ""), qp.get("type", ""), qp.get("status", ""),
                                        qp.get("q", "").strip(), qp.get("due") == "1")
        recs = [r for r in memory.records().values() if ctx.me.can_read(r.meta["scope"])]
        types = sorted({r.meta["type"] for r in recs if r.meta["type"]})
        terms = [t for t in q.lower().split() if t]
        out = []
        for r in sorted(recs, key=lambda r: r.name):
            m = r.meta
            if (scope and m["scope"] != scope) or (rtype and m["type"] != rtype) or (status and m["status"] != status):
                continue
            if due and not (m["status"] == "active" and rules.review_due(m)):
                continue
            if terms and not all(t in r.text.lower() for t in terms):
                continue
            out.append(r)
        return ctx.render("app_records.html", rows=out[:500], total=len(out), scopes=ctx.me.readable_scopes(),
                          types=types, f={"scope": scope, "type": rtype, "status": status, "q": q, "due": due},
                          due_fn=rules.review_due)

    def visible_record(ctx):
        name = ctx.request.path_params["name"]
        r = memory.records().get(name)
        if not r or not ctx.me.can_read(r.meta["scope"]):
            return None
        return r

    @route("/app/records/{name}")
    @page
    async def record(ctx):
        r = visible_record(ctx)
        if not r:
            return not_found(ctx)
        if ctx.me.role(r.meta["scope"]) != "owner":
            ctx.audit("record.read", r.name, {"scope": r.meta["scope"], "via": "web"})
        m = rules.FRONT.match(r.text)
        front, body = (m.group(0), r.text[m.end():]) if m else ("", r.text)
        history = [c for c in store.history(r.name) if memory.version_visible(ctx.me, c, r.name)]
        return ctx.render("app_record.html", r=r, front=front, body=body, history=history,
                          due=rules.review_due(r.meta), editable=editable(ctx, r),
                          can_delete=editable(ctx, r) and ctx.me.at_least(r.meta["scope"], "maintainer"))

    @route("/app/records/{name}/commits/{sha}")
    @page
    async def record_diff(ctx):
        r = visible_record(ctx)
        sha = ctx.request.path_params["sha"]
        if not r:
            return not_found(ctx)
        commit = next((c for c in store.history(r.name, limit=500) if c["sha"] == sha), None)
        if not commit or not memory.version_visible(ctx.me, commit, r.name):
            return not_found(ctx)
        return ctx.render("app_diff.html", r=r, c=commit, lines=diff_lines(store.diff(sha, r.name)),
                          can_undo=editable(ctx, r))

    # ---------- Records: edit, mark outdated, delete, undo by commit ----------
    def editable(ctx, r):
        """Whether the web UI may edit: needs write access; pinned records are edited by hand only, in a
        clone of the repository (a mirror's sync may revert pinned changes made on the server)."""
        return ctx.me.can_write(r.meta["scope"]) and r.meta.get("pinned") != "true"

    def web_write(ctx, name, prepare, message, reason=""):
        return store.write(name, prepare, ctx.me.name, ctx.me.email, message, agent=("web", None),
                           trailers={"Reason": reason} if reason else None)

    @route("/app/records/{name}/edit", ("GET", "POST"))
    @page
    async def record_edit(ctx):
        r = visible_record(ctx)
        if not r or not editable(ctx, r):
            return not_found(ctx)
        if ctx.form is None:
            return ctx.render("app_edit.html", r=r, content=r.text, sha=r.sha, error="", current=None)
        content = (ctx.form.get("content") or "").replace("\r\n", "\n")
        if not content.endswith("\n"):
            content += "\n"
        sha, reason = ctx.form.get("sha", ""), (ctx.form.get("reason") or "").strip()[:300]
        target = rules.meta(content)["scope"]
        if target != r.meta["scope"] and db.members(target) and not ctx.form.get("confirm"):
            return confirm(ctx, "Move to a shared scope", "Move %s from “%s” to “%s”? Everyone who has access to "
                           "“%s” will be able to read it." % (r.name, r.meta["scope"], target, target),
                           "/app/records/%s/edit" % r.name, danger=True, button="Move")

        def prepare(existing):
            if existing is None or existing.sha != sha:
                raise Conflict("changed")
            if not ctx.me.can_write(existing.meta["scope"]):
                raise rules.RuleError("you can read this record but not change it")
            text, _ = rules.check_write(content, existing.text, ctx.me.can_write, ctx.me.is_auto_load)
            return text

        try:
            _, _, changed = web_write(ctx, r.name, prepare, "Edit memory: %s" % r.name[:-3], reason)
        except Conflict:
            now = memory.records().get(r.name)
            return ctx.render("app_edit.html", r=now or r, content=content, sha=now.sha if now else sha,
                              current=now.text if now else "", status=409,
                              error="Someone changed this record while you were editing. Your text is below; the "
                                    "current version is underneath it. Merge them and save again.")
        except rules.RuleError as exc:
            return ctx.render("app_edit.html", r=r, content=content, sha=sha, error=str(exc), current=None, status=400)
        if changed:
            ctx.audit("record.edited", r.name, {"scope": target, "reason": reason})
        return back("/app/records/" + r.name, "saved" if changed else None)

    @route("/app/records/{name}/deprecate", ("POST",))
    @page
    async def record_deprecate(ctx):
        r = visible_record(ctx)
        if not r or not editable(ctx, r):
            return not_found(ctx)
        reason = (ctx.form.get("reason") or "").strip()[:300] or "marked outdated in the web app"

        def prepare(existing):
            if existing is None or existing.sha != r.sha:
                raise Conflict("changed")
            text, _ = rules.check_write(rules.deprecate(existing.text, reason), existing.text, ctx.me.can_write,
                                        ctx.me.is_auto_load)
            return text

        try:
            web_write(ctx, r.name, prepare, "Deprecate memory: %s" % r.name[:-3], reason)
        except (Conflict, rules.RuleError) as exc:
            return ctx.render("app_message.html", status=409, title="Not changed",
                              message="The record changed or can't be marked outdated (%s)." % exc,
                              back="/app/records/" + r.name)
        ctx.audit("record.deprecated", r.name, {"scope": r.meta["scope"], "reason": reason})
        return back("/app/records/" + r.name, "deprecated")

    @route("/app/records/{name}/delete", ("POST",))
    @page
    async def record_delete(ctx):
        r = visible_record(ctx)
        if not r or not editable(ctx, r) or not ctx.me.at_least(r.meta["scope"], "maintainer"):
            return not_found(ctx)
        reason = (ctx.form.get("reason") or "").strip()[:300]
        if not ctx.form.get("confirm"):
            return confirm(ctx, "Delete record", "Delete %s for good? It stays in the history, but no agent will see it "
                           "again. Marking it outdated is usually enough." % r.name, "/app/records/" + r.name,
                           button="Delete")

        def prepare(existing):
            if existing is None or existing.sha != r.sha:
                raise Conflict("changed")
            return None

        try:
            web_write(ctx, r.name, prepare, "Delete memory: %s" % r.name[:-3], reason or "deleted in the web app")
        except Conflict:
            return ctx.render("app_message.html", status=409, title="Not deleted",
                              message="The record changed after you opened it. Look at it again first.",
                              back="/app/records/" + r.name)
        ctx.audit("record.deleted", r.name, {"scope": r.meta["scope"], "reason": reason})
        return back("/app/records?scope=" + r.meta["scope"], "deleted")

    @route("/app/records/{name}/commits/{sha}/undo", ("POST",))
    @page
    async def commit_undo(ctx):
        name, sha = ctx.request.path_params["name"], ctx.request.path_params["sha"]
        here = "/app/records/%s/commits/%s" % (name, sha)
        r = memory.records().get(name)
        before = store.blob_at(sha + "^", name)
        scope = r.meta["scope"] if r else rules.meta(store.blob_text(before) or "")["scope"] if before else ""
        if not scope or not ctx.me.can_read(scope) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return not_found(ctx)
        commit = next((c for c in store.history(name, limit=500) if c["sha"] == sha), None)
        if not commit or not memory.version_visible(ctx.me, commit, name):
            return not_found(ctx)
        item, why = undo.commit_item(store, memory, ctx.me, sha, name)
        if why:
            return ctx.render("app_message.html", status=409, title="Can't undo", message=why, back=here)
        if not ctx.form.get("confirm"):
            return confirm(ctx, "Undo this change", "Put %s back the way it was before this change?%s" % (
                name, "" if item["before"] else " The change created it, so it will be removed."), here, button="Undo")
        if not undo.apply(store, ctx.me, item, "Undo change to %s" % name[:-3], {"Reverts": sha}):
            return ctx.render("app_message.html", status=409, title="Can't undo",
                              message="The record changed while you were deciding.", back=here)
        ctx.audit("record.reverted", name, {"scope": scope, "commit": sha})
        return back("/app/records/" + name if item["before"] else "/app/records", "undone")

    # ---------- agent ----------
    @route("/app/agents")
    @page
    async def agents(ctx):
        clients = {}
        for row in db.q("SELECT client_id, info FROM clients"):
            clients[row["client_id"]] = json.loads(row["info"]).get("client_name") or ""
        rows = []
        for a in db.agents_for(ctx.me.id):
            ceiling = json.loads(a["ceiling"]) if a["ceiling"] else None
            client = clients.get(a["oauth_client_id"], "")
            if not client and a["oauth_client_id"]:           # signed in with a metadata document
                known, _ = catalog.identify(catalog.entries(), a["oauth_client_id"])
                client = known["name"] if known else ""
            rows.append({"a": a, "client": client, "ceiling": ceiling,
                         "summary": ceiling_summary(a["ceiling"])})
        scopes = [(s, role) for s, role in ctx.me.visible_scopes() if not s["archived_at"]]
        return ctx.render("app_agents.html", rows=rows, scopes=scopes, issuer=cfg.issuer)

    def own_agent(ctx):
        try:
            a = db.agent(int(ctx.request.path_params["agent"]))
        except ValueError:
            return None
        return a if a and a["account_id"] == ctx.me.id else None

    @route("/app/agents/{agent}", ("POST",))
    @page
    async def edit_agent(ctx):
        a = own_agent(ctx)
        if not a or a["revoked_at"]:
            return not_found(ctx)
        f = ctx.form
        name = (f.get("name") or "").strip()[:80] or a["name"]
        kind = f.get("kind") if f.get("kind") in ("device", "bot") else a["kind"]
        ceiling = None if f.get("access") == "all" else parse_ceiling(ctx, f)
        db.update_agent(a["id"], name=name, kind=kind, ceiling=ceiling)
        ctx.audit("agent.updated", "agent:%d" % a["id"], {"name": name, "kind": kind, "ceiling": ceiling})
        return back("/app/agents", "agent-saved")

    def parse_ceiling(ctx, f):
        ceiling = {}
        for s, role in ctx.me.visible_scopes():
            mode = f.get("scope:" + s["id"])
            if mode == "rw" and role != "viewer":
                ceiling[s["id"]] = "rw"
            elif mode in ("r", "rw"):
                ceiling[s["id"]] = "r"
        return ceiling

    @route("/app/bots", ("POST",))
    @page
    async def issue_bot(ctx):
        """Long-lived token for bots without OAuth support: needs a recent identity check and is shown only once."""
        if not ctx.recently_confirmed():
            return need_reauth(ctx, "/app/agents")
        f = ctx.form
        name = (f.get("name") or "").strip()[:80]
        ceiling = None if f.get("access") == "all" else parse_ceiling(ctx, f)
        try:
            days = int(f.get("days", "90"))
        except ValueError:
            days = 90
        if not name or ceiling == {} or days not in (7, 30, 90, 365):
            return ctx.render("app_message.html", status=400, title="Check the form",
                              message="Give the bot a name, pick at least one scope and an expiry.", back="/app/agents")
        token = issue_token(ctx, name, "bot", ceiling, days)
        return ctx.render("app_token.html", name=name, token=token, days=days, issuer=cfg.issuer)

    def issue_token(ctx, name, kind, ceiling, days, via=None):
        """A new agent with a long-lived bearer token, shown once. Callers check the recent identity first."""
        agent_id = db.create_agent(ctx.me.id, name, kind, None, ceiling)
        token = provider.issue_bearer(ctx.me.id, agent_id, days)
        detail = {"name": name, "days": days, "ceiling": ceiling}
        if via:
            detail["via"] = via
        ctx.audit("agent.token_issued", "agent:%d" % agent_id, detail)
        return token

    # ---------- Connecting clients: the catalog, rendered for this server ----------
    def server_name():
        return re.sub(r"[^a-z0-9]+", "-", cfg.instance_name.lower()).strip("-") or "khala"

    @route("/app/connect")
    @page
    async def connect(ctx):
        return ctx.render("app_connect.html", entries=catalog.entries(), url=cfg.issuer + "/mcp")

    @route("/app/connect/{client}", ("GET", "POST"))
    @page
    async def connect_client(ctx):
        entry = catalog.by_id(catalog.entries(), ctx.request.path_params["client"])
        if not entry:
            return not_found(ctx)
        here = "/app/connect/" + entry["id"]
        url, token, days = cfg.issuer + "/mcp", None, 90
        if ctx.form is not None:                # create a token for the channels that need one
            if not ctx.recently_confirmed():
                return need_reauth(ctx, here)
            try:
                days = int(ctx.form.get("days", "90"))
            except ValueError:
                days = 0
            if days not in DEVICE_DAYS:
                return ctx.render("app_message.html", status=400, title="Check the form", back=here,
                                  message="Pick when the token expires.")
            token = issue_token(ctx, "%s (token)" % entry["name"], "device", None, days, via="connect:" + entry["id"])
        channels = catalog.render(entry, url, server_name(), token)
        return ctx.render("app_connect_client.html", entry=entry, url=url, token=token, days=days,
                          channels=channels, choices=DEVICE_DAYS)

    @route("/app/device", ("GET", "POST"))
    @page
    async def device(ctx):
        """Approve a command-line tool (RFC 8628): enter the code it shows, name it, limit it, approve."""
        f = ctx.form or {}
        code = (f.get("code") if ctx.form else ctx.request.query_params.get("code")) or ""
        here = "/app/device?code=" + quote(code) if code else "/app/device"
        row = devices.pending(code) if code else None
        if code and not row:
            ctx.audit("device.code_unknown")
            wrong = db.one("SELECT COUNT(*) n FROM audit_events WHERE action='device.code_unknown' AND account_id=? "
                           "AND at>?", ctx.me.id, time.time() - 3600)["n"]
            error = ("Too many wrong codes; try again in an hour." if wrong > 10 else
                     "That code is unknown or expired. Check it, or start again on your computer.")
            return ctx.render("app_device.html", code="", row=None, error=error, scopes=ctx.me.visible_scopes(),
                              days=DEVICE_DAYS)
        action = f.get("action", "") if ctx.form else ""
        if not row or action not in ("approve", "deny"):
            return ctx.render("app_device.html", code=code, row=row, error="", scopes=ctx.me.visible_scopes(),
                              days=DEVICE_DAYS)
        if action == "deny":
            devices.decide(code, ctx.me.id, False)
            ctx.audit("device.denied", detail={"client": row["client_name"]})
            return back("/app/agents", "device-denied")
        if not ctx.recently_confirmed():
            return need_reauth(ctx, here)
        name = (f.get("name") or "").strip()[:80] or row["client_name"]
        ceiling = None if f.get("access") == "all" else parse_ceiling(ctx, f)
        try:
            days = int(f.get("days", "90"))
        except ValueError:
            days = 0
        if ceiling == {} or days not in DEVICE_DAYS:
            return ctx.render("app_device.html", code=code, row=row, scopes=ctx.me.visible_scopes(), days=DEVICE_DAYS,
                              error="Pick at least one scope, or allow all of them, and an expiry.")
        agent_id = devices.decide(code, ctx.me.id, True, name=name, ceiling=ceiling, days=days)
        if agent_id is None:
            return ctx.render("app_device.html", code="", row=None, scopes=ctx.me.visible_scopes(), days=DEVICE_DAYS,
                              error="That code expired or was already used. Start again on your computer.")
        ctx.audit("device.approved", "agent:%d" % agent_id, {"name": name, "ceiling": ceiling, "days": days})
        return back("/app/agents", "device-approved")

    @route("/app/agents/{agent}/revoke", ("POST",))
    @page
    async def revoke_agent(ctx):
        a = own_agent(ctx)
        if not a or a["revoked_at"]:
            return not_found(ctx)
        if not ctx.form.get("confirm"):
            return confirm(ctx, "Revoke agent", "Revoke “%s”? Its tokens stop working on the next request. To use it "
                           "again, connect the app from scratch." % a["name"], "/app/agents", button="Revoke")
        db.revoke_agent(a["id"])
        ctx.audit("agent.revoked", "agent:%d" % a["id"], {"name": a["name"]})
        return back("/app/agents", "agent-revoked")

    @route("/app/agents/{agent}/undo", ("GET", "POST"))
    @page
    async def agent_undo(ctx):
        """Undo all of an agent's changes within a time range: show a preview first, run it after confirmation."""
        a = own_agent(ctx)
        if not a:
            return not_found(ctx)
        window = (ctx.form or ctx.request.query_params).get("window", "1")
        window = window if window in undo.WINDOWS else "1"
        since = max(time.time() - undo.WINDOWS[window], a["created_at"] - 60)
        plan, notes = undo.agent_plan(db, store, memory, ctx.me, a["id"], since)
        open_props = [r for r in db.q("SELECT * FROM proposals WHERE state='open'")
                      if r["agent_id"] == a["id"] or notes.intersection(json.loads(r["note_ids"]))]
        new_notes = db.one("SELECT COUNT(*) n FROM notes WHERE agent_id=? AND state='new'", a["id"])["n"]
        if ctx.form is None or ctx.form.get("action") != "run":
            return ctx.render("app_agent_undo.html", a=a, plan=plan, window=window, open_props=len(open_props),
                              new_notes=new_notes, done=None)
        if not ctx.recently_confirmed():
            return need_reauth(ctx, "/app/agents/%d/undo?window=%s" % (a["id"], window))
        done = {"restored": [], "removed": [], "skipped": [i for i in plan.items if i["action"] == "skip"]}
        for item in plan.doable:
            ok = undo.apply(store, ctx.me, item, "Undo %s's change to %s" % (a["name"], item["name"][:-3]),
                            {"Reverts-Agent": a["id"]})
            if ok:
                done["restored" if item["action"] == "restore" else "removed"].append(item)
            else:
                done["skipped"].append(dict(item, why="changed while undoing"))
        rejected, dropped = undo.reject_open(db, inbox, ctx.me, a["id"], notes)
        if ctx.form.get("revoke") and a["revoked_at"] is None:
            db.revoke_agent(a["id"])
            ctx.audit("agent.revoked", "agent:%d" % a["id"], {"name": a["name"], "with": "undo"})
        ctx.audit("agent.undone", "agent:%d" % a["id"], {"window": window, "restored": len(done["restored"]),
                  "removed": len(done["removed"]), "skipped": len(done["skipped"]), "proposals": rejected,
                  "notes": dropped})
        return ctx.render("app_agent_undo.html", a=db.agent(a["id"]), plan=plan, window=window, done=done,
                          rejected=rejected, dropped=dropped, open_props=0, new_notes=0)

    # ---------- Audit ----------
    def audit_rows(ctx, limit):
        qp = ctx.request.query_params
        where, args = [], []
        if not ctx.me.is_admin or qp.get("all") != "1":
            owned = [s["id"] for s, role in ctx.me.visible_scopes() if role == "owner"]
            marks = ",".join("?" * len(owned)) or "NULL"
            where.append("(e.account_id=? OR e.target IN (%s) OR json_extract(CASE WHEN json_valid(e.detail) "
                         "THEN e.detail END, '$.scope') IN (%s))" % (marks, marks))
            args += [ctx.me.id] + owned + owned
        if qp.get("action"):
            where.append("e.action LIKE ?")
            args.append(qp.get("action") + "%")
        if qp.get("target"):
            where.append("(e.target=? OR json_extract(CASE WHEN json_valid(e.detail) THEN e.detail END, '$.scope')=?)")
            args += [qp.get("target"), qp.get("target")]
        if qp.get("agent", "").isdigit():
            where.append("e.agent_id=?")
            args.append(int(qp.get("agent")))
        try:
            days = max(1, min(400, int(qp.get("days", "30"))))
        except ValueError:
            days = 30
        where.append("e.at>?")
        args.append(time.time() - days * 86400)
        sql = ("SELECT e.*, a.email account_email, g.name agent_name FROM audit_events e "
               "LEFT JOIN accounts a ON a.id=e.account_id LEFT JOIN agents g ON g.id=e.agent_id WHERE %s "
               "ORDER BY e.at DESC LIMIT %d" % (" AND ".join(where), limit))
        return db.q(sql, *args), days

    @route("/app/audit")
    @page
    async def audit(ctx):
        rows, days = audit_rows(ctx, 500)
        qp = ctx.request.query_params
        return ctx.render("app_audit.html", rows=rows, days=days, f=dict(qp), all=qp.get("all") == "1",
                          query=str(ctx.request.url.query))

    @route("/app/audit.csv")
    @page
    async def audit_csv(ctx):
        rows, _ = audit_rows(ctx, 20000)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "account", "agent", "action", "target", "detail", "ip_prefix"])
        for r in rows:
            w.writerow([clock.local(r["at"]).isoformat(timespec="seconds"), r["account_email"] or "",
                        r["agent_name"] or "", r["action"], r["target"], r["detail"], r["ip_prefix"]])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=audit.csv", "Cache-Control": "no-store"})

    # ---------- Instance administration ----------
    def admin_only(ctx):
        return ctx.me.is_admin

    @route("/app/admin")
    @page
    async def admin(ctx):
        if not admin_only(ctx):
            return not_found(ctx)
        accounts = []
        for a in db.accounts():
            ag = db.agents_for(a["id"])
            live = [x for x in ag if x["revoked_at"] is None]
            accounts.append({"a": a, "emails": db.emails(a["id"]), "agents": len(live),
                             "last": max((x["last_used_at"] or 0 for x in live), default=0)})
        pushes = []
        log = store.repo / PUSH_LOG
        if log.exists():
            for line in log.read_text(errors="replace").splitlines()[-20:]:
                try:
                    pushes.append(json.loads(line))
                except ValueError:
                    continue
        return ctx.render("app_admin.html", accounts=accounts, invites=db.pending_invites(), pushes=pushes[::-1],
                          push_enforce=cfg.push_enforce,
                          health=health(), db_size=cfg.db.stat().st_size if cfg.db.exists() else 0,
                          smtp_host=cfg.smtp.get("host") or "(not set)", mailer_kind=cfg.mailer)

    @route("/app/admin/accounts/{account}", ("POST",))
    @page
    async def admin_account(ctx):
        if not admin_only(ctx):
            return not_found(ctx)
        try:
            target = db.account(int(ctx.request.path_params["account"]))
        except ValueError:
            target = None
        if not target:
            return not_found(ctx)
        action = ctx.form.get("action")
        if target["id"] == ctx.me.id and action in ("disable", "unadmin"):
            return ctx.render("app_message.html", status=400, title="Not allowed",
                              message="You cannot disable yourself or remove your own admin role.", back="/app/admin")
        if action in ("admin", "unadmin") and not ctx.recently_confirmed():
            return need_reauth(ctx, "/app/admin")
        if action == "disable" and not ctx.form.get("confirm"):
            return confirm(ctx, "Disable account", "Disable %s? They are signed out everywhere and all of their "
                           "agents stop working." % target["email"], "/app/admin", button="Disable")
        if action in ("disable", "enable"):
            db.set_account_status(target["id"], "disabled" if action == "disable" else "active")
        elif action in ("admin", "unadmin"):
            db.set_admin(target["id"], action == "admin")
        else:
            return back("/app/admin")
        ctx.audit("account." + action, target["email"])
        return back("/app/admin", "account-updated")

    @route("/app/admin/invite", ("POST",))
    @page
    async def admin_invite(ctx):
        if not admin_only(ctx):
            return not_found(ctx)
        email = (ctx.form.get("email") or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or db.account_by_email(email):
            return ctx.render("app_message.html", status=400, title="Cannot invite",
                              message="Enter an email address that does not have an account yet.", back="/app/admin")
        token = db.create_invite(email, None, None, ctx.me.id)
        ctx.audit("invite.sent", email, {"email": email})
        await notify(email, "%s invited you to shared memory" % ctx.me.name,
               "%s invited you to a shared memory server.\n\nAccept: %s/app/invite/%s\n\n"
               "The link expires in 7 days.\n" % (ctx.me.name, cfg.issuer, token))
        return back("/app/admin", "invite-sent")

    @route("/app/admin/mailtest", ("POST",))
    @page
    async def admin_mailtest(ctx):
        if not admin_only(ctx):
            return not_found(ctx)
        try:
            await asyncio.to_thread(mailer.send_text, ctx.me.email, "%s test email" % cfg.instance_name,
                             "This is a test email from %s.\n" % cfg.issuer)
            ok = True
        except Exception:
            ok = False
        ctx.audit("mail.test", ctx.me.email, "ok" if ok else "failed")
        return back("/app/admin", "mail-sent" if ok else "mail-failed")

    # ---------- Review: consolidation proposals, conflicts, inbox ----------
    def can_review(me, prop):
        scopes = {prop["scope"], prop["old_scope"]} - {None}
        return all(inbox.reviewable(me, sc) for sc in scopes)

    def review_counts(me):
        props = [r for r in db.q("SELECT * FROM proposals WHERE state='open'") if can_review(me, r)]
        conflicts = [c for c in db.q("SELECT * FROM conflicts WHERE state='open'") if inbox.reviewable(me, c["scope"])]
        notes = [n for n in db.q("SELECT * FROM notes WHERE state='new'") if inbox.can_see(me, n)]
        return {"proposals": len(props), "conflicts": len(conflicts), "notes": len(notes)}

    def names():
        out = {}
        for table, key in (("accounts", "name"), ("agents", "name")):
            out[table] = {r["id"]: r[key] for r in db.q("SELECT id, %s FROM %s" % (key, table))}
        return out

    @route("/app/review")
    @page
    async def review(ctx):
        me = ctx.me
        who = names()
        props = [r for r in db.q("SELECT * FROM proposals WHERE state='open' ORDER BY created_at") if can_review(me, r)]
        applied = [r for r in db.q("SELECT * FROM proposals WHERE state IN ('applied', 'approved') AND "
                                   "COALESCE(decided_at, created_at)>? ORDER BY COALESCE(decided_at, created_at) DESC",
                                   time.time() - 14 * 86400) if can_review(me, r)]
        conflicts = [c for c in db.q("SELECT * FROM conflicts WHERE state='open' ORDER BY created_at")
                     if inbox.reviewable(me, c["scope"])]
        notes = [n for n in db.q("SELECT * FROM notes WHERE state='new' ORDER BY created_at") if inbox.can_see(me, n)]
        by_scope = {}
        for n in notes:
            by_scope[n["scope"]] = by_scope.get(n["scope"], 0) + 1
        return ctx.render("app_review.html", props=props, applied=applied, conflicts=conflicts, notes=notes[:100],
                          notes_total=len(notes), by_scope=sorted(by_scope.items()), who=who, now=time.time(),
                          candidates=inbox.candidates(me))

    @route("/app/review/candidates", ("POST",))
    @page
    async def candidate(ctx):
        name, action = ctx.form.get("name", ""), ctx.form.get("action")
        if action not in ("approve", "reject"):
            return back("/app/review")
        try:
            inbox.settle_candidate(ctx.me, name, action == "approve")
        except InboxError as exc:
            return ctx.render("app_message.html", status=409, title="Not applied", message=str(exc), back="/app/review")
        return back("/app/review", "approved" if action == "approve" else "candidate-rejected")

    def note_rows(me, ids):
        """Source notes referenced by proposals and conflicts: a reference does not pass on read access, so each
        note is checked by its own rules. Returns (visible notes, number of hidden ones)."""
        out, hidden = [], 0
        for nid in ids:
            n = inbox.note(nid)
            if n and inbox.can_see(me, n):
                out.append({"n": n, "text": inbox.note_body(n)})
            elif n:
                hidden += 1
        return out, hidden

    @route("/app/review/proposals/{pid}", ("GET", "POST"))
    @page
    async def proposal(ctx):
        prop = inbox.proposal(ctx.request.path_params["pid"])
        if not prop or not can_review(ctx.me, prop):
            return not_found(ctx)
        if ctx.form is not None:
            action = ctx.form.get("action")
            try:
                if action == "approve" and prop["state"] == "open":
                    result = inbox.approve(ctx.me, prop)
                    return back("/app/review", result)
                if action == "reject" and prop["state"] == "open":
                    inbox.reject(ctx.me, prop, ctx.form.get("why", ""))
                    return back("/app/review", "rejected")
                if action == "undo" and prop["state"] in ("applied", "approved"):
                    if not ctx.form.get("confirm"):
                        return confirm(ctx, "Undo change", "Put %s back the way it was before this consolidation?%s"
                                       % (prop["record"], "" if prop["base_blob"] else " It was created by this "
                                          "consolidation, so it will be removed."), "/app/review/proposals/" + prop["id"],
                                       button="Undo")
                    inbox.undo(ctx.me, prop)
                    return back("/app/review", "undone")
            except InboxError as exc:
                return ctx.render("app_message.html", status=409, title="Not applied", message=str(exc),
                                  back="/app/review/proposals/" + prop["id"])
            return back("/app/review/proposals/" + prop["id"])
        who = names()
        sources, hidden = note_rows(ctx.me, json.loads(prop["note_ids"]))
        return ctx.render("app_proposal.html", prop=prop, who=who, lines=diff_lines(inbox.proposal_diff(prop)),
                          sources=sources, hidden_sources=hidden)

    @route("/app/review/conflicts/{cid}", ("GET", "POST"))
    @page
    async def conflict(ctx):
        c = db.one("SELECT * FROM conflicts WHERE id=?", ctx.request.path_params["cid"])
        if not c or not inbox.reviewable(ctx.me, c["scope"]):
            return not_found(ctx)
        if ctx.form is not None:
            if c["state"] != "open":
                return back("/app/review")
            try:
                inbox.resolve(ctx.me, c, ctx.form.get("action"), ctx.form.get("guidance", ""))
            except InboxError as exc:
                return ctx.render("app_message.html", status=400, title="Not resolved", message=str(exc),
                                  back="/app/review/conflicts/" + c["id"])
            return back("/app/review", "resolved")
        rec = memory.records().get(c["record"]) if c["record"] else None
        if rec is not None and not ctx.me.can_read(rec.meta["scope"]):
            rec = None
        sources, hidden = note_rows(ctx.me, json.loads(c["note_ids"]))
        return ctx.render("app_conflict.html", c=c, rec=rec, who=names(), sources=sources, hidden_sources=hidden)

    @route("/app/inbox/{note}")
    @page
    async def note_view(ctx):
        n = inbox.note(ctx.request.path_params["note"])
        if not n or not inbox.can_see(ctx.me, n):
            return not_found(ctx)
        return ctx.render("app_note.html", n=n, text=inbox.note_body(n), who=names())


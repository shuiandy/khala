"""Email code sign-in (shared by OAuth clients and the web UI) and the OAuth consent page.

An uninvited email gets exactly the same pages as an invited one, but no code is sent, so the list is not leaked.
A verified code does not go straight back to the client. It goes to the consent page first: name the agent,
pick its kind, tick its permission ceiling, and only after confirming is an authorization code issued.
"""
import asyncio
import hmac
import json
import logging
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlparse

from mcp.server.auth.provider import construct_redirect_uri
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from . import catalog
from .auth import AuthError
from .db import digest
from .oauth import PENDING_TTL
from .ui import render

log = logging.getLogger("memory.mail")
CODE_TTL = 10 * 60
MAX_ATTEMPTS = 5
SENDS_PER_EMAIL_HOUR = 5
SENDS_PER_IP_HOUR = 20


SMTP_PORTS = {"ssl": 465, "starttls": 587, "none": 25}


class SMTPMailer:
    """security: ssl (implicit TLS, usually port 465), starttls (usually 587) or none (a trusted local relay).
    Certificates are always verified: smtplib's own default for SMTP_SSL does not."""
    def __init__(self, host, port, username, password, sender, security="ssl", name="Khala"):
        if security not in SMTP_PORTS:
            raise ValueError("SMTP security must be one of %s" % ", ".join(SMTP_PORTS))
        self.host, self.username, self.password, self.sender = host, username, password, sender
        self.security, self.name = security, name
        self.port = int(port) if port else SMTP_PORTS[security]

    def _connect(self):
        if self.security == "ssl":
            return smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=ssl.create_default_context())
        s = smtplib.SMTP(self.host, self.port, timeout=20)
        if self.security == "starttls":
            s.starttls(context=ssl.create_default_context())
        return s

    def send_text(self, to, subject, body):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        with self._connect() as s:
            if self.username:                       # a local relay may take mail without signing in
                s.login(self.username, self.password)
            s.send_message(msg)

    def send(self, to, code):
        self.send_text(to, "%s sign-in code: %s" % (self.name, code),
                       "Your sign-in code is %s\n\nIt expires in 10 minutes. If you did not try to sign in, "
                       "ignore this email.\n" % code)


class LogMailer:
    """For development: sends nothing, writes codes and email bodies to the log."""
    def send(self, to, code):
        log.warning("sign-in code for %s: %s", to, code)

    def send_text(self, to, subject, body):
        log.warning("mail to %s: %s\n%s", to, subject, body)


class FakeMailer:
    """For tests: codes and other emails stay in memory."""
    def __init__(self):
        self.sent = []
        self.messages = []

    def send(self, to, code):
        self.sent.append((to, code))

    def send_text(self, to, subject, body):
        self.messages.append((to, subject, body))


def client_ip(request: Request):
    # khala serve has uvicorn rewrite request.client from X-Forwarded-For, for KHALA_TRUSTED_PROXIES only
    return request.client.host if request.client else ""


async def send_code(db, mailer, key, email, ip, allowed):
    """Rate-limit, then send a code to email. With allowed=False nothing is sent, but the path and result are the same.
    Returns an error message or None."""
    now = time.time()
    by_email = db.one("SELECT COUNT(*) n FROM sends WHERE email=? AND ts>?", email, now - 3600)["n"]
    by_ip = db.one("SELECT COUNT(*) n FROM sends WHERE ip=? AND ts>?", ip, now - 3600)["n"]
    if by_email >= SENDS_PER_EMAIL_HOUR or by_ip >= SENDS_PER_IP_HOUR:
        return "Too many codes requested. Try again later."
    db.q("INSERT INTO sends(email, ip, ts) VALUES (?,?,?)", email, ip, now)
    if allowed:
        code = "%06d" % secrets.randbelow(10 ** 6)
        db.q("INSERT OR REPLACE INTO login_codes(req, email, code_hash, expires, attempts) VALUES (?,?,?,?,0)",
             key, email, digest(code), now + CODE_TTL)
        try:
            await asyncio.to_thread(mailer.send, email, code)
        except Exception:
            return "Could not send the email. Try again later."
    return None


def check_code(db, key, email, code):
    """Returns (passed, error). After 5 wrong tries the code is void, and even the right one is refused.
    Counting the try and checking the limit is one statement, and so is using the code up, so concurrent
    requests (other workers included) cannot get extra tries or both sign in with one code."""
    rows = db.q("UPDATE login_codes SET attempts=attempts+1 WHERE req=? AND email=? AND expires>? AND attempts<? "
                "RETURNING code_hash", key, email, time.time(), MAX_ATTEMPTS)
    if not rows:
        db.q("DELETE FROM login_codes WHERE req=? AND email=?", key, email)
        return False, "expired"
    if not hmac.compare_digest(rows[0]["code_hash"], digest((code or "").strip())):
        return False, "wrong"
    if not db.changed("DELETE FROM login_codes WHERE req=? AND email=?", key, email):
        return False, "expired"
    return True, ""


HALF_LOGIN_TTL = 10 * 60
HALF_LOGIN_ATTEMPTS = 5


class LoginFlow:
    """Three ways to sign in: a passkey is enough on its own; an email code is enough for an account with no strong
    factor; for an account with a strong factor an email code is only a half login and needs a TOTP code,
    recovery code or passkey as well.

    key binds the steps to one browser: for the web UI it is the random string in the login cookie, for OAuth
    it is the pending authorization request ID.
    """
    def __init__(self, db, mailer, factors, require_strong=False):
        self.db, self.mailer, self.factors = db, mailer, factors
        self.require_strong = require_strong

    def needs_enrolment(self, account_id):
        """The instance requires a strong factor and this account has none yet."""
        return self.require_strong and not self.factors.has_strong(account_id)

    async def send(self, key, email, ip, invites=False):
        """An uninvited email takes the same path and gets the same page, it just gets no code. With invites=True
        (web UI), an email with no account but an open invite also gets a code, and signing in creates the account."""
        allowed = bool(self.db.active_account_by_email(email)) or (
            invites and not self.db.account_by_email(email) and bool(self.db.open_invite_for(email)))
        return await send_code(self.db, self.mailer, key, email, ip, allowed)

    def code(self, key, email, code, ip, target):
        """Returns ("ok", account) / ("second", account) / ("wrong" | "expired", account or None)."""
        ok, why = check_code(self.db, key, email, code)
        account = self.db.active_account_by_email(email)
        if not ok:
            self.db.audit("login.failed", account["id"] if account else None, target=target, detail=why, ip=ip)
            return why, None
        if account and self.factors.has_strong(account["id"]):
            self.db.q("INSERT OR REPLACE INTO half_logins(key, account_id, attempts, expires) VALUES (?,?,0,?)",
                      key, account["id"], time.time() + HALF_LOGIN_TTL)
            return "second", account
        return "ok", account

    def half(self, key):
        row = self.db.one("SELECT * FROM half_logins WHERE key=? AND expires>?", key, time.time())
        if not row or row["attempts"] >= HALF_LOGIN_ATTEMPTS:
            return None
        account = self.db.account(row["account_id"])
        return account if account and account["status"] == "active" else None

    def second(self, key, method, code, ip, target):
        """The second step after the email code. Returns (account, method) or (None, error)."""
        account = self.half(key)
        if not account:
            return None, "This sign-in expired. Start again."
        if not self.db.q("UPDATE half_logins SET attempts=attempts+1 WHERE key=? AND expires>? AND attempts<? "
                         "RETURNING 1", key, time.time(), HALF_LOGIN_ATTEMPTS):
            return None, "This sign-in expired. Start again."
        ok = (self.factors.check_totp(account["id"], code) if method == "totp" else
              self.factors.use_recovery(account["id"], code) if method == "recovery" else False)
        if not ok:
            self.db.audit("login.failed", account["id"], target=target, detail="second factor: " + method, ip=ip)
            return None, "That code did not work."
        if not self.db.changed("DELETE FROM half_logins WHERE key=?", key):     # finished by a concurrent request
            return None, "This sign-in expired. Start again."
        if method == "recovery":
            self.db.audit("auth.recovery_used", account["id"], target=target,
                          detail={"left": self.factors.recovery_left(account["id"])}, ip=ip)
        return account, "email+" + method

    def passkey_options(self, key, mode):
        """mode=second: the second step after the email code, listing only this account's passkeys.
        Otherwise a discoverable credential sign-in."""
        if mode == "second":
            account = self.half(key)
            if not account:
                raise AuthError("This sign-in expired. Start again.")
            return self.factors.login_options(key, account["id"])
        return self.factors.login_options(key)

    def passkey_verify(self, key, credential, ip, target):
        try:
            account_id, passkey = self.factors.authenticate(key, credential)
        except AuthError as exc:
            self.db.audit("login.failed", None, target=target, detail="passkey: %s" % exc, ip=ip)
            raise
        account = self.db.account(account_id)
        if not account or account["status"] != "active":
            raise AuthError("this account is disabled")
        self.db.q("DELETE FROM half_logins WHERE key=?", key)
        return account, "passkey:%s" % passkey["name"]


def json_error(message, status=400):
    return JSONResponse({"error": message}, status_code=status, headers={"Cache-Control": "no-store"})


def same_origin(request, origin):
    """Requests that change data must come from this site.

    Sec-Fetch-Site comes first: the browser adds it and page scripts cannot change it. With a no-referrer policy
    (privacy extensions, browser settings) a same-site POST carries Origin: null and no Referer, so relying on
    Origin alone would treat a valid request as cross-site. Only when the browser sends no Sec-Fetch-Site do we
    fall back to comparing Origin / Referer. An Origin that names another site is rejected outright."""
    sent = request.headers.get("origin")
    if sent and sent != "null" and sent != origin:
        return False
    site = request.headers.get("sec-fetch-site")
    if site:
        return site == "same-origin"
    src = sent if sent and sent != "null" else request.headers.get("referer") or ""
    return src == origin or src.startswith(origin + "/")


async def read_json(request):
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _redirect_is_local(uri):
    host = urlparse(uri).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def make_routes(server, provider, db, mailer, scopes_for, flow, origin):
    """scopes_for(account_id) -> [(scope_row, role)], the scopes the consent page offers to tick."""
    secure = origin.startswith("https://")
    async def client_info(client_id):
        client = await provider.get_client(client_id)
        name = client.client_name if client and client.client_name else "An app"
        return client, name

    def expired(status=400):
        return render("oauth_message.html", status=status, csp="oauth",
                      message="This sign-in link expired. Start again from your app.")

    def step(req, client_name, step, email="", error="", account=None):
        return render("login_steps.html", csp="oauth_js", step=step, action="/login", fields=[("req", req)],
                      subtitle="%s wants access to your memory." % client_name, email=email, error=error,
                      passkey_options="/login/passkey/options", passkey_verify="/login/passkey/verify",
                      has_passkey=bool(account and flow.factors.passkeys(account["id"])),
                      has_totp=bool(account and flow.factors.has_totp(account["id"])))

    def browser_cookie(req):
        return "ms_oauth_" + digest(req)[:16]          # one per request, so parallel grants in one browser do not clash

    def signed_in(response, req, client_id, account, method, ip):
        """Verified: bind this authorization request to the current browser, then go to the consent page."""
        secret = provider.mark_verified(req, account["id"])
        response.set_cookie(browser_cookie(req), secret, max_age=PENDING_TTL, httponly=True, secure=secure,
                            samesite="lax", path="/")
        db.audit("login.ok", account["id"], target="oauth:" + client_id, detail=method, ip=ip)
        return response

    @server.custom_route("/login", methods=["GET", "POST"], include_in_schema=False)
    async def login(request: Request):
        if request.method == "GET":
            req = request.query_params.get("req", "")
            found = provider.pending(req)
            if not found:
                return expired()
            return step(req, (await client_info(found[0]))[1], "email")

        form = await request.form()
        req, email = form.get("req", ""), (form.get("email") or "").strip().lower()
        found = provider.pending(req)
        if not found:
            return expired()
        name = (await client_info(found[0]))[1]
        ip, action = client_ip(request), form.get("action")
        if action == "send":
            err = await flow.send(req, email, ip)
            return step(req, name, "email", email, err) if err else step(req, name, "code", email)
        if action in ("totp", "recovery"):
            account, result = flow.second(req, action, form.get("code"), ip, "oauth")
            if not account:
                half = flow.half(req)
                return step(req, name, "second", email, result, half) if half else step(req, name, "email", "", result)
            return signed_in(RedirectResponse("/consent?req=" + req, status_code=303), req, found[0], account, result, ip)
        result, account = flow.code(req, email, form.get("code"), ip, "oauth")
        if result == "wrong":
            return step(req, name, "code", email, "Wrong code.")
        if result not in ("ok", "second") or not account:
            return step(req, name, "email", email, "That code expired or was used up. Request a new one.")
        if result == "second":
            return step(req, name, "second", email, account=account)
        return signed_in(RedirectResponse("/consent?req=" + req, status_code=303), req, found[0], account, "email", ip)

    @server.custom_route("/login/passkey/options", methods=["POST"], include_in_schema=False)
    async def passkey_options(request: Request):
        body = await read_json(request)
        if body is None or not same_origin(request, origin):
            return json_error("bad request", 403)
        req = body.get("req", "")
        if not provider.pending(req):
            return json_error("This sign-in link expired. Start again from your app.")
        try:
            return JSONResponse(flow.passkey_options(req, body.get("mode")), headers={"Cache-Control": "no-store"})
        except AuthError as exc:
            return json_error(str(exc))

    @server.custom_route("/login/passkey/verify", methods=["POST"], include_in_schema=False)
    async def passkey_verify(request: Request):
        body = await read_json(request)
        if body is None or not same_origin(request, origin):
            return json_error("bad request", 403)
        req = body.get("req", "")
        found = provider.pending(req)
        if not found:
            return json_error("This sign-in link expired. Start again from your app.")
        ip = client_ip(request)
        try:
            account, method = flow.passkey_verify(req, body.get("credential") or {}, ip, "oauth")
        except AuthError as exc:
            return json_error(str(exc))
        return signed_in(JSONResponse({"redirect": "/consent?req=" + req}), req, found[0], account, method, ip)

    def consent_defaults(account_id, client_id, params, client_name):
        existing = db.agent_for_client(account_id, client_id)
        if existing:
            ceiling = json.loads(existing["ceiling"]) if existing["ceiling"] else None
            return existing, existing["name"], existing["kind"], ceiling
        wants_offline_only = set(params.get("scopes") or []) == {"offline_access"}
        kind = "device" if _redirect_is_local(params["redirect_uri"]) or not wants_offline_only else "bot"
        entry, _ = catalog.identify(catalog.entries(), client_id, client_name)
        if entry:                               # a known client: its usual name and kind, still editable below
            client_name, kind = entry["name"], entry["kind"]
        return None, client_name, kind, (None if kind == "device" else {})

    def same_client_agents(account_id, client_id, client_name, entry):
        """This account's other live agents for the same known client: a reconnection under a new client id
        (after a client switches from registration to a metadata document, say) leaves the old one behind."""
        if not entry:
            return []
        out = []
        for a in db.agents_for(account_id):
            if a["revoked_at"] or not a["oauth_client_id"] or a["oauth_client_id"] == client_id:
                continue
            row = db.one("SELECT info FROM clients WHERE client_id=?", a["oauth_client_id"])
            name = json.loads(row["info"]).get("client_name", "") if row else ""
            other, _ = catalog.identify(catalog.entries(), a["oauth_client_id"], name)
            if other and other["id"] == entry["id"]:
                out.append(a)
        return out

    @server.custom_route("/consent", methods=["GET", "POST"], include_in_schema=False)
    async def consent(request: Request):
        if request.method == "GET":
            req = request.query_params.get("req", "")
            form = {}
        else:
            form = await request.form()
            req = form.get("req", "")
        if request.method == "POST" and not same_origin(request, origin):
            return expired(403)
        found = provider.pending(req)
        account_id = provider.consenting_account(req, request.cookies.get(browser_cookie(req)))
        account = db.account(account_id) if account_id else None
        if not found or not account or account["status"] != "active":
            return expired()
        if flow.needs_enrolment(account_id):
            return render("oauth_message.html", status=403, csp="oauth",
                          message="This server requires a passkey or an authenticator app before you connect an "
                          "app. Sign in at %s/app, add one on the Security page, then connect again." % origin)
        client_id, params = found
        _, client_name = await client_info(client_id)
        existing, name, kind, ceiling = consent_defaults(account_id, client_id, params, client_name)
        scopes = [(s, role) for s, role in scopes_for(account_id) if not s["archived_at"]]
        known, verified = catalog.identify(catalog.entries(), client_id, client_name)
        others = same_client_agents(account_id, client_id, client_name, known)

        def page(error=""):
            return render("oauth_consent.html", csp="oauth", req=req, client_name=client_name, account=account,
                          existing=existing, name=name, kind=kind, ceiling=ceiling, scopes=scopes, error=error,
                          known=known, verified=verified, others=others)

        if request.method == "GET":
            return page()
        if form.get("action") == "deny":
            db.q("DELETE FROM pending WHERE id=?", req)
            db.audit("consent.denied", account_id, target=client_id, ip=client_ip(request))
            return RedirectResponse(construct_redirect_uri(params["redirect_uri"], error="access_denied",
                                                           state=params["state"]), status_code=302)
        name = (form.get("name") or "").strip()[:80] or client_name
        kind = form.get("kind") if form.get("kind") in ("device", "bot") else kind
        if form.get("access") == "all":
            ceiling = None
        else:
            ceiling = {}
            for s, role in scopes:
                mode = form.get("scope:" + s["id"])
                if mode == "rw" and role != "viewer":
                    ceiling[s["id"]] = "rw"
                elif mode in ("r", "rw"):
                    ceiling[s["id"]] = "r"
            if not ceiling:
                return page("Pick at least one scope, or allow all of them.")
        if existing:
            db.update_agent(existing["id"], name=name, kind=kind, ceiling=ceiling)
            agent_id = existing["id"]
        else:
            agent_id = db.create_agent(account_id, name, kind, client_id, ceiling)
        db.audit("agent.authorized", account_id, agent_id, target=client_id,
                 detail={"name": name, "kind": kind, "ceiling": ceiling}, ip=client_ip(request))
        resp = RedirectResponse(provider.complete_login(req, account_id, agent_id), status_code=302)
        resp.delete_cookie(browser_cookie(req), path="/")
        return resp

    @server.custom_route("/oauth/callback", methods=["GET"], include_in_schema=False)
    async def callback(request: Request):
        """For an agent whose own callback the person's browser cannot reach (a bot on a cloud machine): it registers
        this page as its redirect URI, and the person copies the result back to it. The code arrives in the
        fragment (see AuthServerExtras), so this handler never sees it."""
        return render("oauth_callback.html", csp="oauth_js")

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(request: Request):
        return PlainTextResponse("ok")

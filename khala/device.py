"""Device authorization (RFC 8628), for command-line tools that set up a client and have no browser of their own.

The tool asks for a device code and shows the person a short user code and a URL. The person, signed in on the web,
enters the code, names the agent, limits what it may reach and approves (with a recent identity check, as for any
long-lived token). The tool's next poll receives a bearer token, once; the server never stores it.
"""
import json
import secrets
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from .db import digest
from .login import client_ip

USER_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"     # no vowels (no words), no lookalike digits
CODE_TTL = 10 * 60
INTERVAL = 5
STARTS_PER_IP_HOUR = 20
DAYS = (7, 30, 90, 365)
GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceError(Exception):
    """error is an RFC 8628 / RFC 6749 error code for the polling client."""
    def __init__(self, error, description=""):
        super().__init__(description or error)
        self.error, self.description = error, description


def normalize(user_code):
    return "".join(c for c in (user_code or "").upper() if c in USER_ALPHABET)


class DeviceFlow:
    def __init__(self, db, provider, issuer):
        self.db, self.provider, self.issuer = db, provider, issuer.rstrip("/")

    def start(self, client_name, ip):
        now = time.time()
        recent = self.db.one("SELECT COUNT(*) n FROM device_codes WHERE ip=? AND created>?", ip, now - 3600)["n"]
        if recent >= STARTS_PER_IP_HOUR:
            raise DeviceError("slow_down", "too many device codes from this address; try again later")
        device_code = secrets.token_urlsafe(32)
        user_code = "".join(secrets.choice(USER_ALPHABET) for _ in range(8))
        name = " ".join((client_name or "").split())[:80] or "Command line"
        self.db.q("INSERT INTO device_codes(device_hash, user_hash, client_name, ip, created, expires, interval) "
                  "VALUES (?,?,?,?,?,?,?)", digest(device_code), digest(user_code), name, ip, now, now + CODE_TTL,
                  INTERVAL)
        shown = user_code[:4] + "-" + user_code[4:]
        return {"device_code": device_code, "user_code": shown, "verification_uri": self.issuer + "/app/device",
                "verification_uri_complete": "%s/app/device?code=%s" % (self.issuer, shown),
                "expires_in": CODE_TTL, "interval": INTERVAL}

    def pending(self, user_code):
        code = normalize(user_code)
        if len(code) != 8:
            return None
        return self.db.one("SELECT * FROM device_codes WHERE user_hash=? AND state='pending' AND expires>?",
                           digest(code), time.time())

    def decide(self, user_code, account_id, approve, name="", ceiling=None, days=90):
        """Approve (creating the agent) or deny a pending code. The state check and change are one transaction, so
        a code is decided once."""
        code = normalize(user_code)
        with self.db.tx():
            c = self.db.conn
            row = c.execute("SELECT * FROM device_codes WHERE user_hash=? AND state='pending' AND expires>?",
                            (digest(code), time.time())).fetchone()
            if not row:
                return None
            if not approve:
                c.execute("UPDATE device_codes SET state='denied', account_id=? WHERE device_hash=?",
                          (account_id, row["device_hash"]))
                return None
            agent_id = c.execute(
                "INSERT INTO agents(account_id, name, kind, oauth_client_id, ceiling, created_at) VALUES (?,?,?,?,?,?)",
                (account_id, name or row["client_name"], "device", None,
                 None if ceiling is None else json.dumps(ceiling), time.time())).lastrowid
            c.execute("UPDATE device_codes SET state='approved', account_id=?, agent_id=?, days=? WHERE device_hash=?",
                      (account_id, agent_id, days, row["device_hash"]))
            return agent_id

    def poll(self, device_code):
        """The token once the person approved, else the RFC 8628 error to keep polling or stop."""
        h, now = digest(device_code or ""), time.time()
        row = self.db.one("SELECT * FROM device_codes WHERE device_hash=?", h)
        if not row or row["expires"] < now:
            raise DeviceError("expired_token", "the code expired; start again")
        if row["state"] == "denied":
            self.db.q("DELETE FROM device_codes WHERE device_hash=?", h)
            raise DeviceError("access_denied", "the request was denied")
        if row["state"] == "pending":
            # polling faster than the interval: tell the client to back off, and lengthen it (RFC 8628 3.5)
            if not self.db.changed("UPDATE device_codes SET last_poll=? WHERE device_hash=? AND "
                                   "(last_poll IS NULL OR last_poll<=?)", now, h, now - row["interval"] + 0.5):
                self.db.q("UPDATE device_codes SET interval=MIN(interval+5, 60) WHERE device_hash=?", h)
                raise DeviceError("slow_down")
            raise DeviceError("authorization_pending")
        taken = self.db.q("DELETE FROM device_codes WHERE device_hash=? AND state='approved' "
                          "RETURNING account_id, agent_id, days", h)
        if not taken:                                   # another poll took it a moment ago
            raise DeviceError("expired_token", "the token was already issued")
        t = taken[0]
        token = self.provider.issue_bearer(t["account_id"], t["agent_id"], t["days"])
        self.db.audit("agent.token_issued", t["account_id"], t["agent_id"], target="agent:%d" % t["agent_id"],
                      detail={"via": "device", "days": t["days"]})
        return {"access_token": token, "token_type": "Bearer", "expires_in": t["days"] * 86400}


def make_routes(server, devices):
    """POST /device/code starts; POST /device/token polls. Both answer JSON and are never cached."""
    no_store = {"Cache-Control": "no-store"}

    async def fields(request):
        if "application/json" in request.headers.get("content-type", ""):
            try:
                body = await request.json()
            except ValueError:
                body = None
            return body if isinstance(body, dict) else {}
        return dict(await request.form())

    @server.custom_route("/device/code", methods=["POST"], include_in_schema=False)
    async def device_code(request: Request):
        f = await fields(request)
        try:
            return JSONResponse(devices.start(f.get("client_name", ""), client_ip(request)), headers=no_store)
        except DeviceError as exc:
            return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=429,
                                headers=no_store)

    @server.custom_route("/device/token", methods=["POST"], include_in_schema=False)
    async def device_token(request: Request):
        f = await fields(request)
        if f.get("grant_type", GRANT) != GRANT:
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=no_store)
        try:
            return JSONResponse(devices.poll(f.get("device_code", "")), headers=no_store)
        except DeviceError as exc:
            return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=400,
                                headers=no_store)

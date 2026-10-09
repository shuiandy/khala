"""OAuth authorization server. Identity is confirmed by the email code at /login; this only issues, checks and
rotates codes and tokens."""
import asyncio
import hmac
import ipaddress
import json
import time
from urllib.parse import urlparse

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import cimd
from .db import DB, digest, new_secret

PENDING_TTL = 15 * 60
CODE_TTL = 5 * 60
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 90 * 24 * 3600
# A rotated refresh token may be exchanged once more within this many seconds: the client may have lost the
# response and retried. Any later reuse means a copy is in someone else's hands, and the whole chain is revoked.
REFRESH_GRACE = 30
REGISTRATIONS_PER_IP_HOUR = 20


def _account(subject):
    try:
        return int(subject)
    except (TypeError, ValueError):
        return None


def redirect_allowed(uri: str) -> bool:
    """Accept only https, or http on a loopback address (callbacks of local clients)."""
    p = urlparse(uri)
    if p.scheme == "https" and p.hostname:
        return True
    if p.scheme == "http" and p.hostname:
        if p.hostname == "localhost":
            return True
        try:
            return ipaddress.ip_address(p.hostname).is_loopback
        except ValueError:
            return False
    return False


SCOPES = ["memory", "offline_access"]


class Provider:
    def __init__(self, db: DB, issuer: str, documents=None, client_ip=lambda: ""):
        self.db = db
        self.issuer = issuer.rstrip("/")
        self.documents = documents or cimd.Documents(redirect_allowed, SCOPES)
        self.client_ip = client_ip

    # ---- clients ----
    async def get_client(self, client_id):
        """A client registered here, or one that identifies itself with a metadata document at an https URL."""
        if (client_id or "").startswith("https://"):
            try:
                return await asyncio.to_thread(self.documents.get, client_id)
            except cimd.CimdError:
                return None
        row = self.db.one("SELECT info FROM clients WHERE client_id=?", client_id)
        return cimd.Client.model_validate_json(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull):
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or not all(redirect_allowed(u) for u in uris):
            raise RegistrationError(error="invalid_redirect_uri",
                                    error_description="redirect URIs must be https or loopback http")
        self.db.q("INSERT OR REPLACE INTO clients(client_id, info, created, ip) VALUES (?,?,?,?)",
                  client_info.client_id, client_info.model_dump_json(), time.time(), self.client_ip())

    def registration_allowed(self, ip) -> bool:
        """Dynamic registration is open to anyone, so one address gets a bounded number of clients an hour."""
        self.db.purge_clients()
        n = self.db.one("SELECT COUNT(*) n FROM clients WHERE ip=? AND created>?", ip, time.time() - 3600)["n"]
        return n < REGISTRATIONS_PER_IP_HOUR

    # ---- authorization: park the request, send to the login page ----
    async def authorize(self, client, params: AuthorizationParams) -> str:
        req = new_secret(24)
        data = {"state": params.state, "scopes": params.scopes, "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource}
        self.db.q("INSERT INTO pending(id, client_id, params, expires) VALUES (?,?,?,?)",
                  req, client.client_id, json.dumps(data), time.time() + PENDING_TTL)
        return "%s/login?req=%s" % (self.issuer, req)

    def pending(self, req):
        row = self.db.one("SELECT * FROM pending WHERE id=? AND expires>?", req or "", time.time())
        return (row["client_id"], json.loads(row["params"])) if row else None

    def mark_verified(self, req, account_id):
        """Verified but not yet confirmed on the consent page: record who it is and give the verifying browser a secret
        tied to this request. req passes through the client (which can call /authorize itself), so the consent page
        cannot trust req alone and needs this secret. Returns the plain secret."""
        secret = new_secret(24)
        self.db.q("UPDATE pending SET account_id=?, browser=?, expires=? WHERE id=?", account_id, digest(secret),
                  time.time() + PENDING_TTL, req)
        return secret

    def verified_account(self, req):
        row = self.db.one("SELECT account_id FROM pending WHERE id=? AND expires>?", req or "", time.time())
        return row["account_id"] if row else None

    def consenting_account(self, req, browser_secret):
        """For the consent page: only the browser holding the secret issued at verification can see and submit it."""
        row = self.db.one("SELECT account_id, browser FROM pending WHERE id=? AND expires>?", req or "", time.time())
        if not row or row["account_id"] is None or not row["browser"] or not browser_secret:
            return None
        return row["account_id"] if hmac.compare_digest(row["browser"], digest(browser_secret)) else None

    def complete_login(self, req, account_id, agent_id) -> str:
        """Called once the consent page is confirmed: issues the authorization code, returns the client redirect."""
        found = self.pending(req)
        if not found or self.verified_account(req) != account_id:
            raise ValueError("login request expired")
        client_id, p = found
        self.db.q("DELETE FROM pending WHERE id=?", req)
        code = new_secret(32)
        data = dict(p, scopes=p["scopes"] or ["memory"], agent_id=agent_id)
        self.db.q("INSERT INTO auth_codes(code_hash, client_id, subject, data, expires) VALUES (?,?,?,?,?)",
                  digest(code), client_id, str(account_id), json.dumps(data), time.time() + CODE_TTL)
        return construct_redirect_uri(p["redirect_uri"], code=code, state=p["state"])

    # ---- exchanging authorization codes for tokens ----
    async def load_authorization_code(self, client, authorization_code):
        row = self.db.one("SELECT * FROM auth_codes WHERE code_hash=? AND client_id=? AND expires>?",
                          digest(authorization_code), client.client_id, time.time())
        if not row:
            return None
        d = json.loads(row["data"])
        return AuthorizationCode(code=authorization_code, scopes=d["scopes"], expires_at=row["expires"],
                                 client_id=row["client_id"], code_challenge=d["code_challenge"],
                                 redirect_uri=d["redirect_uri"],
                                 redirect_uri_provided_explicitly=d["redirect_uri_provided_explicitly"],
                                 resource=d.get("resource"), subject=row["subject"])

    def _issue(self, client_id, subject, scopes, resource, agent_id) -> OAuthToken:
        # clients that asked only for offline_access must still be able to use tools, so memory is always included
        scopes = sorted(set(scopes or []) | {"memory"})
        access, refresh = new_secret(32), new_secret(32)
        now = time.time()
        for token, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
            self.db.q("INSERT INTO tokens(token_hash, kind, client_id, subject, scopes, resource, expires, agent_id) "
                      "VALUES (?,?,?,?,?,?,?,?)", digest(token), kind, client_id, subject,
                      " ".join(scopes), resource, now + ttl, agent_id)
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client, authorization_code):
        # single use: only one of two concurrent exchanges gets the row back
        rows = self.db.q("DELETE FROM auth_codes WHERE code_hash=? RETURNING data", digest(authorization_code.code))
        if not rows:
            raise TokenError(error="invalid_grant", error_description="this authorization code was already used")
        agent_id = json.loads(rows[0]["data"]).get("agent_id")
        if not self._live(authorization_code.subject, agent_id):
            raise TokenError(error="invalid_grant", error_description="this sign-in was revoked")
        self.db.audit("token.issued", int(authorization_code.subject), agent_id, target=client.client_id)
        return self._issue(client.client_id, authorization_code.subject, authorization_code.scopes,
                           authorization_code.resource, agent_id)

    # ---- refresh tokens (rotated) ----
    def _token(self, token, kind):
        return self.db.one("SELECT * FROM tokens WHERE token_hash=? AND kind=? AND revoked=0 AND expires>?",
                           digest(token), kind, time.time())

    def _live(self, subject, agent_id):
        """The account exists and is not disabled, and the agent is not revoked."""
        try:
            account = self.db.account(int(subject))
        except (TypeError, ValueError):
            return False
        if not account or account["status"] != "active":
            return False
        agent = self.db.agent(agent_id) if agent_id is not None else None
        return bool(agent and agent["account_id"] == account["id"] and agent["revoked_at"] is None)

    def _in_grace(self, row):
        return bool(row["rotated_at"]) and not row["grace_used"] and row["rotated_at"] >= time.time() - REFRESH_GRACE

    def _revoke_chain(self, row):
        """Revoke every access and refresh token of this sign-in chain (same client, account and agent). Clearing
        rotated_at makes later presentations of any of them a plain refusal, not another reuse report."""
        agent = "agent_id=?" if row["agent_id"] is not None else "agent_id IS NULL"
        args = [row["client_id"], row["subject"]] + ([row["agent_id"]] if row["agent_id"] is not None else [])
        self.db.q("UPDATE tokens SET revoked=1, rotated_at=NULL WHERE client_id=? AND subject=? AND "
                  "kind IN ('access', 'refresh') AND " + agent, *args)

    def _reused(self, row):
        """A rotated refresh token came back after its grace period: revoke the chain and record it, so the person
        sees it in the audit log."""
        self._revoke_chain(row)
        self.db.audit("token.reuse_detected", _account(row["subject"]), row["agent_id"], target=row["client_id"])

    async def load_refresh_token(self, client, refresh_token):
        row = self.db.one("SELECT * FROM tokens WHERE token_hash=? AND kind='refresh' AND expires>?",
                          digest(refresh_token), time.time())
        if not row or row["client_id"] != client.client_id or not self._live(row["subject"], row["agent_id"]):
            return None
        if row["revoked"] and not self._in_grace(row):
            if row["rotated_at"]:
                self._reused(row)
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"].split(),
                            expires_at=int(row["expires"]), resource=row["resource"], subject=row["subject"])

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        h = digest(refresh_token.token)
        row = self.db.token_row(h)
        # checked again here, not only in load: the agent may have been revoked in between
        if row is None or not self._live(row["subject"], row["agent_id"]):
            raise TokenError(error="invalid_grant", error_description="this refresh token is no longer valid")
        now = time.time()
        # each step is a conditional UPDATE, so two concurrent exchanges cannot both pass the same check
        if not self.db.changed("UPDATE tokens SET revoked=1, rotated_at=? WHERE token_hash=? AND revoked=0", now, h):
            if not self.db.changed("UPDATE tokens SET grace_used=1 WHERE token_hash=? AND revoked=1 AND grace_used=0 "
                                   "AND rotated_at>=?", h, now - REFRESH_GRACE):
                # read again: it may have been revoked explicitly since, which is not a reuse
                fresh = self.db.token_row(h)
                if fresh and fresh["rotated_at"]:
                    self._reused(fresh)
                    raise TokenError(error="invalid_grant", error_description="this refresh token was already used")
                raise TokenError(error="invalid_grant", error_description="this refresh token is no longer valid")
        return self._issue(client.client_id, row["subject"], scopes or refresh_token.scopes,
                           refresh_token.resource, row["agent_id"])

    # ---- access tokens ----
    async def load_access_token(self, token):
        # bearer: long-lived bot tokens issued in the web UI take the same check path as OAuth access tokens
        row = self._token(token, "access") or self._token(token, "bearer")
        if not row or not self._live(row["subject"], row["agent_id"]):      # disable or revoke takes effect at once
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                           expires_at=int(row["expires"]), resource=row["resource"], subject=row["subject"])

    def issue_bearer(self, account_id, agent_id, days):
        """Issue a long-lived bot token: the plain token is returned only this once, the database keeps only a hash."""
        token = "mem_" + new_secret(32)
        self.db.q("INSERT INTO tokens(token_hash, kind, client_id, subject, scopes, resource, expires, agent_id) "
                  "VALUES (?,?,?,?,?,?,?,?)", digest(token), "bearer", "bearer", str(account_id), "memory", None,
                  time.time() + days * 86400, agent_id)
        return token

    async def revoke_token(self, token):
        row = self.db.token_row(digest(token.token))
        self.db.q("UPDATE tokens SET revoked=1, rotated_at=NULL WHERE token_hash=?", digest(token.token))
        if row and row["kind"] == "refresh":
            # RFC 7009: revoking a refresh token also ends the access tokens of the same grant
            self._revoke_chain(row)
        if row:
            self.db.audit("token.revoked", _account(row["subject"]), row["agent_id"], target=row["client_id"])

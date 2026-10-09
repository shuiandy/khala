"""Cloud bots: agents on a machine the person's browser cannot reach. They find device authorization in the metadata
and poll for it at the token endpoint, or use the server's own callback page, where the code stays out of logs.
Registrations are limited per address, and clients that never got anywhere are forgotten."""
import base64
import hashlib
import secrets
import time
import unittest
from urllib.parse import parse_qs, urlparse

from base import ALICE, Base

CALLBACK = "http://localhost/oauth/callback"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class CloudBotTests(Base):
    def test_metadata_offers_device_authorization(self):
        m = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(m["device_authorization_endpoint"], "http://localhost/device/code")
        self.assertIn(GRANT, m["grant_types_supported"])
        self.assertIn("authorization_code", m["grant_types_supported"])

    def test_device_code_is_polled_at_the_token_endpoint(self):
        started = self.client.post("/device/code", data={"client_name": "Muse"}).json()
        poll = {"grant_type": GRANT, "device_code": started["device_code"]}
        pending = self.client.post("/token", data=poll)
        self.assertEqual((pending.status_code, pending.json()["error"]), (400, "authorization_pending"))
        self.web_login(ALICE)
        page = "/app/device?code=" + started["user_code"]
        self.post("/app/device", {"code": started["user_code"], "action": "approve", "access": "all", "days": "30"},
                  page=page)
        self.db.q("UPDATE device_codes SET last_poll=NULL")
        got = self.client.post("/token", data=poll)
        self.assertEqual(got.status_code, 200, got.text)
        self.assertTrue(self.call(got.json()["access_token"], "memory_scopes"))

    def test_other_grants_still_reach_the_token_endpoint(self):
        tokens, client_id = self.login(ALICE)
        r = self.client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                             "client_id": client_id})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_callback_page_gets_the_code_in_the_fragment(self):
        client_id = self.client.post("/register", json={
            "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none", "scope": "memory offline_access",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "client_name": "Muse"}).json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        r = self.client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "cb1", "scope": "memory offline_access"}, follow_redirects=False)
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        self.client.get(self.verify(req, ALICE))
        r = self.consent(req, kind="bot")
        back = urlparse(r.headers["location"])
        self.assertEqual((back.path, back.query), ("/oauth/callback", ""))   # nothing for a log to keep
        q = parse_qs(back.fragment)
        self.assertEqual((q["state"], q["iss"]), (["cb1"], ["http://localhost"]))
        tok = self.client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                               "redirect_uri": CALLBACK, "client_id": client_id,
                                               "code_verifier": verifier})
        self.assertEqual(tok.status_code, 200, tok.text)
        self.assertIn("refresh_token", tok.json())

    def test_a_denial_also_lands_in_the_fragment(self):
        client_id = self.register(CALLBACK).json()["client_id"]
        r = self.client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "code_challenge": "x" * 43,
            "code_challenge_method": "S256", "state": "cb2"}, follow_redirects=False)
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        self.client.get(self.verify(req, ALICE))
        back = urlparse(self.consent(req, action="deny").headers["location"])
        self.assertEqual(back.query, "")
        self.assertEqual(parse_qs(back.fragment)["error"], ["access_denied"])

    def test_the_callback_page_only_runs_its_own_script(self):
        r = self.client.get("/oauth/callback")
        self.assertEqual(r.status_code, 200)
        self.assertIn("script-src 'self'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("/app/static/callback.js", r.text)
        js = self.client.get("/app/static/callback.js")
        self.assertEqual((js.status_code, js.headers["content-type"].split(";")[0]), (200, "text/javascript"))

    def test_other_redirects_keep_the_code_in_the_query(self):
        tokens, _ = self.login(ALICE)                 # login() reads the code from the query of a loopback callback
        self.assertIn("access_token", tokens)

    def test_one_address_cannot_register_endless_clients(self):
        for _ in range(20):
            self.assertEqual(self.register().status_code, 201)
        r = self.register()
        self.assertEqual((r.status_code, r.json()["error"]), (429, "too_many_requests"))
        self.db.q("UPDATE clients SET created=created-3601")
        self.assertEqual(self.register().status_code, 201)

    def test_clients_that_never_signed_in_are_forgotten(self):
        _, used = self.login(ALICE)
        unused = self.register().json()["client_id"]
        fresh = self.register().json()["client_id"]
        week = 7 * 86400 + 1
        self.db.q("UPDATE clients SET created=? WHERE client_id IN (?, ?)", time.time() - week, used, unused)
        self.db.purge_clients()
        left = {r["client_id"] for r in self.db.q("SELECT client_id FROM clients")}
        self.assertEqual(left, {used, fresh})


if __name__ == "__main__":
    unittest.main()

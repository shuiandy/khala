"""Passkeys, TOTP, recovery codes; a strong factor ends email-code-only sign-in; scripts only on auth pages."""
import contextlib
import io
import os
import re
import time
import unittest
from urllib.parse import parse_qs, urlparse

from base import ORIGIN, OWNER, ALICE, Base
from khala import cli
from khala.auth import TOTP_STEP, totp_at
from softauthn import SoftPasskey, b64u


class AuthTests(Base):
    # ---------- helpers ----------
    def jpost(self, path, body, origin=ORIGIN):
        return self.client.post(path, json=body, headers=origin)

    def add_passkey_via_web(self, name="Test Mac"):
        """Signed-in web session: register a software passkey through the security page."""
        key = SoftPasskey("http://localhost")
        token = self.csrf("/app/security")
        opts = self.jpost("/app/security/passkey/options", {"csrf": token})
        self.assertEqual(opts.status_code, 200, opts.text)
        r = self.jpost("/app/security/passkey/verify", {"csrf": token, "name": name, "credential": key.create(opts.json())})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["redirect"], "/app/security?msg=passkey-added")
        return key

    def turn_on_totp_via_web(self):
        page = self.post("/app/security/totp", {"action": "start"}, page="/app/security")
        self.assertIn("<svg", page.text)
        secret = re.search(r"Enter this key by hand: <code>([A-Z2-7]+)</code>", page.text).group(1)
        step = int(time.time() // TOTP_STEP)
        r = self.post("/app/security/totp", {"action": "confirm", "code": totp_at(secret, step)}, page="/app/security")
        self.assertEqual(r.headers["location"], "/app/security?msg=totp-enabled")
        return secret, step

    def email_half_login(self, email):
        """The email code step: returns (nonce, page)."""
        self.client.cookies.clear()
        nonce = re.search(r'name="nonce" value="([^"]+)"', self.client.get("/app/login").text).group(1)
        self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "email": email, "action": "send"})
        code = [c for to, c in self.mailer.sent if to == email][-1]
        r = self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "email": email, "code": code,
                                                 "action": "verify"}, follow_redirects=False)
        return nonce, r

    def signed_in(self):
        return self.client.get("/app", follow_redirects=False).status_code == 200

    # ---------- passkey ----------
    def test_passkey_signs_in_without_email_and_email_alone_stops_working(self):
        self.web_login(OWNER)
        self.assertIn("protected only by your email", self.client.get("/app").text)
        key = self.add_passkey_via_web()
        self.assertNotIn("protected only by your email", self.client.get("/app").text)

        # no email entered, use a passkey directly
        self.client.cookies.clear()
        nonce = re.search(r'name="nonce" value="([^"]+)"', self.client.get("/app/login").text).group(1)
        opts = self.jpost("/app/login/passkey/options", {"nonce": nonce, "mode": "discoverable"}).json()
        self.assertFalse(opts.get("allowCredentials"))            # discoverable credential: doesn't reveal whose
        r = self.jpost("/app/login/passkey/verify", {"nonce": nonce, "next": "/app/agents", "credential": key.get(opts)})
        self.assertEqual(r.json(), {"redirect": "/app/agents"})
        self.assertTrue(self.signed_in())
        self.assertTrue(self.db.one("SELECT 1 FROM audit_events WHERE action='login.ok' AND detail LIKE 'passkey:%'"))

        # email code only: stops at the second step, no session
        _, r = self.email_half_login(OWNER)
        self.assertEqual(r.status_code, 200)
        self.assertIn("One more step", r.text)
        self.assertFalse(self.signed_in())

    def test_passkey_works_when_the_browser_hides_the_origin(self):
        self.web_login(OWNER)
        key = self.add_passkey_via_web()
        self.client.cookies.clear()
        nonce = re.search(r'name="nonce" value="([^"]+)"', self.client.get("/app/login").text).group(1)
        hidden = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
        opts = self.jpost("/app/login/passkey/options", {"nonce": nonce}, origin=hidden)
        self.assertEqual(opts.status_code, 200)
        r = self.jpost("/app/login/passkey/verify", {"nonce": nonce, "credential": key.get(opts.json())}, origin=hidden)
        self.assertEqual(r.status_code, 200)

    def test_passkey_as_second_step_after_email(self):
        self.web_login(OWNER)
        key = self.add_passkey_via_web()
        nonce, r = self.email_half_login(OWNER)
        self.assertIn("Use my passkey", r.text)
        opts = self.jpost("/app/login/passkey/options", {"nonce": nonce, "mode": "second"}).json()
        self.assertEqual([c["id"] for c in opts["allowCredentials"]], [b64u(key.cred_id)])
        r = self.jpost("/app/login/passkey/verify", {"nonce": nonce, "next": "/app", "credential": key.get(opts)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.signed_in())

    def test_passkey_checks_origin_challenge_and_binding(self):
        self.web_login(OWNER)
        key = self.add_passkey_via_web()
        self.client.cookies.clear()
        nonce = re.search(r'name="nonce" value="([^"]+)"', self.client.get("/app/login").text).group(1)
        opts = self.jpost("/app/login/passkey/options", {"nonce": nonce}).json()
        # an assertion obtained by another site: wrong origin
        r = self.jpost("/app/login/passkey/verify", {"nonce": nonce, "credential": key.get(opts, origin="https://evil.example")})
        self.assertEqual(r.status_code, 400)
        # a challenge works only once, replaying the same assertion fails too
        opts = self.jpost("/app/login/passkey/options", {"nonce": nonce}).json()
        good = key.get(opts)
        self.assertEqual(self.jpost("/app/login/passkey/verify", {"nonce": nonce, "credential": good}).status_code, 200)
        self.client.cookies.clear()
        nonce2 = re.search(r'name="nonce" value="([^"]+)"', self.client.get("/app/login").text).group(1)
        self.assertEqual(self.jpost("/app/login/passkey/verify", {"nonce": nonce2, "credential": good}).status_code, 400)
        # no sign-in cookie, or a cross-site request: reject outright
        self.assertEqual(self.jpost("/app/login/passkey/options", {"nonce": "guess"}).status_code, 403)
        self.assertEqual(self.jpost("/app/login/passkey/options", {"nonce": nonce2},
                                    origin={"Origin": "https://evil.example"}).status_code, 403)

    def test_passkey_without_user_verification_is_refused(self):
        self.web_login(OWNER)
        key = SoftPasskey("http://localhost", user_verified=False)
        token = self.csrf("/app/security")
        opts = self.jpost("/app/security/passkey/options", {"csrf": token}).json()
        r = self.jpost("/app/security/passkey/verify", {"csrf": token, "name": "x", "credential": key.create(opts)})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.app.state.factors.passkeys(self.owner_id))

    # ---------- TOTP and recovery codes ----------
    def test_totp_second_step_replay_and_recovery_codes(self):
        self.web_login(OWNER)
        secret, step = self.turn_on_totp_via_web()
        self.assertNotIn(secret, str(self.db.one("SELECT secret FROM totp")["secret"]))     # encrypted at rest
        page = self.post("/app/security/recovery", {}, page="/app/security").text
        codes = re.findall(r"<div>([a-z2-9]{5}-[a-z2-9]{5})</div>", page)
        self.assertEqual(len(codes), 10)

        nonce, r = self.email_half_login(OWNER)
        self.assertIn("authenticator app", r.text)
        r = self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "action": "totp", "code": "000000"})
        self.assertIn("did not work", r.text)
        self.assertFalse(self.signed_in())
        # the time window used for confirmation can't be reused
        r = self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "action": "totp",
                                                 "code": totp_at(secret, step)}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "action": "totp",
                                                 "code": totp_at(secret, step + 1)}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.signed_in())

        nonce, _ = self.email_half_login(OWNER)
        r = self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "action": "recovery",
                                                 "code": codes[0].upper()}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.app.state.factors.recovery_left(self.owner_id), 9)
        nonce, _ = self.email_half_login(OWNER)
        r = self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "action": "recovery", "code": codes[0]})
        self.assertIn("did not work", r.text)

    def test_second_step_gives_up_after_five_tries(self):
        self.web_login(OWNER)
        secret, step = self.turn_on_totp_via_web()
        nonce, _ = self.email_half_login(OWNER)
        for _ in range(5):
            self.client.post("/app/login", data={"nonce": nonce, "action": "totp", "code": "000000"})
        r = self.client.post("/app/login", data={"nonce": nonce, "action": "totp", "code": totp_at(secret, step + 1)},
                             follow_redirects=False)
        self.assertNotEqual(r.status_code, 303)
        self.assertIn("expired", r.text)

    # ---------- OAuth sign-in page ----------
    def test_agent_sign_in_respects_second_factor_and_passkeys(self):
        factors = self.app.state.factors
        secret = factors.start_totp(self.alice_id)
        step = int(time.time() // TOTP_STEP)
        self.assertTrue(factors.confirm_totp(self.alice_id, totp_at(secret, step)))

        client_id, verifier, req = self.start()
        self.client.post("/login", data={"req": req, "email": ALICE, "action": "send"})
        code = [c for to, c in self.mailer.sent if to == ALICE][-1]
        r = self.client.post("/login", data={"req": req, "email": ALICE, "code": code, "action": "verify"},
                             follow_redirects=False)
        self.assertIn("One more step", r.text)
        self.assertIsNone(self.app.state.provider.verified_account(req))
        self.assertEqual(self.client.get("/consent", params={"req": req}).status_code, 400)
        r = self.client.post("/login", data={"req": req, "action": "totp", "code": totp_at(secret, step + 1)},
                             follow_redirects=False)
        self.assertEqual(r.headers["location"], "/consent?req=" + req)
        r = self.consent(req)
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        tok = self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                               "redirect_uri": "http://127.0.0.1:9999/cb", "client_id": client_id,
                                               "code_verifier": verifier}).json()
        self.assertTrue(self.call(tok["access_token"], "memory_scopes"))

        # passkey sign-in goes straight to the consent page in one step
        key = SoftPasskey("http://localhost")
        account = self.db.account(self.alice_id)
        opts = factors.registration_options("t", account)
        factors.register("t", account, key.create(opts), "Phone")
        _, _, req = self.start()
        opts = self.jpost("/login/passkey/options", {"req": req, "mode": "discoverable"}).json()
        r = self.jpost("/login/passkey/verify", {"req": req, "credential": key.get(opts)})
        self.assertEqual(r.json(), {"redirect": "/consent?req=" + req})
        self.assertEqual(self.client.get("/consent", params={"req": req}).status_code, 200)

    # ---------- two-factor and security settings ----------
    def test_reauth_uses_the_strong_factor_not_email(self):
        self.web_login(OWNER)
        secret, step = self.turn_on_totp_via_web()
        self.db.q("UPDATE web_sessions SET reauth_at=0")
        sent = len(self.mailer.sent)
        r = self.post("/app/reauth", {"action": "send", "next": "/app/admin"}, page="/app/reauth")
        self.assertEqual(len(self.mailer.sent), sent)
        self.assertIn("did not work", r.text)
        r = self.post("/app/reauth", {"action": "totp", "code": totp_at(secret, step + 1), "next": "/app/admin"},
                      page="/app/reauth")
        self.assertEqual(r.headers["location"], "/app/admin?msg=reauthed")

    def test_security_changes_need_a_recent_check(self):
        self.web_login(OWNER)
        self.db.q("UPDATE web_sessions SET reauth_at=0")
        r = self.post("/app/security/totp", {"action": "start"}, page="/app/security")
        self.assertEqual(r.headers["location"], "/app/reauth?next=/app/security")
        token = self.csrf("/app/security")
        self.assertEqual(self.jpost("/app/security/passkey/options", {"csrf": token}).status_code, 403)
        self.assertEqual(self.jpost("/app/security/passkey/options", {"csrf": "nope"}).status_code, 403)

    def test_removing_the_last_factor_warns(self):
        self.web_login(OWNER)
        self.add_passkey_via_web()
        pid = self.app.state.factors.passkeys(self.owner_id)[0]["id"]
        r = self.post("/app/security/passkeys/%d" % pid, {"action": "remove"}, page="/app/security")
        self.assertIn("last second factor", r.text)
        r = self.post("/app/security/passkeys/%d" % pid, {"action": "remove", "confirm": "1"}, page="/app/security")
        self.assertEqual(r.status_code, 303)
        self.assertFalse(self.app.state.factors.has_strong(self.owner_id))

    def test_scripts_only_where_passkeys_need_them(self):
        self.web_login(OWNER)
        for path in ("/app", "/app/records", "/app/scopes", "/app/agents", "/app/admin"):
            self.assertNotIn("script", self.client.get(path).headers["content-security-policy"], path)
        for path in ("/app/security", "/app/reauth"):
            self.assertIn("script-src 'self'", self.client.get(path).headers["content-security-policy"], path)
        self.client.cookies.clear()
        self.assertIn("script-src 'self'", self.client.get("/app/login").headers["content-security-policy"])
        _, _, req = self.start()
        self.assertIn("script-src 'self'", self.client.get("/login", params={"req": req}).headers["content-security-policy"])
        js = self.client.get("/app/static/webauthn.js")
        self.assertEqual(js.headers["content-type"].split(";")[0], "text/javascript")

    # ---------- instance policy: a strong factor for everyone ----------
    def test_with_the_policy_an_account_without_a_strong_factor_can_only_add_one(self):
        self.app.state.flow.require_strong = True
        self.web_login(ALICE)
        r = self.client.get("/app", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/app/security?msg=enrol")
        self.assertIn("requires a passkey", self.client.get(r.headers["location"]).text)
        r = self.post("/app/scopes/team-shared/settings", {"title": "x"}, page="/app/security")
        self.assertEqual(r.status_code, 403)
        self.add_passkey_via_web()
        self.assertEqual(self.client.get("/app", follow_redirects=False).status_code, 200)

    def test_with_the_policy_agents_are_not_approved_before_enrolment(self):
        self.app.state.flow.require_strong = True
        client_id, _, req = self.start()
        r = self.client.get(self.verify(req, ALICE))
        self.assertEqual(r.status_code, 403)
        self.assertIn("requires a passkey", r.text)
        self.assertEqual(self.consent(req).status_code, 403)
        self.assertIsNone(self.db.agent_for_client(self.alice_id, client_id))

    def test_without_the_policy_email_codes_are_enough(self):
        self.web_login(ALICE)
        self.assertEqual(self.client.get("/app", follow_redirects=False).status_code, 200)

    def test_cli_reset_auth_restores_email_login(self):
        self.web_login(OWNER)
        self.turn_on_totp_via_web()
        os.environ["KHALA_DB"] = str(self.db.path)
        self.addCleanup(os.environ.pop, "KHALA_DB", None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["reset-auth", OWNER])
        self.assertIn("cleared", out.getvalue())
        self.assertFalse(self.app.state.factors.has_strong(self.owner_id))
        _, r = self.email_half_login(OWNER)
        self.assertEqual(r.status_code, 303)


if __name__ == "__main__":
    unittest.main()

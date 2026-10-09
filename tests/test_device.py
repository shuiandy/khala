"""Device authorization (RFC 8628): a command line starts, the person approves on the web, the next poll gets a
bearer token once. Pending, slow_down, denied and expired codes answer the way RFC 8628 says."""
import time
import unittest

from base import ALICE, Base



class DeviceTests(Base):
    def start(self, name="khala connect (cursor)"):
        r = self.client.post("/device/code", data={"client_name": name})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def poll(self, device_code):
        self.db.q("UPDATE device_codes SET last_poll=NULL")          # tests do not wait out the interval
        return self.client.post("/device/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": device_code})

    def approve(self, user_code, **fields):
        data = dict({"code": user_code, "action": "approve", "access": "all", "days": "30"}, **fields)
        return self.post("/app/device", data, page="/app/device?code=" + user_code)

    def test_approve_then_the_next_poll_gets_a_token_once(self):
        started = self.start()
        self.assertTrue(started["verification_uri"].endswith("/app/device"))
        self.assertEqual(self.poll(started["device_code"]).json()["error"], "authorization_pending")
        self.web_login(ALICE)
        page = self.client.get("/app/device", params={"code": started["user_code"]}).text
        self.assertIn("khala connect (cursor)", page)
        r = self.approve(started["user_code"].lower().replace("-", ""), name="Cursor on my laptop")
        self.assertEqual(r.headers["location"], "/app/agents?msg=device-approved")

        got = self.poll(started["device_code"])
        self.assertEqual(got.status_code, 200, got.text)
        token = got.json()["access_token"]
        self.assertTrue(token.startswith("mem_"))
        self.assertEqual(got.json()["expires_in"], 30 * 86400)
        self.assertEqual(self.poll(started["device_code"]).json()["error"], "expired_token")    # only once
        agent = self.db.one("SELECT * FROM agents WHERE name='Cursor on my laptop'")
        self.assertEqual((agent["account_id"], agent["kind"]), (self.alice_id, "device"))
        self.assertTrue(self.call(token, "memory_scopes"))
        self.assertIsNone(self.db.one("SELECT * FROM device_codes"))

    def test_a_limited_approval_limits_the_token(self):
        started = self.start()
        self.web_login(ALICE)
        self.approve(started["user_code"], access="some", **{"scope:team-shared": "r"})
        token = self.poll(started["device_code"]).json()["access_token"]
        self.assertEqual([s["scope"] for s in self.call(token, "memory_scopes")], ["team-shared"])

    def test_deny_stops_the_poller(self):
        started = self.start()
        self.web_login(ALICE)
        self.post("/app/device", {"code": started["user_code"], "action": "deny"}, page="/app/device")
        self.assertEqual(self.poll(started["device_code"]).json()["error"], "access_denied")
        self.assertEqual(self.poll(started["device_code"]).json()["error"], "expired_token")

    def test_polling_too_fast_slows_down(self):
        started = self.start()
        first = self.client.post("/device/token", data={"device_code": started["device_code"]}).json()
        second = self.client.post("/device/token", data={"device_code": started["device_code"]}).json()
        self.assertEqual((first["error"], second["error"]), ("authorization_pending", "slow_down"))
        self.assertEqual(self.db.one("SELECT interval FROM device_codes")["interval"], 10)

    def test_approving_needs_a_fresh_identity_check(self):
        started = self.start()
        self.web_login(ALICE)
        self.db.q("UPDATE web_sessions SET reauth_at=0")
        r = self.approve(started["user_code"])
        self.assertTrue(r.headers["location"].startswith("/app/reauth"))
        self.assertEqual(self.poll(started["device_code"]).json()["error"], "authorization_pending")

    def test_expired_and_unknown_codes(self):
        started = self.start()
        self.db.q("UPDATE device_codes SET expires=?", time.time() - 1)
        self.web_login(ALICE)
        self.assertIn("unknown or expired", self.client.get("/app/device", params={"code": started["user_code"]}).text)
        self.assertEqual(self.poll(started["device_code"]).json()["error"], "expired_token")
        bad = self.client.post("/device/token", data={"grant_type": "password", "device_code": "x"})
        self.assertEqual(bad.json()["error"], "unsupported_grant_type")

    def test_one_address_cannot_start_endless_codes(self):
        for _ in range(20):
            self.start()
        r = self.client.post("/device/code", data={"client_name": "x"})
        self.assertEqual(r.status_code, 429)

    def test_the_approval_form_refuses_other_sites(self):
        started = self.start()
        self.web_login(ALICE)
        r = self.client.post("/app/device", data={"code": started["user_code"], "action": "approve", "access": "all",
                                                  "days": "30", "csrf": self.csrf("/app/device")},
                             headers={"Origin": "https://evil.example"}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.poll(started["device_code"]).json()["error"], "authorization_pending")


if __name__ == "__main__":
    unittest.main()

"""End to end: OAuth plus MCP tool calls, all against a temporary repository and temporary database."""
import base64
import hashlib
import secrets
import unittest
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from base import OWNER, STRANGER, ALICE, Base, git, rec
from khala.db import digest


class ServerTests(Base):
    def test_unauthenticated_mcp_is_rejected(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             headers={"Accept": "application/json, text/event-stream"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("resource_metadata", r.headers.get("www-authenticate", ""))

    def test_root_redirect_leaves_oauth_discovery_alone(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/app"))
        meta = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(meta.status_code, 200)
        self.assertIn("authorization_endpoint", meta.json())
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource/mcp").status_code, 200)

    def test_registration_rejects_non_https_redirects(self):
        self.assertEqual(self.register("http://evil.example.com/cb").status_code, 400)
        self.assertEqual(self.register("https://claude.ai/api/mcp/auth_callback").status_code, 201)

    def test_cloud_bot_offline_access_flow(self):
        """Cloud bots: they register with offline_access and may request only offline_access when authorizing."""
        r = self.client.post("/register", json={"redirect_uris": ["https://bot.example.com/cb"],
                                                "token_endpoint_auth_method": "none",
                                                "grant_types": ["authorization_code", "refresh_token"],
                                                "response_types": ["code"], "scope": "memory offline_access",
                                                "client_name": "Cloud bot"})
        self.assertEqual(r.status_code, 201, r.text)
        client_id = r.json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        r = self.client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": "https://bot.example.com/cb",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s", "scope": "offline_access"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 302, r.text)
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        consent_page = self.client.get(self.verify(req, ALICE)).text
        self.assertIn('value="bot" checked', consent_page)  # cloud, offline only: default bot, nothing granted
        r = self.consent(req, access="some", scopes={"team-shared": "rw"}, kind="bot", name="Helper")
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        tok = self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                               "redirect_uri": "https://bot.example.com/cb", "client_id": client_id,
                                               "code_verifier": verifier}).json()
        self.assertIn("memory", tok["scope"].split())
        self.assertTrue(tok["refresh_token"])
        self.assertEqual([s["scope"] for s in self.call(tok["access_token"], "memory_scopes")], ["team-shared"])
        refreshed = self.client.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                     "refresh_token": tok["refresh_token"]}).json()
        self.assertIn("memory", refreshed["scope"].split())

    def test_only_the_verifying_browser_can_consent(self):
        """Client sees req: after the user verifies the email code in a browser, it can't submit access=all first."""
        client_id, verifier, req = self.start()
        self.verify(req, ALICE)                               # the user's browser (with cookie) completes verification
        attacker = TestClient(self.app, base_url="http://localhost")
        r = attacker.post("/consent", data={"req": req, "access": "all", "name": "x", "kind": "device",
                                            "action": "allow"}, headers={"Origin": "http://localhost"},
                          follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(attacker.get("/consent", params={"req": req}).status_code, 400)
        # a cross-site auto-submit from the user's browser fails too
        r = self.client.post("/consent", data={"req": req, "access": "all", "action": "allow"},
                             headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        # the user can still finish it, and the binding cookie is cleared afterwards
        r = self.consent(req, access="some", scopes={"team-shared": "r"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("ms_oauth_", r.headers.get("set-cookie", ""))
        agent = self.db.agent_for_client(self.alice_id, client_id)
        self.assertEqual(agent["ceiling"], '{"team-shared": "r"}')

    def test_unknown_email_gets_same_page_and_no_code(self):
        _, _, req = self.start()
        known = self.client.post("/login", data={"req": req, "email": ALICE, "action": "send"}).text
        unknown = self.client.post("/login", data={"req": req, "email": STRANGER, "action": "send"}).text
        self.assertEqual(known.replace(ALICE, "X"), unknown.replace(STRANGER, "X"))
        self.assertFalse(any(to == STRANGER for to, _ in self.mailer.sent))

    def test_wrong_codes_lock_out_even_the_right_one(self):
        _, _, req = self.start()
        self.client.post("/login", data={"req": req, "email": ALICE, "action": "send"})
        code = self.mailer.sent[-1][1]
        for _ in range(5):
            self.client.post("/login", data={"req": req, "email": ALICE, "code": "000000" if code != "000000" else "111111",
                                             "action": "verify"})
        r = self.client.post("/login", data={"req": req, "email": ALICE, "code": code, "action": "verify"},
                             follow_redirects=False)
        self.assertNotEqual(r.status_code, 302)

    def test_send_rate_limit(self):
        _, _, req = self.start()
        for _ in range(5):
            self.client.post("/login", data={"req": req, "email": ALICE, "action": "send"})
        r = self.client.post("/login", data={"req": req, "email": ALICE, "action": "send"})
        self.assertIn("Too many", r.text)
        self.assertEqual(len(self.mailer.sent), 5)

    def test_pkce_is_enforced(self):
        client_id, verifier, req = self.start()
        self.verify(req, ALICE)
        r = self.consent(req)
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        bad = self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                               "redirect_uri": "http://127.0.0.1:9999/cb", "client_id": client_id,
                                               "code_verifier": secrets.token_urlsafe(48)})
        self.assertEqual(bad.status_code, 400)

    def test_shared_user_sees_only_granted_scope(self):
        tok, _ = self.login(ALICE)
        t = tok["access_token"]
        self.assertEqual([s["scope"] for s in self.call(t, "memory_scopes")],
                         ["alice", "alice-global", "team-shared"])
        self.assertEqual(self.call(t, "memory_index", scope="personal"), [])
        self.assertEqual(self.call(t, "memory_index", scope="team"), [])
        names = [r["name"] for r in self.call(t, "memory_index", scope="team-shared")]
        self.assertEqual(names, ["project_audit.md", "project_pinned.md"])
        self.assertIn("error", self.call(t, "memory_read", name="project_personal_thing.md"))
        self.assertIn("error", self.call(t, "memory_read", name="PROTOCOL.md"))
        self.assertEqual(self.call(t, "memory_search", query="SECRET_PERSONAL_DETAIL"), [])
        self.assertEqual(len(self.call(t, "memory_search", query="hire dates")), 1)

    def test_shared_user_write_rules(self):
        t = self.login(ALICE)[0]["access_token"]
        r = self.call(t, "memory_write", name="project_new_note.md", content=rec("personal"))
        self.assertIn("cannot write to scope", r["error"])
        # hitting a record Alice can't see: only say the name is unavailable
        r = self.call(t, "memory_write", name="project_personal_thing.md", content=rec("team-shared"))
        self.assertIn("name is unavailable", r["error"])
        self.assertNotIn("exists", r["error"])
        r = self.call(t, "memory_write", name="PROTOCOL.md", content=rec("team-shared"))
        self.assertIn("error", r)
        pinned = self.call(t, "memory_read", name="project_pinned.md")
        r = self.call(t, "memory_write", name="project_pinned.md", content=rec("team-shared", body="x"),
                      expected_sha=pinned["sha"])
        self.assertIn("pinned", r["error"])
        r = self.call(t, "memory_write", name="project_audit.md", content=rec("team-shared", body="token ghp_" + "A" * 36))
        self.assertIn("error", r)

    def test_update_requires_current_sha_and_records_author(self):
        t = self.login(ALICE)[0]["access_token"]
        cur = self.call(t, "memory_read", name="project_audit.md")
        self.assertIn("changed since you read it", self.call(t, "memory_write", name="project_audit.md",
                                                               content=rec("team-shared", body="v2"))["error"])
        ok = self.call(t, "memory_write", name="project_audit.md", content=rec("team-shared", body="v2"),
                       expected_sha=cur["sha"])
        self.assertTrue(ok["changed"])
        stale = self.call(t, "memory_write", name="project_audit.md", content=rec("team-shared", body="v3"),
                          expected_sha=cur["sha"])
        self.assertIn("changed since you read it", stale["error"])
        self.assertEqual(git(self.hub, "log", "-1", "--format=%an <%ae>|%s|%(trailers:key=Agent,valueonly)"),
                         "Alice <alice@example.com>|Update memory: project_audit|Test agent")
        self.assertIn("v2", git(self.hub, "show", "main:project_audit.md"))

    def test_owner_global_rules(self):
        t = self.login(OWNER)[0]["access_token"]
        scopes = {s["scope"] for s in self.call(t, "memory_scopes")}
        self.assertTrue({"personal", "team", "team-shared", "global"} <= scopes)
        r = self.call(t, "memory_write", name="feedback_new_global.md", content=rec("global"))
        self.assertTrue(r["notes"])
        body = git(self.hub, "show", "main:feedback_new_global.md")
        self.assertIn("status: proposed", body)
        self.assertIn("proposed_at:", body)
        cur = self.call(t, "memory_read", name="feedback_new_global.md")
        r = self.call(t, "memory_write", name="feedback_new_global.md", content=rec("global"), expected_sha=cur["sha"])
        self.assertIn("error", r)

    def test_concurrent_push_and_write_do_not_clobber(self):
        t = self.login(ALICE)[0]["access_token"]
        cur = self.call(t, "memory_read", name="project_audit.md")
        # simulate a mirror pushing a version first
        git(self.work, "pull", "-q", str(self.hub), "main")
        (self.work / "project_audit.md").write_text(rec("team-shared", body="from mac"))
        git(self.work, "commit", "-qam", "mac edit")
        git(self.work, "push", "-q", str(self.hub), "HEAD:main")
        r = self.call(t, "memory_write", name="project_audit.md", content=rec("team-shared", body="from alice"),
                      expected_sha=cur["sha"])
        self.assertIn("changed since you read it", r["error"])
        self.assertIn("from mac", git(self.hub, "show", "main:project_audit.md"))

    def test_removed_user_loses_access_and_refresh_rotates(self):
        tok, client_id = self.login(ALICE)
        r = self.client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                             "client_id": client_id})
        self.assertEqual(r.status_code, 200)
        # past the grace period for retries (tests/test_tokens.py covers the grace itself)
        self.db.q("UPDATE tokens SET rotated_at=0 WHERE token_hash=?", digest(tok["refresh_token"]))
        again = self.client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                                 "client_id": client_id})
        self.assertEqual(again.status_code, 400)
        # the reuse above revoked that whole chain, so sign in again
        tok, client_id = self.login(ALICE)
        new = tok["access_token"]
        self.assertTrue(self.call(new, "memory_scopes"))
        self.db.set_account_status(self.alice_id, "disabled")
        r = self.client.post("/mcp", headers={"Authorization": "Bearer " + new, "Accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()

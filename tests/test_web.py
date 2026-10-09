"""Web admin: sign-in, sessions, CSRF, access checks, invite/grant/revoke, 2FA, agent management, disabled accounts."""
import contextlib
import io
import os
import re
import threading
import time
import unittest

from base import ORIGIN, OWNER, STRANGER, ALICE, Base, git, rec
from khala import cli

NEWBIE = "newbie@example.com"


class WebTests(Base):
    # ---------- sign-in and sessions ----------
    def test_pages_need_a_session(self):
        r = self.client.get("/app/scopes", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/app/login?next=/app/scopes")
        self.assertEqual(self.client.post("/app/scopes", data={"id": "x"}).status_code, 401)

    def test_login_sets_a_locked_down_session_cookie(self):
        r = self.web_login(OWNER)
        cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("ms_session=")][0].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        page = self.client.get("/app")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("script", page.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(page.headers["x-frame-options"], "DENY")

    def test_login_needs_the_browser_that_started_it(self):
        r = self.client.get("/app/login")
        nonce = re.search(r'name="nonce" value="([^"]+)"', r.text).group(1)
        self.client.cookies.clear()
        r = self.client.post("/app/login", data={"nonce": nonce, "email": OWNER, "action": "send"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertFalse(self.mailer.sent)

    def test_unknown_email_gets_same_page_and_no_code(self):
        r = self.client.get("/app/login")
        nonce = re.search(r'name="nonce" value="([^"]+)"', r.text).group(1)
        a = self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "email": ALICE, "action": "send"}).text
        b = self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "email": STRANGER, "action": "send"}).text
        self.assertEqual(a.replace(ALICE, "X"), b.replace(STRANGER, "X"))
        self.assertFalse(any(to == STRANGER for to, _ in self.mailer.sent))

    def test_every_page_renders(self):
        self.web_login(OWNER)
        for path in ("/app", "/app/scopes", "/app/scopes/team", "/app/records", "/app/records?due=1&q=audit",
                     "/app/records/project_audit.md", "/app/agents", "/app/audit", "/app/audit?all=1", "/app/admin",
                     "/app/audit.csv"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_csrf_and_origin_are_required(self):
        self.web_login(OWNER)
        token = self.csrf()
        self.assertEqual(self.post("/app/scopes", {"id": "a1", "csrf": "nope"}).status_code, 403)
        self.assertEqual(self.post("/app/scopes", {"id": "a2", "csrf": token}, headers={}).status_code, 403)
        self.assertEqual(self.post("/app/scopes", {"id": "a3", "csrf": token},
                                   headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.post("/app/scopes", {"id": "a4", "csrf": token}).status_code, 303)
        self.assertIsNone(self.db.scope("a1"))
        self.assertEqual(self.db.scope("a4")["owner_id"], self.owner_id)

    def test_browsers_that_hide_the_origin_still_work(self):
        """Browsers with no-referrer: same-site forms send Origin: null and no Referer; rely on Sec-Fetch-Site."""
        self.web_login(OWNER)
        token = self.csrf()
        hidden = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.post("/app/scopes", {"id": "b1", "csrf": token}, headers=hidden).status_code, 303)
        cross = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}
        r = self.post("/app/scopes", {"id": "b2", "csrf": token}, headers=cross)
        self.assertEqual(r.status_code, 403)
        self.assertIn("Request refused", r.text)
        self.assertNotIn(">Sign in<", r.text)                     # session still valid: shouldn't look signed out
        self.assertIn(OWNER, r.text)
        lying = {"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.post("/app/scopes", {"id": "b3", "csrf": token}, headers=lying).status_code, 403)
        self.assertIsNone(self.db.scope("b2"))
        row = self.db.one("SELECT detail FROM audit_events WHERE action='web.request_refused' ORDER BY id LIMIT 1")
        self.assertIn('"fetch_site": "cross-site"', row["detail"])

    # ---------- records are read-only ----------
    def test_record_body_is_escaped_and_history_shows_the_agent(self):
        self.push("project_xss.md", rec("personal", body='<script>alert(1)</script> <img src=x onerror=alert(2)>'))
        t = self.login(OWNER, name="Mac · Claude Code")[0]["access_token"]
        cur = self.call(t, "memory_read", name="project_xss.md")
        self.call(t, "memory_write", name="project_xss.md", content=rec("personal", body="safe now"),
                  expected_sha=cur["sha"])
        self.web_login(OWNER)
        page = self.client.get("/app/records/project_xss.md").text
        self.assertIn("Mac · Claude Code", page)
        sha = re.search(r'/commits/([0-9a-f]{40})"', page).group(1)
        diff = self.client.get("/app/records/project_xss.md/commits/" + sha).text
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", diff)
        self.assertNotIn("<script>alert", diff)
        self.assertNotIn("<img src=x", diff)

    def test_members_cannot_see_what_is_not_shared(self):
        self.web_login(ALICE)
        for path in ("/app/scopes/team", "/app/records/project_private_team.md", "/app/admin",
                     "/app/scopes/global"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        records = self.client.get("/app/records").text
        self.assertIn("project_audit.md", records)
        self.assertNotIn("project_personal_thing", records)
        # an editor isn't a maintainer: can't see member management or add people
        self.assertNotIn("Add someone", self.client.get("/app/scopes/team-shared").text)
        r = self.post("/app/scopes/team-shared/members", {"email": STRANGER, "role": "viewer", "confirm": "1"})
        self.assertEqual(r.status_code, 404)

    # ---------- sharing: phase 1 acceptance flow ----------
    def test_invite_accept_and_revoke(self):
        """Invite; they sign in with the invited email and see only this scope; once revoked, the next request fails."""
        self.push("project_sensitive.md", rec("team", body="salary").replace(
            "  status: active\n", "  status: active\n  sensitivity: work\n"))
        self.web_login(OWNER)
        r = self.post("/app/scopes/team/members", {"email": NEWBIE, "role": "viewer"}, page="/app/scopes/team")
        self.assertEqual(r.status_code, 200)
        self.assertIn("1 record in this scope is marked sensitive", r.text)
        self.assertFalse(self.mailer.messages)
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
        r = self.post("/app/scopes/team/members", {"email": NEWBIE, "role": "viewer", "confirm": "1", "csrf": csrf})
        self.assertEqual(r.headers["location"], "/app/scopes/team?msg=invite-sent")
        to, subject, body = self.mailer.messages[-1]
        self.assertEqual(to, NEWBIE)
        link = re.search(r"http://localhost(/app/invite/\S+)", body).group(1)

        self.client.cookies.clear()
        self.assertIn("Sign in to accept", self.client.get(link).text)
        self.web_login(NEWBIE)
        newbie = self.db.account_by_email(NEWBIE)
        self.assertEqual(self.db.grants_for_account(newbie["id"]), {"team": "viewer"})
        self.assertEqual(self.client.get(link, follow_redirects=False).status_code, 303)  # already used: redirects home

        t = self.login(NEWBIE)[0]["access_token"]
        self.assertEqual([s["scope"] for s in self.call(t, "memory_scopes")], ["newbie", "newbie-global", "team"])
        self.assertNotIn("error", self.call(t, "memory_read", name="project_private_team.md"))

        self.web_login(OWNER)
        page = self.client.get("/app/scopes/team").text
        self.assertIn(NEWBIE, page)
        r = self.post("/app/scopes/team/members/%d" % newbie["id"], {"action": "remove"}, page="/app/scopes/team")
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
        r = self.post("/app/scopes/team/members/%d" % newbie["id"], {"action": "remove", "confirm": "1", "csrf": csrf})
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("team", [s["scope"] for s in self.call(t, "memory_scopes")])
        self.assertIn("error", self.call(t, "memory_read", name="project_private_team.md"))
        audit = self.client.get("/app/audit").text
        for action in ("invite.sent", "grant.set", "grant.removed", "record.read"):
            self.assertIn(action, audit)

    def test_slow_email_does_not_block_other_requests(self):
        """A slow notification email shouldn't hold up other requests (such as /health)."""
        self.web_login(OWNER)
        slow = threading.Event()
        def send_text(*args):
            slow.set()
            time.sleep(0.6)
        self.mailer.send_text = send_text
        timings = {}
        def share():
            self.post("/app/scopes/team/members", {"email": ALICE, "role": "viewer", "confirm": "1"},
                      page="/app/scopes/team")
        t = threading.Thread(target=share)
        t.start()
        slow.wait(5)
        start = time.time()
        self.assertEqual(self.client.get("/health").status_code, 200)
        timings["health"] = time.time() - start
        t.join()
        self.assertLess(timings["health"], 0.3)

    def test_sharing_with_an_existing_account_is_immediate(self):
        self.web_login(OWNER)
        r = self.post("/app/scopes/team/members", {"email": ALICE, "role": "viewer", "confirm": "1"},
                      page="/app/scopes/team")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.grants_for_account(self.alice_id)["team"], "viewer")
        self.assertEqual(self.mailer.messages[-1][0], ALICE)

    def test_auto_load_scope_cannot_be_shared(self):
        self.web_login(OWNER)
        r = self.post("/app/scopes/global/members", {"email": ALICE, "role": "viewer", "confirm": "1"},
                      page="/app/scopes/global")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("global", self.db.grants_for_account(self.alice_id))

    def test_the_owner_decides_which_scopes_are_auto_loaded(self):
        self.web_login(OWNER)
        r = self.post("/app/scopes/personal/auto-load", {}, page="/app/scopes/personal")
        self.assertIn("Load in every session", r.text)                # asks first
        self.assertFalse(self.db.scope("personal")["auto_load"])
        r = self.post("/app/scopes/personal/auto-load", {"confirm": "1"}, page="/app/scopes/personal")
        self.assertEqual(r.headers["location"], "/app/scopes/personal?msg=auto-load-on")
        s = self.db.scope("personal")
        self.assertEqual((s["auto_load"], s["shareable"]), (1, 0))
        t = self.login(OWNER)[0]["access_token"]
        out = self.call(t, "memory_write", name="feedback_new.md", content=rec("personal", body="always X"))
        self.assertTrue(any("auto-loaded" in n for n in out["notes"]))
        self.assertIn("status: proposed", git(self.hub, "show", "main:feedback_new.md"))
        self.post("/app/scopes/personal/auto-load", {"confirm": "1"}, page="/app/scopes/personal")
        s = self.db.scope("personal")
        self.assertEqual((s["auto_load"], s["shareable"]), (0, 1))
        self.assertEqual(self.db.q("SELECT detail FROM audit_events WHERE action='scope.auto_load' ORDER BY id")[0][0],
                         '{"on": true}')

    def test_a_shared_scope_cannot_become_auto_loaded(self):
        self.web_login(OWNER)
        r = self.post("/app/scopes/team-shared/auto-load", {"confirm": "1"}, page="/app/scopes/team-shared")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.db.scope("team-shared")["auto_load"])
        self.assertFalse(self.db.set_auto_load("team-shared", True))           # the database check holds on its own

    def test_the_command_line_switches_auto_loading(self):
        os.environ["KHALA_DB"] = str(self.db.path)
        self.addCleanup(os.environ.pop, "KHALA_DB", None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["auto-load", "personal", "on"])
        self.assertIn("auto-load on", out.getvalue())
        self.assertTrue(self.db.scope("personal")["auto_load"])
        with self.assertRaises(SystemExit) as cm:
            cli.main(["auto-load", "team-shared", "on"])
        self.assertIn("members", str(cm.exception.code))
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["auto-load", "personal", "off"])
        self.assertFalse(self.db.scope("personal")["auto_load"])

    def test_only_the_owner_switches_auto_loading(self):
        self.db.set_grant("team-shared", self.alice_id, "maintainer", self.owner_id)
        self.web_login(ALICE)
        r = self.post("/app/scopes/team-shared/auto-load", {"confirm": "1"}, page="/app/scopes/team-shared")
        self.assertEqual(r.status_code, 404)

    def test_switching_auto_loading_needs_a_fresh_code(self):
        self.web_login(OWNER)
        self.db.q("UPDATE web_sessions SET reauth_at=0")
        r = self.post("/app/scopes/personal/auto-load", {"confirm": "1"}, page="/app/scopes/personal")
        self.assertTrue(r.headers["location"].startswith("/app/reauth"))
        self.assertFalse(self.db.scope("personal")["auto_load"])

    def test_appointing_a_maintainer_needs_a_fresh_code(self):
        self.web_login(OWNER)
        self.db.q("UPDATE web_sessions SET reauth_at=0")
        data = {"email": ALICE, "role": "maintainer", "confirm": "1"}
        r = self.post("/app/scopes/team/members", data, page="/app/scopes/team")
        self.assertEqual(r.headers["location"], "/app/reauth?next=/app/scopes/team")
        self.assertNotIn("team", self.db.grants_for_account(self.alice_id))
        self.post("/app/reauth", {"action": "send", "next": "/app/scopes/team"}, page="/app/reauth")
        code = [c for to, c in self.mailer.sent if to == OWNER][-1]
        r = self.post("/app/reauth", {"action": "verify", "code": code, "next": "/app/scopes/team"},
                      page="/app/reauth")
        self.assertEqual(r.headers["location"], "/app/scopes/team?msg=reauthed")
        r = self.post("/app/scopes/team/members", data, page="/app/scopes/team")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.grants_for_account(self.alice_id)["team"], "maintainer")

    def test_reauth_next_stays_on_site(self):
        self.web_login(OWNER)
        page = self.client.get("/app/reauth", params={"next": "https://evil.example/x"}).text
        self.assertIn('name="next" value="/app"', page)

    # ---------- agents and accounts ----------
    def test_agent_ceiling_and_revoke_from_the_web(self):
        tok, client_id = self.login(ALICE)
        t = tok["access_token"]
        agent = self.db.agent_for_client(self.alice_id, client_id)
        self.web_login(ALICE)
        self.assertIn("Test agent", self.client.get("/app/agents").text)
        r = self.post("/app/agents/%d" % agent["id"], {"name": "Helper", "kind": "bot", "access": "some",
                                                       "scope:team-shared": "r"}, page="/app/agents")
        self.assertEqual(r.status_code, 303)
        self.assertEqual([(s["scope"], s["mode"]) for s in self.call(t, "memory_scopes")], [("team-shared", "r")])
        r = self.post("/app/agents/%d/revoke" % agent["id"], {"confirm": "1"}, page="/app/agents")
        self.assertEqual(r.status_code, 303)
        r = self.client.post("/mcp", headers={"Authorization": "Bearer " + t, "Accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 401)

    def test_cannot_touch_someone_elses_agent(self):
        _, client_id = self.login(OWNER)
        agent = self.db.agent_for_client(self.owner_id, client_id)
        self.web_login(ALICE)
        r = self.post("/app/agents/%d/revoke" % agent["id"], {"confirm": "1"}, page="/app/agents")
        self.assertEqual(r.status_code, 404)
        self.assertIsNone(self.db.agent(agent["id"])["revoked_at"])

    def test_admin_disables_an_account(self):
        t = self.login(ALICE)[0]["access_token"]
        self.web_login(OWNER)
        r = self.post("/app/admin/accounts/%d" % self.alice_id, {"action": "disable", "confirm": "1"}, page="/app/admin")
        self.assertEqual(r.status_code, 303)
        r = self.client.post("/mcp", headers={"Authorization": "Bearer " + t, "Accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 401)
        self.client.cookies.clear()
        r = self.client.get("/app/login")
        nonce = re.search(r'name="nonce" value="([^"]+)"', r.text).group(1)
        before = len(self.mailer.sent)
        self.client.post("/app/login", data={"nonce": nonce, "email": ALICE, "action": "send"})
        self.assertEqual(len(self.mailer.sent), before)

    def test_admin_page_is_admin_only_and_cannot_demote_self(self):
        self.web_login(ALICE)
        self.assertEqual(self.client.get("/app/admin").status_code, 404)
        self.web_login(OWNER)
        r = self.post("/app/admin/accounts/%d" % self.owner_id, {"action": "disable", "confirm": "1"}, page="/app/admin")
        self.assertEqual(r.status_code, 400)

    def test_audit_is_scoped_to_what_you_own(self):
        t = self.login(ALICE)[0]["access_token"]
        self.call(t, "memory_read", name="project_audit.md")
        self.web_login(ALICE)
        self.assertNotIn("instance.", self.client.get("/app/audit?days=400").text)
        self.web_login(OWNER)
        page = self.client.get("/app/audit").text
        self.assertIn("record.read", page)
        csv = self.client.get("/app/audit.csv")
        self.assertEqual(csv.headers["content-type"].split(";")[0], "text/csv")
        self.assertIn("record.read", csv.text)


if __name__ == "__main__":
    unittest.main()

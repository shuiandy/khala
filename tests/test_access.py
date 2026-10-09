"""Access: the role x action matrix, agent ceilings, revocation, auto-loaded scopes and new scopes."""
import unittest

from base import OWNER, ALICE, Base, git, rec

BOB = "bob@example.com"


class AccessTests(Base):
    def setUp(self):
        super().setUp()
        self.bob_id = self.db.create_account(BOB, "Bob")

    def read_ok(self, token, name, scope):
        return "error" not in self.call(token, "memory_read", name=name) and \
            scope in [s["scope"] for s in self.call(token, "memory_scopes")]

    def write_ok(self, token, name, scope):
        cur = self.call(token, "memory_read", name=name)
        r = self.call(token, "memory_write", name=name, content=rec(scope, body="edit " + str(len(self.mailer.sent))),
                      expected_sha=cur.get("sha"))
        return "error" not in r

    def test_role_matrix(self):
        """Whether each role can read and write a shared scope; the owner can always do both."""
        cases = [(None, False, False), ("viewer", True, False), ("editor", True, True), ("maintainer", True, True)]
        for role, can_read, can_write in cases:
            with self.subTest(role=role):
                if role:
                    self.db.set_grant("team", self.bob_id, role, self.owner_id)
                else:
                    self.db.remove_grant("team", self.bob_id)
                t = self.login(BOB)[0]["access_token"]
                self.assertEqual(self.read_ok(t, "project_private_team.md", "team"), can_read)
                if can_read:
                    self.assertEqual(self.write_ok(t, "project_private_team.md", "team"), can_write)
        t = self.login(OWNER)[0]["access_token"]
        self.assertTrue(self.read_ok(t, "project_private_team.md", "team"))
        self.assertTrue(self.write_ok(t, "project_private_team.md", "team"))

    def test_ceiling_narrows_but_never_widens(self):
        """Agent ceilings only narrow: an editor's read-only agent can't write, nor can a viewer's read-write agent."""
        self.db.set_grant("team", self.bob_id, "viewer", self.owner_id)
        t = self.login(BOB, access="some", scopes={"team": "rw", "team-shared": "rw"})[0]["access_token"]
        self.assertEqual([s["scope"] for s in self.call(t, "memory_scopes")], ["team"])
        self.assertFalse(self.write_ok(t, "project_private_team.md", "team"))

        t = self.login(ALICE, access="some", scopes={"team-shared": "r"})[0]["access_token"]
        scopes = self.call(t, "memory_scopes")
        self.assertEqual([(s["scope"], s["mode"]) for s in scopes], [("team-shared", "r")])
        self.assertFalse(self.write_ok(t, "project_audit.md", "team-shared"))
        # Alice's own personal scope is not visible to this agent either
        r = self.call(t, "memory_write", name="project_mine.md", content=rec("alice"))
        self.assertIn("cannot write", r["error"])

    def test_limited_agent_does_not_see_new_shares(self):
        t_all = self.login(BOB)[0]["access_token"]
        t_some = self.login(BOB, access="some", scopes={"bob": "rw"})[0]["access_token"]
        self.db.set_grant("team", self.bob_id, "viewer", self.owner_id)
        self.assertIn("team", [s["scope"] for s in self.call(t_all, "memory_scopes")])
        self.assertNotIn("team", [s["scope"] for s in self.call(t_some, "memory_scopes")])

    def test_revoked_agent_and_revoked_grant_take_effect_on_next_request(self):
        self.db.set_grant("team", self.bob_id, "editor", self.owner_id)
        tok, client_id = self.login(BOB)
        t = tok["access_token"]
        self.assertTrue(self.read_ok(t, "project_private_team.md", "team"))
        self.db.remove_grant("team", self.bob_id)
        self.assertIn("error", self.call(t, "memory_read", name="project_private_team.md"))
        agent = self.db.agent_for_client(self.bob_id, client_id)
        self.db.revoke_agent(agent["id"])
        r = self.client.post("/mcp", headers={"Authorization": "Bearer " + t, "Accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                             "refresh_token": tok["refresh_token"]})
        self.assertEqual(r.status_code, 400)

    def test_same_client_reuses_its_agent(self):
        _, c1 = self.login(BOB, name="Mac · Claude Code")
        agent = self.db.agent_for_client(self.bob_id, c1)
        # same client authorizes again: consent prefills the old name, and confirming keeps the same agent
        _, verifier, req = self.start(c1)
        page = self.client.get(self.verify(req, BOB)).text
        self.assertIn("already connected", page)
        self.assertIn("Mac · Claude Code", page)
        self.consent(req, name="Mac · Claude Code")
        self.login(BOB)                              # a different client is a different agent
        live = [a for a in self.db.agents_for(self.bob_id) if a["revoked_at"] is None]
        self.assertEqual(len(live), 2)
        self.assertEqual(self.db.agent_for_client(self.bob_id, c1)["id"], agent["id"])

    def test_auto_load_scope_never_shared(self):
        """Even if someone forces a grant into the database, auto-loaded scopes ignore it."""
        self.db.q("INSERT INTO grants(scope_id, account_id, role, created_at) VALUES ('global', ?, 'editor', 0)",
                  self.bob_id)
        t = self.login(BOB)[0]["access_token"]
        self.assertNotIn("global", [s["scope"] for s in self.call(t, "memory_scopes")])
        self.assertIn("error", self.call(t, "memory_read", name="feedback_global_pref.md"))

    def test_new_account_auto_load_scope_records_start_proposed(self):
        t = self.login(BOB)[0]["access_token"]
        scopes = {s["scope"]: s for s in self.call(t, "memory_scopes")}
        self.assertTrue(scopes["bob-global"]["auto_load"])
        r = self.call(t, "memory_write", name="feedback_bob_pref.md", content=rec("bob-global"))
        self.assertTrue(r["notes"])
        self.assertIn("status: proposed", git(self.hub, "show", "main:feedback_bob_pref.md"))

    def test_agents_can_open_new_scopes_only_without_a_ceiling(self):
        t = self.login(BOB)[0]["access_token"]
        r = self.call(t, "memory_write", name="project_side.md", content=rec("bob-side-project"))
        self.assertNotIn("error", r)
        self.assertEqual(self.db.scope("bob-side-project")["owner_id"], self.bob_id)
        limited = self.login(BOB, access="some", scopes={"bob": "rw"})[0]["access_token"]
        r = self.call(limited, "memory_write", name="project_other.md", content=rec("bob-other"))
        self.assertIn("cannot write", r["error"])
        self.assertIsNone(self.db.scope("bob-other"))
        # an existing scope owned by someone else can't be taken over by "creating" it
        r = self.call(t, "memory_write", name="project_steal.md", content=rec("team"))
        self.assertIn("cannot write", r["error"])

    def test_pinned_cannot_be_set_through_the_service(self):
        t = self.login(OWNER)[0]["access_token"]
        r = self.call(t, "memory_write", name="project_pin_me.md", content=rec("personal", pinned=True))
        self.assertIn("pinned", r["error"])

    def test_moving_a_record_needs_both_scopes(self):
        self.db.set_grant("team", self.bob_id, "viewer", self.owner_id)
        self.db.set_grant("team-shared", self.bob_id, "editor", self.owner_id)
        t = self.login(BOB)[0]["access_token"]
        cur = self.call(t, "memory_read", name="project_private_team.md")
        r = self.call(t, "memory_write", name="project_private_team.md", content=rec("team-shared"),
                      expected_sha=cur["sha"])
        self.assertIn("error", r)

    def test_shared_reads_are_audited_for_the_owner(self):
        t = self.login(ALICE)[0]["access_token"]
        self.call(t, "memory_read", name="project_audit.md")
        row = self.db.one("SELECT * FROM audit_events WHERE action='record.read'")
        self.assertEqual((row["account_id"], row["target"]), (self.alice_id, "project_audit.md"))
        self.assertIsNotNone(row["agent_id"])
        self.assertTrue(self.db.one("SELECT 1 FROM audit_events WHERE action='token.issued' AND account_id=?",
                                    self.alice_id))


if __name__ == "__main__":
    unittest.main()

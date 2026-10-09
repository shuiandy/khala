"""Isolation: same names across accounts, private-to-shared moves, proposals citing private notes, auto-loaded moves."""
import unittest

from base import OWNER, ALICE, Base, git, rec

BOB = "bob@example.com"


class IsolationTests(Base):
    def setUp(self):
        super().setUp()
        self.bob_id = self.db.create_account(BOB, "Bob")

    def test_a_reused_file_name_does_not_expose_the_previous_owners_history(self):
        tb = self.login(BOB)[0]["access_token"]
        self.call(tb, "memory_write", name="project_same.md", content=rec("bob", body="BOB PRIVATE PLAN"))
        bob_commit = git(self.hub, "rev-parse", "main")
        self.web_login(BOB)
        self.post("/app/records/project_same.md/delete", {"confirm": "1"}, page="/app/records/project_same.md")
        self.assertNotIn("project_same.md", git(self.hub, "ls-tree", "--name-only", "main"))

        tv = self.login(ALICE)[0]["access_token"]
        self.call(tv, "memory_write", name="project_same.md", content=rec("alice", body="alice's own"))
        hist = self.call(tv, "memory_history", name="project_same.md")
        self.assertEqual([h["by"] for h in hist], ["Alice"])
        self.web_login(ALICE)
        page = self.client.get("/app/records/project_same.md").text
        self.assertNotIn(bob_commit, page)
        self.assertEqual(self.client.get("/app/records/project_same.md/commits/" + bob_commit).status_code, 404)
        r = self.post("/app/records/project_same.md/commits/%s/undo" % bob_commit, {"confirm": "1"},
                      page="/app/records/project_same.md")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("BOB PRIVATE PLAN", self.client.get("/app").text)

    def test_moving_a_private_record_into_a_shared_scope_keeps_its_past_private(self):
        to = self.login(OWNER)[0]["access_token"]
        self.call(to, "memory_write", name="project_moving.md", content=rec("personal", body="OWNER ONLY DRAFT"))
        cur = self.call(to, "memory_read", name="project_moving.md")
        self.call(to, "memory_write", name="project_moving.md", expected_sha=cur["sha"],
                  content=rec("team-shared", body="cleaned up for sharing"))
        move = git(self.hub, "rev-parse", "main")

        tv = self.login(ALICE)[0]["access_token"]
        self.assertIn("cleaned up", self.call(tv, "memory_read", name="project_moving.md")["content"])
        self.assertEqual(self.call(tv, "memory_history", name="project_moving.md"), [])
        self.assertNotIn("last_changed", self.call(tv, "memory_read", name="project_moving.md"))
        self.web_login(ALICE)
        self.assertEqual(self.client.get("/app/records/project_moving.md/commits/" + move).status_code, 404)
        self.assertNotIn("OWNER ONLY DRAFT", self.client.get("/app/records/project_moving.md").text)
        # later changes in the shared scope are still visible to Alice
        cur = self.call(tv, "memory_read", name="project_moving.md")
        self.call(tv, "memory_write", name="project_moving.md", expected_sha=cur["sha"],
                  content=rec("team-shared", body="alice edit"))
        self.assertEqual([h["by"] for h in self.call(tv, "memory_history", name="project_moving.md")], ["Alice"])

    def test_a_proposal_does_not_reveal_notes_its_reviewer_cannot_see(self):
        self.db.set_grant("team-shared", self.alice_id, "maintainer", self.owner_id)
        to = self.login(OWNER)[0]["access_token"]
        nid = self.call(to, "memory_note", scope="personal", text="PRIVATE SOURCE: salary numbers")["id"]
        self.call(to, "memory_inbox", claim=True)
        pid = self.call(to, "memory_write", name="project_cited.md", notes=[nid],
                        content=rec("team-shared", body="a neutral summary"))["proposal"]
        self.web_login(ALICE)
        self.assertEqual(self.client.get("/app/inbox/" + nid).status_code, 404)
        page = self.client.get("/app/review/proposals/" + pid).text
        self.assertIn("a neutral summary", page)
        self.assertNotIn("PRIVATE SOURCE", page)
        self.assertIn("1 more source note you can’t see", page)
        self.web_login(OWNER)
        self.assertIn("PRIVATE SOURCE", self.client.get("/app/review/proposals/" + pid).text)

    def test_moving_an_active_record_into_an_auto_loaded_scope_makes_it_a_candidate(self):
        to = self.login(OWNER)[0]["access_token"]
        self.call(to, "memory_write", name="feedback_sneak.md", content=rec("personal", body="always do X"))
        cur = self.call(to, "memory_read", name="feedback_sneak.md")
        r = self.call(to, "memory_write", name="feedback_sneak.md", expected_sha=cur["sha"], content=rec("global"))
        self.assertTrue(any("auto-loaded" in n for n in r["notes"]))
        body = git(self.hub, "show", "main:feedback_sneak.md")
        self.assertIn("status: proposed", body)
        self.assertIn("proposed_at:", body)


if __name__ == "__main__":
    unittest.main()

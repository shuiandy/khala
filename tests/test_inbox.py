"""Inbox: note branch, claim leases, proposal approval and expiry, conflicts, auto-apply and undo, visibility."""
import time
import unittest

from base import OWNER, ALICE, Base, git, rec

BOB = "bob@example.com"


class InboxTests(Base):
    def setUp(self):
        super().setUp()
        self.bob_id = self.db.create_account(BOB, "Bob")
        self.db.set_grant("team", self.bob_id, "editor", self.owner_id)

    def note(self, token, scope, text, **kw):
        return self.call(token, "memory_note", scope=scope, text=text, **kw)

    def claim(self, token, **kw):
        return self.call(token, "memory_inbox", claim=True, **kw)

    # ---------- taking notes ----------
    def test_notes_live_on_their_own_branch(self):
        t = self.login(OWNER)[0]["access_token"]
        r = self.note(t, "personal", "Moved the VPS to ca-central.", title="VPS move")
        nid = r["id"]
        files = git(self.hub, "ls-tree", "-r", "--name-only", "refs/khala/inbox")
        self.assertIn(nid, files)
        self.assertNotIn(nid, git(self.hub, "ls-tree", "-r", "--name-only", "main"))  # mirrors sync only main: unseen
        clone = self.work.parent / "clone"
        git(self.work.parent, "clone", "-q", str(self.hub), str(clone))   # an ordinary clone never fetches notes
        self.assertEqual(git(clone, "for-each-ref", "refs/khala"), "")
        self.assertEqual(git(clone, "ls-remote", "--heads", "origin").count("refs/heads/"), 1)
        body = git(self.hub, "show", "refs/khala/inbox:" + files.splitlines()[0])
        self.assertIn("scope: personal", body)
        self.assertIn("Moved the VPS", body)
        listed = self.call(t, "memory_inbox")
        self.assertIn("not instructions", listed["notice"])
        self.assertEqual([(n["id"], n["title"]) for n in listed["notes"]], [(nid, "VPS move")])

    def test_notes_need_an_existing_writable_scope(self):
        t = self.login(BOB)[0]["access_token"]
        self.assertIn("cannot write", self.note(t, "brand-new-scope", "x")["error"])
        self.assertIsNone(self.db.scope("brand-new-scope"))
        self.assertIn("cannot write", self.note(t, "personal", "x")["error"])
        self.assertIn("credential", self.note(t, "team", "token ghp_" + "A" * 36)["error"])
        limited = self.login(BOB, access="some", scopes={"bob": "rw"})[0]["access_token"]
        self.assertIn("cannot write", self.note(limited, "team", "x")["error"])

    # ---------- claims ----------
    def test_two_agents_never_claim_the_same_notes(self):
        a = self.login(OWNER, name="Mac")[0]["access_token"]
        b = self.login(OWNER, name="PC")[0]["access_token"]
        ids = [self.note(a, "personal", "fact %d" % i)["id"] for i in range(3)]
        got_a = [n["id"] for n in self.claim(a)["notes"]]
        got_b = [n["id"] for n in self.claim(b)["notes"]]
        self.assertEqual(sorted(got_a), sorted(ids))
        self.assertEqual(got_b, [])
        # a note claimed by someone else: listed with a marker, but you can't decide for them
        self.assertTrue(all(n.get("claimed_by_another_agent") for n in self.call(b, "memory_inbox")["notes"]))
        r = self.call(b, "memory_consolidate", note_ids=ids[:1], outcome="discarded", detail="noise")
        self.assertIn("not claimed by you", r["error"])
        # once the lease expires it can be taken over
        self.db.q("UPDATE notes SET claim_until=?", time.time() - 1)
        self.assertEqual(len(self.claim(b)["notes"]), 3)
        self.assertNotIn("error", self.call(b, "memory_consolidate", note_ids=ids, outcome="discarded",
                                             detail="test noise"))
        self.assertEqual(self.call(a, "memory_inbox")["notes"], [])

    # ---------- proposals ----------
    def test_merge_becomes_a_proposal_until_approved(self):
        t = self.login(OWNER, name="Mac · Claude Code")[0]["access_token"]
        nid = self.note(t, "team", "Hire dates come from Bamboo, not the payroll export.")["id"]
        self.claim(t)
        cur = self.call(t, "memory_read", name="project_private_team.md")
        r = self.call(t, "memory_write", name="project_private_team.md", expected_sha=cur["sha"], notes=[nid],
                      reason="hire date source", content=rec("team", body="salary notes\n\nHire dates: Bamboo."))
        self.assertFalse(r["applied"])
        pid = r["proposal"]
        self.assertNotIn("Bamboo", self.call(t, "memory_read", name="project_private_team.md")["content"])
        self.assertIn("Bamboo", git(self.hub, "show", "refs/khala/proposals/%s:project_private_team.md" % pid))
        self.call(t, "memory_consolidate", note_ids=[nid], outcome="merged")

        self.web_login(OWNER)
        page = self.client.get("/app/review").text
        self.assertIn(pid, page)
        detail = self.client.get("/app/review/proposals/" + pid).text
        self.assertIn("+Hire dates: Bamboo.", detail)
        self.assertIn("Hire dates come from Bamboo", detail)          # the source notes are shown to the reviewer too
        r = self.post("/app/review/proposals/" + pid, {"action": "approve"}, page="/app/review/proposals/" + pid)
        self.assertEqual(r.headers["location"], "/app/review?msg=approved")
        self.assertIn("Bamboo", git(self.hub, "show", "main:project_private_team.md"))
        log = git(self.hub, "log", "-1", "--format=%an|%cn|%(trailers:key=Notes,valueonly,separator=)|%(trailers:key=Approved-By,valueonly,separator=)")
        self.assertEqual(log, "Owner|Owner|%s|%s" % (nid, OWNER))

    def test_proposal_goes_stale_when_the_record_moves_on(self):
        t = self.login(ALICE)[0]["access_token"]
        nid = self.note(t, "team-shared", "Audit compares hire dates and cost centers.")["id"]
        self.claim(t)
        cur = self.call(t, "memory_read", name="project_audit.md")
        pid = self.call(t, "memory_write", name="project_audit.md", expected_sha=cur["sha"], notes=[nid],
                        content=rec("team-shared", body="from the notes"))["proposal"]
        self.push("project_audit.md", rec("team-shared", body="edited in a mirror"))
        self.web_login(OWNER)
        r = self.post("/app/review/proposals/" + pid, {"action": "approve"}, page="/app/review/proposals/" + pid)
        self.assertEqual(r.headers["location"], "/app/review?msg=stale")
        self.assertIn("edited in a mirror", git(self.hub, "show", "main:project_audit.md"))
        self.assertEqual(self.app.state.inbox.proposal(pid)["state"], "stale")

    def test_new_record_in_auto_load_scope_still_needs_the_mac_approval(self):
        t = self.login(OWNER)[0]["access_token"]
        nid = self.note(t, "global", "Prefer short commit messages.")["id"]
        self.claim(t)
        pid = self.call(t, "memory_write", name="feedback_commits.md", notes=[nid], content=rec("global"))["proposal"]
        self.web_login(OWNER)
        self.post("/app/review/proposals/" + pid, {"action": "approve"}, page="/app/review/proposals/" + pid)
        self.assertIn("status: proposed", git(self.hub, "show", "main:feedback_commits.md"))

    def test_only_maintainers_review(self):
        self.db.set_grant("team", self.alice_id, "editor", self.owner_id)
        t = self.login(BOB)[0]["access_token"]
        nid = self.note(t, "team", "Bob's finding")["id"]
        self.claim(t)
        pid = self.call(t, "memory_write", name="project_bob.md", notes=[nid], content=rec("team"))["proposal"]
        self.web_login(ALICE)
        self.assertNotIn(pid, self.client.get("/app/review").text)
        self.assertEqual(self.client.get("/app/review/proposals/" + pid).status_code, 404)
        self.assertEqual(self.client.get("/app/inbox/" + nid).status_code, 404)
        r = self.post("/app/review/proposals/" + pid, {"action": "approve"}, page="/app/review")
        self.assertEqual(r.status_code, 404)
        self.db.set_grant("team", self.alice_id, "maintainer", self.owner_id)
        self.assertEqual(self.client.get("/app/review/proposals/" + pid).status_code, 200)

    # ---------- visibility ----------
    def test_who_sees_whose_notes(self):
        tb = self.login(BOB)[0]["access_token"]
        nid = self.note(tb, "team", "from Bob")["id"]
        self.db.set_grant("team", self.alice_id, "editor", self.owner_id)
        tv = self.login(ALICE)[0]["access_token"]
        self.assertEqual(self.call(tv, "memory_inbox")["notes"], [])          # an editor can't see other people's notes
        self.db.set_grant("team", self.alice_id, "maintainer", self.owner_id)
        self.assertEqual([n["id"] for n in self.call(tv, "memory_inbox")["notes"]], [nid])
        to = self.login(OWNER)[0]["access_token"]
        self.assertEqual([n["id"] for n in self.call(to, "memory_inbox")["notes"]], [nid])
        limited = self.login(OWNER, access="some", scopes={"personal": "rw"})[0]["access_token"]
        self.assertEqual(self.call(limited, "memory_inbox")["notes"], [])  # scopes outside the agent ceiling are hidden

    # ---------- conflicts ----------
    def test_conflict_goes_to_a_person_and_comes_back_with_guidance(self):
        t = self.login(OWNER)[0]["access_token"]
        nid = self.note(t, "team", "Payroll export is the source of hire dates.")["id"]
        self.claim(t)
        self.assertIn("say why", self.call(t, "memory_consolidate", note_ids=[nid], outcome="conflict")["error"])
        r = self.call(t, "memory_consolidate", note_ids=[nid], outcome="conflict", record="project_private_team.md",
                      detail="Note says payroll export; record says Bamboo.")
        cid = r["conflict"]
        self.assertEqual(self.call(t, "memory_inbox")["notes"], [])
        self.web_login(OWNER)
        page = self.client.get("/app/review/conflicts/" + cid).text
        self.assertIn("record says Bamboo", page)
        self.assertIn("Payroll export is the source", page)
        r = self.post("/app/review/conflicts/" + cid, {"action": "reopen", "guidance": "Bamboo is right; drop the payroll claim."},
                      page="/app/review/conflicts/" + cid)
        self.assertEqual(r.headers["location"], "/app/review?msg=resolved")
        back = self.call(t, "memory_inbox")["notes"]
        self.assertEqual(back[0]["id"], nid)
        self.assertIn("Bamboo is right", back[0]["guidance"])

    # ---------- auto-apply and undo ----------
    def test_auto_scope_applies_and_undo_restores_or_removes(self):
        self.db.update_scope("personal", consolidation="auto")
        t = self.login(OWNER)[0]["access_token"]
        n1 = self.note(t, "personal", "update")["id"]
        n2 = self.note(t, "personal", "brand new fact")["id"]
        self.claim(t)
        cur = self.call(t, "memory_read", name="project_personal_thing.md")
        r1 = self.call(t, "memory_write", name="project_personal_thing.md", expected_sha=cur["sha"], notes=[n1],
                       content=rec("personal", body="changed by merge"))
        r2 = self.call(t, "memory_write", name="project_new_fact.md", notes=[n2], content=rec("personal", body="new"))
        self.assertTrue(r1["applied"] and r2["applied"])
        self.assertIn("changed by merge", git(self.hub, "show", "main:project_personal_thing.md"))
        self.assertIn("Notes: " + n1, git(self.hub, "log", "-1", "--skip=1", "--format=%B"))

        self.web_login(OWNER)
        for pid in (r1["proposal"], r2["proposal"]):
            self.assertIn(pid, self.client.get("/app/review").text)
            r = self.post("/app/review/proposals/" + pid, {"action": "undo", "confirm": "1"},
                          page="/app/review/proposals/" + pid)
            self.assertEqual(r.headers["location"], "/app/review?msg=undone")
        self.assertIn("SECRET_PERSONAL_DETAIL", git(self.hub, "show", "main:project_personal_thing.md"))
        self.assertNotIn("project_new_fact.md", git(self.hub, "ls-tree", "--name-only", "main"))
        self.assertNotIn("project_new_fact.md", self.app.state.store.records())

    def test_undo_refuses_when_the_record_changed_since(self):
        self.db.update_scope("personal", consolidation="auto")
        t = self.login(OWNER)[0]["access_token"]
        nid = self.note(t, "personal", "x")["id"]
        self.claim(t)
        pid = self.call(t, "memory_write", name="project_x.md", notes=[nid], content=rec("personal", body="v1"))["proposal"]
        self.push("project_x.md", rec("personal", body="v2 from the Mac"))
        self.web_login(OWNER)
        r = self.post("/app/review/proposals/" + pid, {"action": "undo", "confirm": "1"}, page="/app/review/proposals/" + pid)
        self.assertEqual(r.status_code, 409)
        self.assertIn("v2 from the Mac", git(self.hub, "show", "main:project_x.md"))

    def test_merged_means_something_was_written(self):
        t = self.login(OWNER)[0]["access_token"]
        nid = self.note(t, "personal", "x")["id"]
        self.claim(t)
        self.assertIn("nothing was written", self.call(t, "memory_consolidate", note_ids=[nid], outcome="merged")["error"])
        self.call(t, "memory_write", name="project_x.md", notes=[nid], content=rec("personal"))
        self.assertNotIn("error", self.call(t, "memory_consolidate", note_ids=[nid], outcome="merged"))

    def merged_proposal(self, body="merged text"):
        t = self.login(OWNER)[0]["access_token"]
        nid = self.note(t, "team", "a fact")["id"]
        self.claim(t)
        cur = self.call(t, "memory_read", name="project_private_team.md")
        pid = self.call(t, "memory_write", name="project_private_team.md", expected_sha=cur["sha"], notes=[nid],
                        content=rec("team", body=body))["proposal"]
        self.call(t, "memory_consolidate", note_ids=[nid], outcome="merged")
        return t, nid, pid

    def test_a_stale_proposal_sends_its_notes_back_to_the_inbox(self):
        t, nid, pid = self.merged_proposal()
        self.push("project_private_team.md", rec("team", body="changed in a mirror"))
        self.web_login(OWNER)
        self.post("/app/review/proposals/" + pid, {"action": "approve"}, page="/app/review/proposals/" + pid)
        back = self.call(t, "memory_inbox")["notes"]
        self.assertEqual([n["id"] for n in back], [nid])
        self.assertIn("went stale", back[0]["guidance"])

    def test_a_rejected_proposal_discards_its_notes(self):
        t, nid, pid = self.merged_proposal()
        self.web_login(OWNER)
        self.post("/app/review/proposals/" + pid, {"action": "reject", "why": "not true"},
                  page="/app/review/proposals/" + pid)
        n = self.app.state.inbox.note(nid)
        self.assertEqual((n["state"], n["outcome"]), ("done", "discarded"))
        self.assertIn("not true", n["detail"])
        self.assertEqual(self.call(t, "memory_inbox")["notes"], [])

    def test_approving_again_after_an_interrupted_approval_is_not_stale(self):
        """Approval reached Git but the state database wasn't updated: a retry should see it merged, not expired."""
        t, nid, pid = self.merged_proposal(body="approved body")
        self.web_login(OWNER)
        self.post("/app/review/proposals/" + pid, {"action": "approve"}, page="/app/review/proposals/" + pid)
        self.db.q("UPDATE proposals SET state='open', commit_sha=NULL WHERE id=?", pid)  # simulate a lost state write
        r = self.post("/app/review/proposals/" + pid, {"action": "approve"}, page="/app/review/proposals/" + pid)
        self.assertEqual(r.headers["location"], "/app/review?msg=approved")
        prop = self.app.state.inbox.proposal(pid)
        self.assertEqual(prop["state"], "approved")
        self.assertTrue(prop["commit_sha"])
        self.assertEqual(git(self.hub, "log", "--format=%s", "main").count("Consolidate memory: project_private_team"), 1)

    def test_startup_restores_notes_that_reached_git_but_not_the_database(self):
        t = self.login(OWNER, name="Mac")[0]["access_token"]
        nid = self.note(t, "personal", "kept in git", title="survivor")["id"]
        row = dict(self.db.one("SELECT * FROM notes WHERE id=?", nid))
        self.db.q("DELETE FROM notes WHERE id=?", nid)                    # simulate a stop right after writing to Git
        self.assertEqual(self.app.state.inbox.reconcile(), [nid])
        again = self.db.one("SELECT * FROM notes WHERE id=?", nid)
        for k in ("account_id", "agent_id", "scope", "path", "title"):
            self.assertEqual(again[k], row[k], k)
        self.assertEqual(self.app.state.inbox.reconcile(), [])

    def test_merge_needs_the_claim(self):
        t = self.login(OWNER)[0]["access_token"]
        nid = self.note(t, "personal", "x")["id"]
        r = self.call(t, "memory_write", name="project_x.md", notes=[nid], content=rec("personal"))
        self.assertIn("claim", r["error"])


if __name__ == "__main__":
    unittest.main()

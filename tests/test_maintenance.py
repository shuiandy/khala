"""Phase 2 maintenance: history and expiry, write rate limits, bot tokens, web edit/delete, undo by commit or agent."""
import re
import unittest
from datetime import date

from base import OWNER, ALICE, Base, git, rec


class McpToolTests(Base):
    def test_history_deprecate_due_and_status(self):
        t = self.login(OWNER, name="Mac · Claude Code")[0]["access_token"]
        cur = self.call(t, "memory_read", name="project_personal_thing.md")
        self.assertEqual(cur["last_changed"]["agent"], "git push")
        self.call(t, "memory_write", name="project_personal_thing.md", expected_sha=cur["sha"], reason="tidy",
                  content=rec("personal", body="v2"))
        hist = self.call(t, "memory_history", name="project_personal_thing.md")
        self.assertEqual((hist[0]["agent"], hist[0]["reason"]), ("Mac · Claude Code", "tidy"))
        self.assertEqual(self.call(t, "memory_read", name="project_personal_thing.md")["last_changed"]["agent"],
                         "Mac · Claude Code")

        self.assertIn("say why", self.call(t, "memory_deprecate", name="project_personal_thing.md", reason=" ")["error"])
        r = self.call(t, "memory_deprecate", name="project_personal_thing.md", reason="replaced by project_new.md")
        self.assertTrue(r["changed"])
        body = git(self.hub, "show", "main:project_personal_thing.md")
        self.assertIn("status: outdated", body)
        self.assertIn("expired_reason: 'replaced by project_new.md", body)
        self.assertEqual(self.call(t, "memory_search", query="v2", status="active"), [])
        self.assertEqual(len(self.call(t, "memory_search", query="v2", status="outdated")), 1)
        self.assertIn("pinned", self.call(t, "memory_deprecate", name="project_pinned.md", reason="x")["error"])
        tv = self.login(ALICE)[0]["access_token"]
        self.assertIn("not found", self.call(tv, "memory_deprecate", name="feedback_global_pref.md", reason="x")["error"])

        # rec() writes verified 2026-10-01 + 90d: move it far into the past and index flags it for review
        self.push("project_old.md", rec("personal", name="old").replace("verified: 2026-10-01", "verified: 2020-01-01"))
        self.push("project_fresh.md", rec("personal", name="fresh").replace("verified: 2026-10-01",
                                                                              "verified: " + date.today().isoformat()))
        items = {i["name"]: i for i in self.call(t, "memory_index", scope="personal")}
        self.assertTrue(items["project_old.md"].get("due"))
        self.assertNotIn("due", items["project_fresh.md"])

    def test_write_limit_per_agent(self):
        self.app.state.cfg.writes_per_hour = 3
        t = self.login(OWNER, name="Runaway bot")[0]["access_token"]
        for i in range(3):
            self.assertNotIn("error", self.call(t, "memory_write", name="project_r%d.md" % i, content=rec("personal")))
        r = self.call(t, "memory_write", name="project_r9.md", content=rec("personal"))
        self.assertIn("write limit", r["error"])
        other = self.login(OWNER, name="Another agent")[0]["access_token"]
        self.assertNotIn("error", self.call(other, "memory_write", name="project_r9.md", content=rec("personal")))
        self.web_login(OWNER)
        self.assertIn("Runaway bot", self.client.get("/app").text)


class BotTokenTests(Base):
    def test_issue_use_and_revoke_a_bot_token(self):
        self.web_login(OWNER)
        r = self.post("/app/bots", {"name": "Digest bot", "days": "30", "scope:team-shared": "r"},
                      page="/app/agents")
        self.assertEqual(r.status_code, 200)
        token = re.search(r"<pre>(mem_[A-Za-z0-9_-]+)</pre>", r.text).group(1)
        self.assertNotIn(token, str([tuple(row) for row in self.db.q("SELECT * FROM audit_events")]))
        self.assertNotIn(token, str([tuple(row) for row in self.db.q("SELECT * FROM tokens")]))
        self.assertEqual([(s["scope"], s["mode"]) for s in self.call(token, "memory_scopes")], [("team-shared", "r")])
        agent = [a for a in self.db.agents_for(self.owner_id) if a["name"] == "Digest bot"][0]
        self.assertEqual(agent["kind"], "bot")
        self.db.revoke_agent(agent["id"])
        r = self.client.post("/mcp", headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 401)

    def test_token_for_everything(self):
        self.web_login(OWNER)
        r = self.post("/app/bots", {"name": "Mac collect", "days": "365", "access": "all"}, page="/app/agents")
        token = re.search(r"<pre>(mem_[A-Za-z0-9_-]+)</pre>", r.text).group(1)
        scopes = {s["scope"] for s in self.call(token, "memory_scopes")}
        self.assertTrue({"personal", "global", "team"} <= scopes)
        self.assertNotIn("error", self.call(token, "memory_note", scope="global", text="from a native memory dir"))

    def test_issuing_needs_a_recent_check_and_a_scope(self):
        self.web_login(OWNER)
        r = self.post("/app/bots", {"name": "x", "days": "30"}, page="/app/agents")
        self.assertEqual(r.status_code, 400)
        self.db.q("UPDATE web_sessions SET reauth_at=0")
        r = self.post("/app/bots", {"name": "x", "days": "30", "scope:personal": "rw"}, page="/app/agents")
        self.assertEqual(r.headers["location"], "/app/reauth?next=/app/agents")


class WebEditTests(Base):
    def edit(self, name, content, reason="", **extra):
        page = self.client.get("/app/records/%s/edit" % name).text
        sha = re.search(r'name="sha" value="([0-9a-f]+)"', page).group(1)
        return self.post("/app/records/%s/edit" % name, dict({"sha": sha, "content": content, "reason": reason}, **extra),
                         page="/app/records/%s/edit" % name)

    def test_edit_records_who_and_why(self):
        self.web_login(OWNER)
        r = self.edit("project_audit.md", rec("team-shared", body="edited in the browser"), reason="clarify")
        self.assertEqual(r.headers["location"], "/app/records/project_audit.md?msg=saved")
        log = git(self.hub, "log", "-1", "--format=%an|%(trailers:key=Agent,valueonly,separator=)|%(trailers:key=Reason,valueonly,separator=)")
        self.assertEqual(log, "Owner|web|clarify")

    def test_edit_never_overwrites_a_newer_version(self):
        self.web_login(OWNER)
        page = self.client.get("/app/records/project_audit.md/edit").text
        sha = re.search(r'name="sha" value="([0-9a-f]+)"', page).group(1)
        self.push("project_audit.md", rec("team-shared", body="from the Mac meanwhile"))
        r = self.post("/app/records/project_audit.md/edit", {"sha": sha, "content": rec("team-shared", body="mine")},
                      page="/app/records/project_audit.md")
        self.assertEqual(r.status_code, 409)
        self.assertIn("from the Mac meanwhile", r.text)          # the current version is shown so the user can merge
        self.assertIn("mine", r.text)                            # their own edit isn't lost either
        self.assertIn("from the Mac meanwhile", git(self.hub, "show", "main:project_audit.md"))

    def test_moving_into_a_shared_scope_asks_first(self):
        self.web_login(OWNER)
        r = self.edit("project_personal_thing.md", rec("team-shared"))
        self.assertIn("Move to a shared scope", r.text)
        self.assertIn("scope: personal", git(self.hub, "show", "main:project_personal_thing.md"))
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
        sha = re.search(r'name="sha" value="([0-9a-f]+)"', r.text).group(1)
        r = self.post("/app/records/project_personal_thing.md/edit",
                      {"sha": sha, "content": rec("team-shared"), "confirm": "1", "csrf": csrf})
        self.assertEqual(r.status_code, 303)
        self.assertIn("scope: team-shared", git(self.hub, "show", "main:project_personal_thing.md"))

    def test_who_can_edit_and_delete(self):
        self.web_login(ALICE)                                   # editor of team-shared
        self.assertEqual(self.client.get("/app/records/project_pinned.md/edit").status_code, 404)
        self.assertEqual(self.client.get("/app/records/project_audit.md/edit").status_code, 200)
        r = self.post("/app/records/project_audit.md/delete", {"confirm": "1"}, page="/app/records/project_audit.md")
        self.assertEqual(r.status_code, 404)
        r = self.post("/app/records/project_audit.md/deprecate", {"reason": "superseded"}, page="/app/records/project_audit.md")
        self.assertEqual(r.headers["location"], "/app/records/project_audit.md?msg=deprecated")
        self.assertIn("status: outdated", git(self.hub, "show", "main:project_audit.md"))
        self.web_login(OWNER)
        r = self.post("/app/records/project_audit.md/delete", {}, page="/app/records/project_audit.md")
        self.assertIn("Delete record", r.text)
        self.post("/app/records/project_audit.md/delete", {"confirm": "1", "reason": "test"}, page="/app/records/project_audit.md")
        self.assertNotIn("project_audit.md", git(self.hub, "ls-tree", "--name-only", "main"))

    def test_undo_one_commit(self):
        t = self.login(OWNER)[0]["access_token"]
        cur = self.call(t, "memory_read", name="project_audit.md")
        self.call(t, "memory_write", name="project_audit.md", expected_sha=cur["sha"], content=rec("team-shared", body="bad"))
        sha = git(self.hub, "rev-parse", "main")
        self.web_login(OWNER)
        page = self.client.get("/app/records/project_audit.md/commits/" + sha).text
        self.assertIn("Undo this change", page)
        r = self.post("/app/records/project_audit.md/commits/%s/undo" % sha, {"confirm": "1"},
                      page="/app/records/project_audit.md")
        self.assertEqual(r.status_code, 303)
        self.assertIn("compare hire dates", git(self.hub, "show", "main:project_audit.md"))
        # undo again: the record is no longer the result of that commit
        r = self.post("/app/records/project_audit.md/commits/%s/undo" % sha, {"confirm": "1"},
                      page="/app/records/project_audit.md")
        self.assertEqual(r.status_code, 409)


class AgentUndoTests(Base):
    def test_runaway_bot_is_undone_in_one_go(self):
        """From the docs: a runaway bot writes 50 records, one undo on the web; the one someone changed later stays."""
        self.db.update_scope("personal", consolidation="auto")
        bot_tok, bot_client = self.login(OWNER, name="Rogue bot", kind="bot")
        bot = bot_tok["access_token"]
        for i in range(48):
            self.call(bot, "memory_write", name="project_spam_%02d.md" % i, content=rec("personal", body="spam %d" % i))
        cur = self.call(bot, "memory_read", name="project_personal_thing.md")
        self.call(bot, "memory_write", name="project_personal_thing.md", expected_sha=cur["sha"],
                  content=rec("personal", body="overwritten by the bot"))
        # the bot's note consolidated into a record by another agent: still counts as the bot's
        helper = self.login(OWNER, name="Helper")[0]["access_token"]
        nid = self.call(bot, "memory_note", scope="personal", text="poisoned claim")["id"]
        self.call(helper, "memory_inbox", claim=True)
        self.call(helper, "memory_write", name="project_from_note.md", notes=[nid], content=rec("personal", body="poison"))
        # someone changed one of them after the bot: keep it when undoing
        cur = self.call(helper, "memory_read", name="project_spam_07.md")
        self.call(helper, "memory_write", name="project_spam_07.md", expected_sha=cur["sha"],
                  content=rec("personal", body="fixed by a person"))
        bot_agent = self.db.agent_for_client(self.owner_id, bot_client)

        self.web_login(OWNER)
        preview = self.client.get("/app/agents/%d/undo" % bot_agent["id"]).text
        self.assertIn("Undo 49 records", preview)               # 47 created + 1 changed + 1 consolidated
        self.assertIn("changed after this agent", preview)
        r = self.post("/app/agents/%d/undo" % bot_agent["id"], {"action": "run", "window": "1", "revoke": "1"},
                      page="/app/agents/%d/undo" % bot_agent["id"])
        self.assertIn("Restored 1, removed 48, skipped 1", r.text)
        tree = git(self.hub, "ls-tree", "--name-only", "main")
        self.assertNotIn("project_spam_00.md", tree)
        self.assertNotIn("project_from_note.md", tree)
        self.assertIn("project_spam_07.md", tree)
        self.assertIn("fixed by a person", git(self.hub, "show", "main:project_spam_07.md"))
        self.assertIn("SECRET_PERSONAL_DETAIL", git(self.hub, "show", "main:project_personal_thing.md"))
        self.assertIsNotNone(self.db.agent(bot_agent["id"])["revoked_at"])


if __name__ == "__main__":
    unittest.main()

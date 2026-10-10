"""Decisions a person makes, relayed by their agent: memory_review shows what waits, memory_decide applies the
person's answer only after their app asked them to confirm (form elicitation, as an input_required round trip under
2026-07-28 or a request mid-call before it), and memory_scopes says where something waits. Approving a proposed
record in an auto-loaded scope makes it active in one step, on the web too."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY

from base import ALICE, OWNER, Base, git, rec
from khala import instance

FORM = {"elicitation": {"form": {}}}


class DecisionTests(Base):
    def setUp(self):
        super().setUp()
        self.token = self.login(OWNER)[0]["access_token"]

    def rpc(self, tool, args, token=None, caps=FORM, answer=None, state=None):
        """A 2026-07-28 tools/call; answer is the person's reply to the confirmation, sent with the echoed state."""
        params = {"name": tool, "arguments": args,
                  "_meta": {PROTOCOL_VERSION_META_KEY: "2026-07-28", CLIENT_CAPABILITIES_META_KEY: caps}}
        if answer is not None:
            params["inputResponses"] = answer
            params["requestState"] = state
        r = self.client.post("/mcp", headers={
            "Authorization": "Bearer " + (token or self.token), "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": tool},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["result"]

    def decide(self, args, reply=None, token=None, caps=FORM):
        """Ask, then answer the confirmation the way the person's app would; returns (question, result)."""
        first = self.rpc("memory_decide", args, token=token, caps=caps)
        if first.get("resultType") != "input_required":
            return None, first
        (key, request), = first["inputRequests"].items()
        if reply is None:
            reply = {"action": "accept", "content": {"confirm": True}}
        return request["params"]["message"], self.rpc("memory_decide", args, token=token, caps=caps,
                                                     answer={key: reply}, state=first["requestState"])

    @staticmethod
    def content(result):
        sc = result.get("structuredContent")
        return sc.get("result", sc) if isinstance(sc, dict) else json.loads(result["content"][0]["text"])

    def proposal(self, scope="team", name="project_new_finding.md"):
        nid = self.call(self.token, "memory_note", scope=scope, text="a finding")["id"]
        self.call(self.token, "memory_inbox", claim=True)
        return self.call(self.token, "memory_write", name=name, notes=[nid],
                         content=rec(scope, name=name[:-3]))["proposal"]

    def candidate(self, name="feedback_new.md"):
        out = self.call(self.token, "memory_write", name=name, content=rec("global", name=name[:-3]))
        self.assertIn("status: proposed", git(self.hub, "show", "main:" + name))
        return out

    def test_review_shows_what_waits_and_scopes_say_where(self):
        pid = self.proposal()
        self.candidate()
        scopes = {s["scope"]: s for s in self.call(self.token, "memory_scopes")}
        self.assertEqual((scopes["team"].get("waiting_for_you"), scopes["global"].get("waiting_for_you")), (1, 1))
        self.assertNotIn("waiting_for_you", scopes["personal"])
        review = self.content(self.rpc("memory_review", {}))
        self.assertEqual([p["id"] for p in review["proposals"]], [pid])
        self.assertIn("+name: project_new_finding", review["proposals"][0]["diff"])
        self.assertEqual([r["record"] for r in review["records_to_load_everywhere"]], ["feedback_new.md"])
        self.assertTrue(review["can_confirm_here"])
        self.assertFalse(self.content(self.rpc("memory_review", {}, caps={}))["can_confirm_here"])

    def test_nothing_changes_until_the_person_confirms(self):
        pid = self.proposal()
        self.candidate()
        first = self.rpc("memory_decide", {"approve": [pid, "feedback_new.md"]})
        self.assertEqual(first["resultType"], "input_required")
        message = next(iter(first["inputRequests"].values()))["params"]["message"]
        self.assertIn("project_new_finding.md in team", message)
        self.assertIn("feedback_new.md in global (load in every session)", message)
        self.assertEqual(self.db.one("SELECT state FROM proposals WHERE id=?", pid)["state"], "open")
        _, result = self.decide({"approve": [pid, "feedback_new.md"]})
        out = self.content(result)
        self.assertTrue(out["done"])
        self.assertEqual({r["result"] for r in out["results"]}, {"approved"})
        self.assertIn("status: active", git(self.hub, "show", "main:feedback_new.md"))
        body = git(self.hub, "log", "-1", "--format=%B", "main", "--", "feedback_new.md")
        self.assertIn("Approved-By: " + OWNER, body)
        self.assertIn("Approved-Via: Test agent", body)
        self.assertIn("project_new_finding.md", git(self.hub, "ls-tree", "--name-only", "main"))
        audit = self.db.one("SELECT agent_id FROM audit_events WHERE action='proposal.approved'")
        self.assertIsNotNone(audit["agent_id"])

    def test_declining_or_answering_no_changes_nothing(self):
        pid = self.proposal()
        for reply in ({"action": "decline"}, {"action": "cancel"},
                      {"action": "accept", "content": {"confirm": False}}):
            _, result = self.decide({"approve": [pid]}, reply=reply)
            out = self.content(result)
            self.assertFalse(out["done"])
            # an app may decline without showing the form, so an unanswered question also offers the page;
            # an explicit no does not
            if reply["action"] == "accept":
                self.assertNotIn("confirm_url", out)
            else:
                self.assertEqual(out["confirm_url"], "http://localhost/app/review/decide?approve=" + pid)
        self.assertEqual(self.db.one("SELECT state FROM proposals WHERE id=?", pid)["state"], "open")

    def test_an_app_that_cannot_ask_gets_a_page_with_exactly_those_decisions(self):
        pid = self.proposal()
        self.candidate()
        question, result = self.decide({"approve": [pid], "reject": ["feedback_new.md"]}, caps={})
        self.assertIsNone(question)
        out = self.content(result)
        self.assertFalse(out["done"])
        url = out["confirm_url"]
        self.assertTrue(url.startswith("http://localhost/app/review/decide?"))
        self.assertEqual(self.db.one("SELECT state FROM proposals WHERE id=?", pid)["state"], "open")
        path = url[len("http://localhost"):]
        self.client.cookies.clear()                             # signed out: sign in first, then back to this page
        r = self.client.get(path, follow_redirects=False)
        self.assertIn("next=/app/review/decide%3Fapprove%3D", r.headers["location"])
        self.web_login(OWNER)
        page = self.client.get(path).text
        self.assertIn("Apply 2 decisions", page)
        self.assertIn("project_new_finding.md", page)
        self.assertEqual(self.db.one("SELECT state FROM proposals WHERE id=?", pid)["state"], "open")
        version = re.search(r'name="version" value="([^"]+)"', page).group(1)
        r = self.post("/app/review/decide", {"approve": pid, "reject": "feedback_new.md", "version": version},
                      page=path)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Decisions applied", r.text)
        self.assertEqual(self.db.one("SELECT state FROM proposals WHERE id=?", pid)["state"], "approved")
        self.assertNotIn("feedback_new.md", git(self.hub, "ls-tree", "--name-only", "main"))
        self.assertEqual(self.client.get(path).status_code, 409)       # nothing left to apply

    def test_the_review_page_offers_all_or_ticked_items_through_the_same_page(self):
        pid = self.proposal()
        self.candidate()
        self.web_login(OWNER)
        page = self.client.get("/app/review").text
        self.assertIn("Approve all 2", page)
        self.assertIn('href="/app/review/decide?approve=%s%%2Cfeedback_new.md"' % pid, page)
        self.assertEqual(page.count('type="checkbox" form="pick" name="item"'), 2)
        ticked = self.client.get("/app/review/decide", params=[("as", "reject"), ("item", "feedback_new.md")]).text
        self.assertIn("Apply 1 decision", ticked)
        self.assertIn('name="reject" value="feedback_new.md"', ticked)

    def test_the_page_asks_again_when_something_changed_and_others_cannot_use_it(self):
        self.candidate()
        path = "/app/review/decide?approve=feedback_new.md"
        self.web_login(OWNER)
        page = self.client.get(path).text
        version = re.search(r'name="version" value="([^"]+)"', page).group(1)
        changed = rec("global", name="new", status="proposed", body="edited meanwhile").replace(
            "  status: proposed\n", "  status: proposed\n  proposed_at: 2026-10-09\n")
        self.push("feedback_new.md", changed)
        r = self.post("/app/review/decide", {"approve": "feedback_new.md", "version": version}, page=path)
        self.assertIn("changed since the page was opened", r.text)
        self.assertIn("status: proposed", git(self.hub, "show", "main:feedback_new.md"))
        self.web_login(ALICE)
        self.assertEqual(self.client.get(path).status_code, 409)

    def test_unattended_agents_cannot_decide(self):
        pid = self.proposal()
        bot = self.login(OWNER, name="Nightly bot", kind="bot")[0]["access_token"]
        _, result = self.decide({"approve": [pid]}, token=bot)
        self.assertTrue(result.get("isError"))
        self.assertIn("unattended", result["content"][0]["text"])
        scopes = {s["scope"]: s for s in self.call(bot, "memory_scopes")}
        self.assertNotIn("waiting_for_you", scopes["team"])

    def test_only_what_the_person_may_decide_and_the_agent_may_change(self):
        pid = self.proposal()
        alice = self.login(ALICE)[0]["access_token"]
        self.assertEqual(self.content(self.rpc("memory_review", {}, token=alice))["proposals"], [])
        _, result = self.decide({"approve": [pid]}, token=alice)
        self.assertIn("not waiting", result["content"][0]["text"])
        reader = self.login(OWNER, access="some", scopes={"team": "r"}, name="Reader")[0]["access_token"]
        _, result = self.decide({"approve": [pid]}, token=reader)
        self.assertIn("cannot change scope 'team'", result["content"][0]["text"])
        _, result = self.decide({"approve": [pid], "reject": [pid]})
        self.assertIn("more than once", result["content"][0]["text"])

    def test_a_change_between_asking_and_answering_asks_again(self):
        self.candidate()
        first = self.rpc("memory_decide", {"approve": ["feedback_new.md"]})
        (key, _), = first["inputRequests"].items()
        changed = rec("global", name="new", status="proposed", body="edited meanwhile").replace(
            "  status: proposed\n", "  status: proposed\n  proposed_at: 2026-10-09\n")
        self.push("feedback_new.md", changed)
        again = self.rpc("memory_decide", {"approve": ["feedback_new.md"]}, answer={
            key: {"action": "accept", "content": {"confirm": True}}}, state=first["requestState"])
        self.assertEqual(again["resultType"], "input_required")
        self.assertIn("status: proposed", git(self.hub, "show", "main:feedback_new.md"))

    def test_rejecting_a_record_removes_it_and_keep_settles_a_conflict(self):
        self.candidate()
        nid = self.call(self.token, "memory_note", scope="team", text="contradicts the audit")["id"]
        self.call(self.token, "memory_inbox", claim=True)
        self.call(self.token, "memory_consolidate", note_ids=[nid], outcome="conflict", record="project_private_team.md",
                  detail="the note says X, the record says Y")
        cid = self.db.one("SELECT id FROM conflicts")["id"]
        _, result = self.decide({"reject": ["feedback_new.md"], "keep": [cid]}, reply=None)
        out = self.content(result)
        self.assertEqual(sorted(r["result"] for r in out["results"]), ["kept", "rejected"])
        self.assertNotIn("feedback_new.md", git(self.hub, "ls-tree", "--name-only", "main"))
        self.assertIn("Rejected-By: " + OWNER, git(self.hub, "log", "-1", "--format=%B", "main"))
        self.assertEqual(self.db.one("SELECT state FROM conflicts WHERE id=?", cid)["state"], "resolved")

    def test_the_web_review_page_approves_and_rejects_records_to_load_everywhere(self):
        self.candidate()
        self.candidate("feedback_other.md")
        self.web_login(OWNER)
        page = self.client.get("/app/review").text
        self.assertIn("To load in every session", page)
        r = self.post("/app/review/candidates", {"name": "feedback_new.md", "action": "approve"}, page="/app/review")
        self.assertEqual(r.status_code, 303)
        self.assertIn("status: active", git(self.hub, "show", "main:feedback_new.md"))
        self.post("/app/review/candidates", {"name": "feedback_other.md", "action": "reject"}, page="/app/review")
        self.assertNotIn("feedback_other.md", git(self.hub, "ls-tree", "--name-only", "main"))
        r = self.post("/app/review/candidates", {"name": "feedback_new.md", "action": "approve"}, page="/app/review")
        self.assertEqual(r.status_code, 409)                    # already active: nothing waits any more


class EarlierProtocolTests(unittest.TestCase):
    """Before 2026-07-28 the confirmation is a request the server sends in the middle of the call, here over stdio
    to a real process the way a local client would run it."""
    def test_the_server_asks_mid_call_and_applies_after_the_answer(self):
        tmp = tempfile.TemporaryDirectory(prefix="khala-decide-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        with contextlib.redirect_stdout(io.StringIO()):
            instance.init("http://localhost:8100", "you@example.com", root / "data", root / "khala.env")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KHALA_", "MEMORY_", "SMTP_"))}
        proc = subprocess.Popen([sys.executable, "-m", "khala.cli", "--env", str(root / "khala.env"), "serve", "--stdio"],
                                cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)

        def send(msg):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        def reply_to(msg_id):
            while True:
                m = json.loads(proc.stdout.readline())
                if m.get("id") == msg_id and "method" not in m:
                    return m
                if m.get("method") == "elicitation/create":
                    self.asked = m["params"]["message"]
                    send({"jsonrpc": "2.0", "id": m["id"], "result": {"action": "accept", "content": {"confirm": True}}})

        def tool(msg_id, tool_name, **args):
            send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": tool_name, "arguments": args}})
            res = reply_to(msg_id)["result"]
            self.assertFalse(res.get("isError"), res)
            sc = res.get("structuredContent")
            return sc.get("result", sc) if isinstance(sc, dict) else json.loads(res["content"][0]["text"])

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {"elicitation": {}}, "clientInfo": {"name": "t", "version": "1"}}})
        reply_to(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        scope = tool(2, "memory_scopes")[0]["scope"]
        nid = tool(3, "memory_note", scope=scope, text="a finding")["id"]
        tool(4, "memory_inbox", claim=True)
        pid = tool(5, "memory_write", name="project_x.md", notes=[nid], content=rec(scope, name="x"))["proposal"]
        self.asked = None
        out = tool(6, "memory_decide", approve=[pid])
        self.assertIn("project_x.md in %s" % scope, self.asked)
        self.assertEqual(out["results"][0]["result"], "approved")


if __name__ == "__main__":
    unittest.main()

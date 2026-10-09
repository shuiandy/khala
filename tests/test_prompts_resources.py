"""MCP resources and prompts: the guide, a scope's index and single records as resources, with the same access rules
as the tools; and the recall, remember and tidy_inbox routines as prompts."""
import unittest

from base import ALICE, OWNER, Base

MCP = {"Accept": "application/json, text/event-stream"}


class PromptResourceTests(Base):
    def rpc(self, token, method, params=None):
        r = self.client.post("/mcp", headers=dict(MCP, Authorization="Bearer " + token),
                             json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_resources_are_listed_and_read_with_the_callers_access(self):
        t = self.login(ALICE)[0]["access_token"]
        listed = [r["uri"] for r in self.rpc(t, "resources/list")["result"]["resources"]]
        self.assertIn("khala://guide", listed)
        templates = [r["uriTemplate"] for r in self.rpc(t, "resources/templates/list")["result"]["resourceTemplates"]]
        self.assertEqual(sorted(templates), ["khala://records/{name}", "khala://scopes/{scope}/index"])

        guide = self.rpc(t, "resources/read", {"uri": "khala://guide"})["result"]["contents"][0]["text"]
        self.assertIn("memory_note", guide)
        index = self.rpc(t, "resources/read", {"uri": "khala://scopes/team-shared/index"})["result"]["contents"][0]
        self.assertIn("project_audit.md", index["text"])
        record = self.rpc(t, "resources/read", {"uri": "khala://records/project_audit.md"})["result"]["contents"][0]
        self.assertIn("compare hire dates", record["text"])

        hidden = self.rpc(t, "resources/read", {"uri": "khala://records/project_private_team.md"})
        self.assertIn("error", hidden)
        self.assertNotIn("salary", str(hidden))
        self.assertIn("[]", self.rpc(t, "resources/read", {"uri": "khala://scopes/team/index"})["result"]["contents"][0]["text"])

    def test_prompts_carry_the_routines(self):
        t = self.login(OWNER)[0]["access_token"]
        names = [p["name"] for p in self.rpc(t, "prompts/list")["result"]["prompts"]]
        self.assertEqual(sorted(names), ["recall", "remember", "tidy_inbox"])
        got = self.rpc(t, "prompts/get", {"name": "recall", "arguments": {"task": "fix the audit report"}})
        text = got["result"]["messages"][0]["content"]["text"]
        self.assertIn("on: fix the audit report", text)
        self.assertIn("memory_index", text)
        tidy = self.rpc(t, "prompts/get", {"name": "tidy_inbox", "arguments": {"scope": "personal"}})
        self.assertIn('scope="personal"', tidy["result"]["messages"][0]["content"]["text"])

    def test_reading_without_signing_in_is_refused(self):
        r = self.client.post("/mcp", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                                                         "params": {"uri": "khala://guide"}})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()

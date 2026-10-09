"""The connect wizard: every catalog entry renders for this server, links and commands carry its address, and a
token is issued once, behind a fresh identity check, into the channels that need one."""
import html
import re
import unittest

from base import ALICE, Base
from khala import catalog


class ConnectWizardTests(Base):
    def test_every_client_page_renders_with_this_servers_address(self):
        self.web_login(ALICE)
        index = self.client.get("/app/connect").text
        for e in catalog.entries():
            self.assertIn("/app/connect/" + e["id"], index)
            page = self.client.get("/app/connect/" + e["id"])
            self.assertEqual(page.status_code, 200, e["id"])
            if not all(ch.get("needs_token") for ch in e["channels"]):
                self.assertIn("http://localhost/mcp", html.unescape(page.text), e["id"])
            self.assertNotIn("YOUR_TOKEN", page.text, e["id"])
        self.assertEqual(self.client.get("/app/connect/nope").status_code, 404)

    def test_quickest_way_first_and_untested_ways_marked(self):
        self.web_login(ALICE)
        page = html.unescape(self.client.get("/app/connect/cursor").text)
        self.assertIn('href="cursor://anysphere.cursor-deeplink/mcp/install?name=khala&amp;config=', 
                      self.client.get("/app/connect/cursor").text)
        self.assertLess(page.index("Add to Cursor"), page.index("Config file"))
        self.assertIn("untested", self.client.get("/app/connect/vscode").text)
        self.assertNotIn("untested", self.client.get("/app/connect/cursor").text)

    def test_a_token_is_issued_once_into_the_channels_that_need_it(self):
        self.web_login(ALICE)
        r = self.post("/app/connect/jetbrains-ai", {"days": "30"}, page="/app/connect/jetbrains-ai")
        self.assertEqual(r.status_code, 200, r.text)
        token = re.search(r"mem_[A-Za-z0-9_-]+", r.text).group(0)
        self.assertIn('"KHALA_TOKEN": "%s"' % token, html.unescape(r.text))
        self.assertTrue(self.call(token, "memory_scopes"))
        agent = self.db.one("SELECT * FROM agents WHERE name='JetBrains AI Assistant (token)'")
        self.assertEqual((agent["account_id"], agent["kind"]), (self.alice_id, "device"))
        again = self.client.get("/app/connect/jetbrains-ai").text
        self.assertNotIn(token, again)

    def test_creating_a_token_needs_a_fresh_identity_check(self):
        self.web_login(ALICE)
        self.db.q("UPDATE web_sessions SET reauth_at=0")
        r = self.post("/app/connect/cursor", {"days": "30"}, page="/app/connect/cursor")
        self.assertTrue(r.headers["location"].startswith("/app/reauth"))
        self.assertIsNone(self.db.one("SELECT * FROM agents WHERE name='Cursor (token)'"))


if __name__ == "__main__":
    unittest.main()

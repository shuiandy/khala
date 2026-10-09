"""A fresh install: no repository yet, so the server creates it and the first write makes the root commit."""
import os
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from base import OWNER, Base, git, rec
from khala.app import Config, create_app
from khala.login import FakeMailer


class FreshInstallTests(Base):
    def setUp(self):
        super().setUp()
        self.umask = os.umask(0o022)
        os.umask(self.umask)
        tmp = tempfile.TemporaryDirectory(prefix="khala-fresh-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.hub = root / "data" / "vault.git"
        cfg = Config({"KHALA_REPO": str(self.hub), "KHALA_DB": str(root / "k.db"), "KHALA_ISSUER": "http://localhost",
                      "KHALA_OWNERS": OWNER, "KHALA_MAILER": "fake", "KHALA_SECRET_KEY": "test-secret"})
        self.mailer = FakeMailer()
        self.app = create_app(cfg, mailer=self.mailer)
        self.db = self.app.state.db
        self.addCleanup(self.db.conn.close)
        self.client = TestClient(self.app, base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_the_server_starts_on_nothing_and_the_first_write_makes_the_root_commit(self):
        self.assertTrue((self.hub / "HEAD").exists())
        self.assertEqual(self.hub.stat().st_mode & 0o777, 0o777 & ~self.umask)     # follows the umask
        self.assertEqual(self.app.state.store.head(), "")
        self.assertEqual(self.client.get("/health").status_code, 200)

        t = self.login(OWNER)[0]["access_token"]
        scopes = [s["scope"] for s in self.call(t, "memory_scopes") if not s.get("auto_load")]
        self.assertTrue(scopes)
        self.assertEqual(self.call(t, "memory_index", scope=scopes[0]), [])
        self.assertEqual(self.call(t, "memory_search", query="anything"), [])
        self.assertIn("not found", self.call(t, "memory_history", name="project_first.md")["error"])

        r = self.call(t, "memory_write", name="project_first.md", content=rec(scopes[0], body="first fact"))
        self.assertNotIn("error", r)
        self.assertEqual(git(self.hub, "rev-list", "--count", "main"), "1")
        self.assertIn("first fact", self.call(t, "memory_read", name="project_first.md")["content"])
        self.assertEqual(len(self.call(t, "memory_history", name="project_first.md")), 1)

        self.web_login(OWNER)
        self.assertEqual(self.client.get("/app").status_code, 200)
        self.assertEqual(self.client.get("/app/admin").status_code, 200)

    def test_notes_and_the_web_admin_work_before_any_record_exists(self):
        self.web_login(OWNER)
        for page in ("/app", "/app/records", "/app/admin", "/app/review", "/app/audit"):
            self.assertEqual(self.client.get(page).status_code, 200, page)
        t = self.login(OWNER)[0]["access_token"]
        scope = [s["scope"] for s in self.call(t, "memory_scopes") if not s.get("auto_load")][0]
        self.assertIn("id", self.call(t, "memory_note", scope=scope, text="remember this"))


if __name__ == "__main__":
    unittest.main()

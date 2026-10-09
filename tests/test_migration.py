"""Old database (users/grants) migrated in place to v2; old tokens keep working after the migration."""
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from khala.app import Config, create_app
from khala.db import COLUMNS, DB, digest
from khala.login import FakeMailer

V1 = """
CREATE TABLE users (email TEXT PRIMARY KEY, name TEXT NOT NULL, is_owner INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE grants (email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE, scope TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('r', 'rw')), PRIMARY KEY (email, scope));
CREATE TABLE clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE pending (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE login_codes (req TEXT NOT NULL, email TEXT NOT NULL, code_hash TEXT NOT NULL, expires REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (req, email));
CREATE TABLE sends (email TEXT NOT NULL, ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE auth_codes (code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, subject TEXT NOT NULL, data TEXT NOT NULL,
  expires REAL NOT NULL);
CREATE TABLE tokens (token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, subject TEXT NOT NULL,
  scopes TEXT NOT NULL, resource TEXT, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
"""


def legacy_db(path):
    c = sqlite3.connect(path)
    c.executescript(V1)
    now = time.time()
    users = [("owner@mail.example", "Owner", 1, 1.0), ("owner@alt.example", "Owner", 1, 2.0),
             ("owner+helper@mail.example", "Helper", 0, 3.0), ("alice@example.com", "Alice", 0, 4.0)]
    c.executemany("INSERT INTO users VALUES (?,?,?,?)", users)
    c.executemany("INSERT INTO grants VALUES (?,?,?)", [("owner+helper@mail.example", "personal", "rw"),
                                                         ("alice@example.com", "team", "r"),
                                                         ("alice@example.com", "global", "rw")])
    for cid, name in (("c-code", "Claude Code"), ("c-helper", "Helper")):
        c.execute("INSERT INTO clients VALUES (?,?,?)", (cid, json.dumps({"client_id": cid, "client_name": name}), now))
    tokens = [("tok-code", "access", "c-code", "owner@mail.example", "memory", now + 3600, 0),
              ("tok-code-r", "refresh", "c-code", "owner@mail.example", "memory", now + 86400, 0),
              ("tok-fm", "access", "c-code", "owner@alt.example", "memory", now + 3600, 0),
              ("tok-helper", "access", "c-helper", "owner+helper@mail.example", "memory offline_access", now + 3600, 0),
              ("tok-old", "access", "c-code", "owner@mail.example", "memory", now - 10, 0),
              ("tok-dead", "access", "c-code", "owner@mail.example", "memory", now + 3600, 1)]
    for raw, kind, cid, sub, scopes, exp, rev in tokens:
        c.execute("INSERT INTO tokens(token_hash, kind, client_id, subject, scopes, expires, revoked) VALUES (?,?,?,?,?,?,?)",
                  (digest(raw), kind, cid, sub, scopes, exp, rev))
    c.execute("INSERT INTO pending VALUES ('p', 'c-code', '{}', ?)", (now + 600,))
    c.commit()
    c.close()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "m.db"
        legacy_db(self.path)
        self.db = DB(self.path)
        self.addCleanup(self.db.conn.close)

    def test_owners_merge_into_one_admin_account(self):
        accounts = self.db.accounts()
        self.assertEqual(len(accounts), 3)
        owner_acct = self.db.account_by_email("owner@alt.example")
        self.assertEqual(owner_acct["email"], "owner@mail.example")
        self.assertTrue(owner_acct["is_admin"])
        self.assertEqual(self.db.emails(owner_acct["id"]), ["owner@alt.example", "owner@mail.example"])
        self.assertEqual(self.db.account_by_email("owner+helper@mail.example")["handle"], "owner-helper")

    def test_grants_become_roles_and_global_stays_private(self):
        owner_acct = self.db.account_by_email("owner@mail.example")["id"]
        helper = self.db.account_by_email("owner+helper@mail.example")["id"]
        alice = self.db.account_by_email("alice@example.com")["id"]
        self.assertEqual(self.db.grants_for_account(helper), {"personal": "editor"})
        self.assertEqual(self.db.grants_for_account(alice), {"team": "viewer"})
        g = self.db.scope("global")
        self.assertEqual((g["owner_id"], g["auto_load"], g["shareable"]), (owner_acct, 1, 0))
        self.assertEqual(self.db.scope("personal")["owner_id"], owner_acct)
        # migrated old accounts don't get an extra <handle> scope
        self.assertIsNone(self.db.scope("alice"))

    def test_live_tokens_get_one_agent_per_account_and_client(self):
        owner_acct = self.db.account_by_email("owner@mail.example")["id"]
        helper = self.db.account_by_email("owner+helper@mail.example")["id"]
        agents = self.db.agents_for(owner_acct)
        self.assertEqual(len(agents), 1)            # two alias emails, same client: same person, same agent
        self.assertEqual((agents[0]["name"], agents[0]["kind"], agents[0]["ceiling"]), ("Claude Code", "device", None))
        bot = self.db.agents_for(helper)[0]
        self.assertEqual((bot["name"], bot["kind"]), ("Helper", "bot"))
        for raw in ("tok-code", "tok-code-r", "tok-fm"):
            row = self.db.token_row(digest(raw))
            self.assertEqual((row["agent_id"], row["subject"]), (agents[0]["id"], str(owner_acct)))
        self.assertIsNone(self.db.token_row(digest("tok-old")))
        self.assertIsNone(self.db.token_row(digest("tok-dead")))
        self.assertIsNone(self.db.one("SELECT * FROM pending"))

    def test_migration_runs_once(self):
        before = self.db.accounts()
        self.db.conn.close()
        again = DB(self.path)
        self.addCleanup(again.conn.close)
        self.assertEqual([a["id"] for a in again.accounts()], [a["id"] for a in before])
        self.assertEqual(again.one("SELECT COUNT(*) n FROM audit_events WHERE action='instance.migrated'")["n"], 1)

    def test_a_scope_named_global_is_ordinary_on_a_new_instance(self):
        """Only the migration from the first schema gives `global` its old meaning; adopting it from a repository
        makes a normal, shareable scope until its owner turns auto-loading on."""
        db = DB(Path(self.tmp.name) / "fresh.db")
        self.addCleanup(db.conn.close)
        db.create_account("admin@example.com", "Admin", is_admin=True)
        db.adopt_scopes({"global", "notes"})
        self.assertEqual([(s["auto_load"], s["shareable"]) for s in map(db.scope, ("global", "notes"))],
                         [(0, 1), (0, 1)])

    def test_columns_added_later_reach_existing_databases(self):
        """Old live database missing later columns (e.g. pending.browser): add them on open, regardless of version."""
        path = Path(self.tmp.name) / "old.db"
        db = DB(path)
        db.conn.close()
        c = sqlite3.connect(path)
        c.executescript("".join("ALTER TABLE %s DROP COLUMN %s; " % (t, col) for t, col, _ in COLUMNS)
                        + "PRAGMA user_version=4;")
        c.close()
        db = DB(path)
        self.addCleanup(db.conn.close)
        for table, column, _ in COLUMNS:
            self.assertIn(column, {r[1] for r in db.q("PRAGMA table_info(%s)" % table)}, table)
        c = sqlite3.connect(path)
        c.execute("ALTER TABLE pending DROP COLUMN browser")
        c.commit()
        c.close()
        again = DB(path)                     # the version number is already current, add them anyway
        self.addCleanup(again.conn.close)
        self.assertIn("browser", {r[1] for r in again.q("PRAGMA table_info(pending)")})

    def test_failed_migration_rolls_back(self):
        """Migration fails midway: full rollback, old tables intact, the next start can migrate again."""
        path = Path(self.tmp.name) / "broken.db"
        legacy_db(path)
        c = sqlite3.connect(path)
        c.execute("UPDATE clients SET info='not json' WHERE client_id='c-code'")
        c.commit()
        c.close()
        with self.assertRaises(ValueError):
            DB(path)
        c = sqlite3.connect(path)
        self.addCleanup(c.close)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 0)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("users", tables)
        self.assertNotIn("accounts", tables)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM grants").fetchone()[0], 3)


class RestoreGuardTests(unittest.TestCase):
    """Only records restored from Git, empty state database: don't quietly give all scopes to the first admin."""
    def test_empty_state_with_existing_records_refuses_to_start(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        work, hub = root / "w", root / "hub.git"
        work.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        (work / "project_x.md").write_text("---\nname: x\ndescription: d\nmetadata:\n  type: project\n  scope: alice\n"
                                           "  verified: 2026-10-01\n  review_after: 90d\n  source: t\n  status: active\n---\nx\n")
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(work), "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-qm", "seed"], check=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(hub)], check=True)
        env = {"KHALA_REPO": str(hub), "KHALA_DB": str(root / "m.db"), "KHALA_ISSUER": "http://localhost",
               "KHALA_ALLOWED_HOSTS": "localhost", "KHALA_OWNERS": "admin@example.com", "KHALA_MAILER": "fake",
               "KHALA_SECRET_KEY": "k"}
        with self.assertRaises(RuntimeError) as ctx:
            create_app(Config(env), mailer=FakeMailer())
        self.assertIn("RESTORE.md", str(ctx.exception))
        app = create_app(Config(dict(env, KHALA_ADOPT_EXISTING="1")), mailer=FakeMailer())
        self.addCleanup(app.state.db.conn.close)
        self.assertEqual(app.state.db.scope("alice")["owner_id"], app.state.db.primary_admin()["id"])


class MigratedServerTests(unittest.TestCase):
    """Serve straight from the migrated database: tokens issued before the migration can still call tools."""
    def test_old_token_still_works(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        legacy_db(root / "m.db")
        work, hub = root / "w", root / "hub.git"
        work.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        (work / "project_x.md").write_text("---\nname: x\ndescription: d\nmetadata:\n  type: project\n  scope: personal\n"
                                           "  verified: 2026-10-01\n  review_after: 90d\n  source: t\n  status: active\n---\nx\n")
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(work), "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-qm", "seed"], check=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(hub)], check=True)
        app = create_app(Config({"KHALA_REPO": str(hub), "KHALA_DB": str(root / "m.db"),
                                 "KHALA_ISSUER": "http://localhost", "KHALA_ALLOWED_HOSTS": "localhost",
                                 "KHALA_OWNERS": "owner@mail.example,owner@alt.example",
                                 "KHALA_MAILER": "fake", "KHALA_SECRET_KEY": "test-secret", "KHALA_ADOPT_EXISTING": "1"}), mailer=FakeMailer())
        self.addCleanup(app.state.db.conn.close)
        with TestClient(app, base_url="http://localhost") as client:
            r = client.post("/mcp", headers={"Authorization": "Bearer tok-fm",
                                             "Accept": "application/json, text/event-stream"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "memory_scopes", "arguments": {}}})
            self.assertEqual(r.status_code, 200, r.text)
            scopes = {s["scope"] for s in r.json()["result"]["structuredContent"]["result"]}
            self.assertTrue({"personal", "global"} <= scopes)
            self.assertEqual(len(app.state.db.accounts()), 3)       # KHALA_OWNERS at startup created no extra accounts


if __name__ == "__main__":
    unittest.main()

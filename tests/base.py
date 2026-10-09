"""Fixtures: temp Git record repository and database, real OAuth (register, authorize, email code, consent, token)."""
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from khala.app import Config, create_app
from khala.login import FakeMailer

OWNER, ALICE, STRANGER = "owner@example.com", "alice@example.com", "nobody@example.com"
ORIGIN = {"Origin": "http://localhost"}


def rec(scope, status="active", body="body", pinned=False, name="sample"):
    pin = "  pinned: true\n" if pinned else ""
    return ("---\nname: %s\ndescription: %s note\nmetadata:\n  type: project\n  scope: %s\n  verified: 2026-10-01\n"
            "  review_after: 90d\n  source: test\n  status: %s\n%s---\n\n%s\n" % (name, scope, scope, status, pin, body))


def git(repo, *args, **kw):
    r = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True, **kw)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memory-server-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work, self.hub = root / "work", root / "hub.git"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        for k, v in (("user.name", "T"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.work, "config", k, v)
        files = {"project_personal_thing.md": rec("personal", body="SECRET_PERSONAL_DETAIL"),
                 "project_audit.md": rec("team-shared", body="audit rules: compare hire dates"),
                 "project_private_team.md": rec("team", body="salary notes"),
                 "feedback_global_pref.md": rec("global", body="global preference"),
                 "project_pinned.md": rec("team-shared", pinned=True, body="pinned text"),
                 "PROTOCOL.md": "protocol\n", "INDEX.md": "- project_personal_thing\n"}
        for n, t in files.items():
            (self.work / n).write_text(t)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "seed")
        subprocess.run(["git", "clone", "-q", "--bare", str(self.work), str(self.hub)], check=True)
        cfg = Config({"KHALA_REPO": str(self.hub), "KHALA_DB": str(root / "m.db"),
                      "KHALA_ISSUER": "http://localhost", "KHALA_ALLOWED_HOSTS": "localhost",
                      "KHALA_OWNERS": OWNER, "KHALA_OWNER_NAME": "Owner", "KHALA_MAILER": "fake", "KHALA_SECRET_KEY": "test-secret", "KHALA_ADOPT_EXISTING": "1"})
        self.mailer = FakeMailer()
        self.app = create_app(cfg, mailer=self.mailer)
        db = self.app.state.db
        self.addCleanup(db.conn.close)
        self.db = db
        self.owner_id = db.account_by_email(OWNER)["id"]
        self.alice_id = db.create_account(ALICE, "Alice")
        self.app.state.memory.records()          # assign the repository's scopes to the owner
        db.set_auto_load("global", True)          # as its owner would; the name alone no longer does it
        self.app.state.memory.write_policy()
        db.set_grant("team-shared", self.alice_id, "editor", self.owner_id)
        self.client = TestClient(self.app, base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    # ---------- OAuth ----------
    def register(self, redirect="http://127.0.0.1:9999/cb"):
        r = self.client.post("/register", json={"redirect_uris": [redirect], "token_endpoint_auth_method": "none",
                                                "grant_types": ["authorization_code", "refresh_token"],
                                                "response_types": ["code"], "client_name": "Test app"})
        return r

    def start(self, client_id=None):
        client_id = client_id or self.register().json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        r = self.client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "st8", "scope": "memory"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 302, r.text)
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        return client_id, verifier, req

    def verify(self, req, email):
        """The email code step; returns the consent page URL."""
        self.client.post("/login", data={"req": req, "email": email, "action": "send"})
        code = [c for to, c in self.mailer.sent if to == email][-1]
        r = self.client.post("/login", data={"req": req, "email": email, "code": code, "action": "verify"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(r.headers["location"].startswith("/consent?req="))
        return r.headers["location"]

    def consent(self, req, access="all", scopes=None, name="Test agent", kind="device", action="allow"):
        data = {"req": req, "name": name, "kind": kind, "access": access, "action": action}
        for sid, mode in (scopes or {}).items():
            data["scope:" + sid] = mode
        return self.client.post("/consent", data=data, headers=ORIGIN, follow_redirects=False)

    def login(self, email, client_id=None, **consent):
        client_id, verifier, req = self.start(client_id)
        self.assertEqual(self.client.get("/login", params={"req": req}).status_code, 200)
        self.assertEqual(self.client.get(self.verify(req, email)).status_code, 200)
        r = self.consent(req, **consent)
        self.assertEqual(r.status_code, 302, r.text)
        q = parse_qs(urlparse(r.headers["location"]).query)
        self.assertEqual(q["state"], ["st8"])
        tok = self.client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                               "redirect_uri": "http://127.0.0.1:9999/cb", "client_id": client_id,
                                               "code_verifier": verifier})
        self.assertEqual(tok.status_code, 200, tok.text)
        return tok.json(), client_id

    def call(self, token, tool, **args):
        r = self.client.post("/mcp", headers={"Authorization": "Bearer " + token,
                                              "Accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": tool, "arguments": args}})
        self.assertEqual(r.status_code, 200, r.text)
        res = r.json()["result"]
        if res.get("isError"):
            return {"error": res["content"][0]["text"]}
        sc = res.get("structuredContent")
        return sc.get("result", sc) if isinstance(sc, dict) else json.loads(res["content"][0]["text"])

    # ---------- web ----------
    def web_login(self, email):
        self.client.cookies.clear()
        r = self.client.get("/app/login", params={"next": "/app"})
        nonce = re.search(r'name="nonce" value="([^"]+)"', r.text).group(1)
        self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "email": email, "action": "send"})
        code = [c for to, c in self.mailer.sent if to == email][-1]
        r = self.client.post("/app/login", data={"nonce": nonce, "next": "/app", "email": email, "code": code,
                                                 "action": "verify"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        return r

    def csrf(self, path="/app"):
        return re.search(r'name="csrf" value="([^"]+)"', self.client.get(path).text).group(1)

    def post(self, path, data, headers=ORIGIN, page="/app"):
        data = dict(data, csrf=data.get("csrf", self.csrf(page)))
        return self.client.post(path, data=data, headers=headers, follow_redirects=False)

    def push(self, name, text):
        git(self.work, "pull", "-q", str(self.hub), "main")
        (self.work / name).write_text(text)
        git(self.work, "add", name)
        git(self.work, "commit", "-qm", "add " + name)
        git(self.work, "push", "-q", str(self.hub), "HEAD:main")

"""khala serve --stdio: one person on this computer, no server and no sign-in, the same tools and rules. Started as a
real process speaking newline-delimited JSON-RPC, the way a client's MCP config would start it."""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY

from khala import instance
from khala.db import DB
from khala.oauth import Provider


class LocalModeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="khala-local-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        with contextlib.redirect_stdout(io.StringIO()):
            instance.init("http://localhost:8100", "you@example.com", self.root / "data", self.root / "khala.env")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("KHALA_", "MEMORY_", "SMTP_"))}

    def start(self):
        proc = subprocess.Popen([sys.executable, "-m", "khala.cli", "--env", str(self.root / "khala.env"), "serve",
                                 "--stdio"], cwd=self.root, env=self.env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        return proc

    def ask(self, proc, msg_id, method, params):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            self.assertTrue(line, proc.stderr.read() if proc.poll() is not None else "no reply")
            reply = json.loads(line)
            if reply.get("id") == msg_id:
                return reply

    def tool(self, proc, msg_id, tool_name, **args):
        reply = self.ask(proc, msg_id, "tools/call", {"name": tool_name, "arguments": args})["result"]
        self.assertFalse(reply.get("isError"), reply)
        sc = reply.get("structuredContent")
        return sc.get("result", sc) if isinstance(sc, dict) else json.loads(reply["content"][0]["text"])

    def test_tools_work_without_signing_in_and_are_attributed_to_this_computer(self):
        proc = self.start()
        init = self.ask(proc, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "test", "version": "1"}})
        self.assertIn("serverInfo", init["result"])
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        scopes = [s["scope"] for s in self.tool(proc, 2, "memory_scopes")]
        self.assertIn("you", scopes)
        record = ("---\nname: first\ndescription: d\nmetadata:\n  type: project\n  scope: you\n  verified: 2026-10-01\n"
                  "  review_after: 90d\n  source: test\n  status: active\n---\n\nlocal fact\n")
        self.tool(proc, 3, "memory_write", name="project_first.md", content=record)
        self.assertIn("local fact", self.tool(proc, 4, "memory_read", name="project_first.md")["content"])
        trailer = subprocess.run(["git", "--git-dir", str(self.root / "data" / "vault.git"), "log", "-1",
                                  "--format=%B"], capture_output=True, text=True).stdout
        self.assertIn("Agent: This computer (stdio)", trailer)

    def test_revoking_the_local_agent_turns_local_access_off(self):
        proc = self.start()
        self.ask(proc, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "1"}})
        proc.kill()
        db = DB(self.root / "data" / "state" / "khala.db")
        self.addCleanup(db.conn.close)
        agent = db.one("SELECT id FROM agents WHERE oauth_client_id='local-stdio'")
        db.revoke_agent(agent["id"])
        again = subprocess.run([sys.executable, "-m", "khala.cli", "--env", str(self.root / "khala.env"), "serve",
                                "--stdio"], cwd=self.root, env=self.env, input="", capture_output=True, text=True,
                               timeout=30)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("was revoked", again.stderr)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM agents WHERE oauth_client_id='local-stdio'")["n"], 1)


class BridgeTests(unittest.TestCase):
    """khala bridge between a stdio client and a real HTTP server process, with a bot token."""
    def test_a_stdio_client_reaches_the_remote_server_through_the_bridge(self):
        tmp = tempfile.TemporaryDirectory(prefix="khala-bridge-")
        self.addCleanup(tmp.cleanup)
        root, port = Path(tmp.name).resolve(), free_port()
        with contextlib.redirect_stdout(io.StringIO()):
            instance.init("http://127.0.0.1:%d" % port, "you@example.com", root / "data", root / "khala.env")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KHALA_", "MEMORY_", "SMTP_"))}
        db = DB(root / "data" / "state" / "khala.db")
        account = db.account_by_email("you@example.com")
        agent_id = db.create_agent(account["id"], "Bridge test", "bot")
        token = Provider(db, "http://127.0.0.1:%d" % port).issue_bearer(account["id"], agent_id, 1)
        db.conn.close()
        server = subprocess.Popen([sys.executable, "-m", "khala.cli", "--env", str(root / "khala.env"), "serve",
                                   "--port", str(port)], cwd=root, env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        self.addCleanup(server.wait, 10)
        self.addCleanup(server.terminate)
        wait_for(port)
        url = "http://127.0.0.1:%d/mcp" % port
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                     "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_scopes",
                                                                                "arguments": {}}}]
        stdin = "".join(json.dumps(m) + "\n" for m in lines)
        good = subprocess.run([sys.executable, "-m", "khala.cli", "bridge", url], input=stdin, capture_output=True,
                              text=True, timeout=60, cwd=root, env=dict(env, KHALA_TOKEN=token))
        replies = {m["id"]: m for m in map(json.loads, good.stdout.splitlines())}
        self.assertEqual(sorted(replies), [1, 2], good.stderr)
        self.assertIn("serverInfo", replies[1]["result"])
        self.assertIn('"scope":"you"', json.dumps(replies[2]["result"], separators=(",", ":")))
        # 2026-07-28: no initialize; each request names its version in _meta, and the bridge sets the routing headers
        meta = {PROTOCOL_VERSION_META_KEY: "2026-07-28", CLIENT_CAPABILITIES_META_KEY: {}}
        modern = [{"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
                      "name": "memory_scopes", "arguments": {}, "_meta": meta}},
                  {"jsonrpc": "2.0", "id": 8, "method": "resources/read", "params": {
                      "uri": "khala://guide", "_meta": meta}}]
        new = subprocess.run([sys.executable, "-m", "khala.cli", "bridge", url], capture_output=True, text=True,
                             input="".join(json.dumps(m) + "\n" for m in modern), timeout=60, cwd=root,
                             env=dict(env, KHALA_TOKEN=token))
        replies = {m["id"]: m for m in map(json.loads, new.stdout.splitlines())}
        self.assertEqual(sorted(replies), [7, 8], new.stdout + new.stderr)
        self.assertNotIn("error", replies[7], replies[7])
        self.assertIn('"scope":"you"', json.dumps(replies[7]["result"], separators=(",", ":")))
        self.assertIn("memory_note", json.dumps(replies[8]["result"]))
        bad = subprocess.run([sys.executable, "-m", "khala.cli", "bridge", url], input=stdin, capture_output=True,
                             text=True, timeout=60, cwd=root, env=dict(env, KHALA_TOKEN="mem_wrong"))
        errors = [json.loads(line) for line in bad.stdout.splitlines()]
        self.assertEqual([e["id"] for e in errors], [1, 2])
        self.assertIn("refused the token", errors[0]["error"]["message"])
        missing = subprocess.run([sys.executable, "-m", "khala.cli", "bridge", url], input="", capture_output=True,
                                 text=True, timeout=30, cwd=root, env=env)
        self.assertIn("KHALA_TOKEN", missing.stderr)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(port):
    for _ in range(100):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2):
                return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    raise AssertionError("server did not start")


if __name__ == "__main__":
    unittest.main()

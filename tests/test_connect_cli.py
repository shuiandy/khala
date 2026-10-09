"""khala connect: the first way this computer can carry out (a client's command, a JSON config merge, an install link),
steps otherwise, and a token through device authorization when one is needed. The end-to-end test runs a real
server and a real `khala connect`, approving the code the way the web page would."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from khala import catalog, connect, instance
from khala.db import DB
from khala.device import DeviceFlow
from khala.oauth import Provider

from test_local import free_port, wait_for

URL = "https://memory.example.com/mcp"


class ConnectTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="khala-connect-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.said = []

    def channels(self, client, token=None):
        return connect.plan(catalog.by_id(catalog.entries(), client), URL, "khala", token)

    def test_the_server_address_is_normalised(self):
        self.assertEqual(connect.endpoint("https://memory.example.com"), URL)
        self.assertEqual(connect.endpoint("https://memory.example.com/mcp/"), URL)
        with self.assertRaises(connect.ConnectError):
            connect.endpoint("memory.example.com")

    def test_merging_keeps_other_servers_and_a_backup(self):
        path = self.home / ".cursor" / "mcp.json"
        self.assertIsNone(connect.merge_json(path, ["mcpServers", "khala"], {"url": URL}))
        path.write_text(json.dumps({"mcpServers": {"other": {"url": "x"}}, "theme": "dark"}))
        backup = connect.merge_json(path, ["mcpServers", "khala"], {"url": URL})
        self.assertEqual(json.loads(path.read_text()),
                         {"mcpServers": {"other": {"url": "x"}, "khala": {"url": URL}}, "theme": "dark"})
        self.assertIn('"other"', backup.read_text())
        path.write_text('{ // a comment\n "mcpServers": {} }')
        with self.assertRaises(connect.ConnectError):
            connect.merge_json(path, ["mcpServers", "khala"], {"url": URL})

    def test_an_installed_clients_own_command_comes_first(self):
        ran = []
        done = connect.carry_out(self.channels("claude-code"), say=self.said.append, which=lambda b: "/usr/bin/" + b,
                                 run=lambda args: ran.append(args) or SimpleNamespace(returncode=0))
        self.assertEqual(done, "command")
        self.assertEqual(ran, [["claude", "mcp", "add", "--transport", "http", "--scope", "user", "khala", URL]])

    def test_without_the_program_the_config_file_is_merged(self):
        opened = []
        done = connect.carry_out(self.channels("cursor"), say=self.said.append, which=lambda b: None,
                                 open_link=opened.append)
        self.assertEqual(done, "link")                          # Cursor's install link is its first channel
        self.assertTrue(opened[0].startswith("cursor://anysphere.cursor-deeplink/mcp/install?"))
        done = connect.carry_out(self.channels("gemini-cli"), say=self.said.append, which=lambda b: None)
        self.assertEqual(done, "file")
        settings = json.loads((self.home / ".gemini" / "settings.json").read_text())
        self.assertEqual(settings["mcpServers"]["khala"], {"url": URL, "type": "http"})

    def test_declining_changes_nothing_and_dry_runs_only_show(self):
        done = connect.carry_out(self.channels("gemini-cli"), say=self.said.append, which=lambda b: None,
                                 confirm=lambda q: False)
        self.assertEqual(done, "shown")
        self.assertFalse((self.home / ".gemini").exists())
        done = connect.carry_out(self.channels("claude-code"), say=self.said.append, dry_run=True,
                                 which=lambda b: "/x", run=lambda a: self.fail("dry run ran it"))
        self.assertEqual(done, "shown")

    def test_clients_with_nothing_to_carry_out_get_steps(self):
        done = connect.carry_out(self.channels("chatgpt"), say=self.said.append, which=lambda b: None)
        self.assertEqual(done, "steps")
        self.assertTrue(any("chatgpt.com/plugins" in line for line in self.said))


class DeviceConnectTests(unittest.TestCase):
    def test_a_token_reaches_the_config_file_without_copying(self):
        tmp = tempfile.TemporaryDirectory(prefix="khala-connect-e2e-")
        self.addCleanup(tmp.cleanup)
        root, port = Path(tmp.name).resolve(), free_port()
        base = "http://127.0.0.1:%d" % port
        with contextlib.redirect_stdout(io.StringIO()):
            instance.init(base, "you@example.com", root / "data", root / "khala.env")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KHALA_", "MEMORY_", "SMTP_"))}
        server = subprocess.Popen([sys.executable, "-m", "khala.cli", "--env", str(root / "khala.env"), "serve",
                                   "--port", str(port)], cwd=root, env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        self.addCleanup(server.wait, 10)
        self.addCleanup(server.terminate)
        wait_for(port)
        home = root / "home"
        home.mkdir()
        cli = subprocess.Popen([sys.executable, "-m", "khala.cli", "connect", "cursor", "--url", base, "--token",
                                "--yes", "--no-browser"], cwd=root, env=dict(env, HOME=str(home)),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(cli.kill)
        first = cli.stdout.readline()
        code = re.search(r"enter the code ([A-Z]{4}-[A-Z]{4})", first).group(1)
        db = DB(root / "data" / "state" / "khala.db")
        self.addCleanup(db.conn.close)
        account = db.account_by_email("you@example.com")
        self.assertIsNotNone(DeviceFlow(db, Provider(db, base), base).decide(code, account["id"], True,
                                                                              name="Cursor", days=30))
        out, err = cli.communicate(timeout=60)
        self.assertEqual(cli.returncode, 0, err)
        config = json.loads((home / ".cursor" / "mcp.json").read_text())
        header = config["mcpServers"]["khala"]["headers"]["Authorization"]
        self.assertRegex(header, r"^Bearer mem_")
        self.assertEqual(config["mcpServers"]["khala"]["url"], base + "/mcp")
        self.assertEqual((home / ".cursor" / "mcp.json").stat().st_mode & 0o777, 0o600)     # it holds a token
        self.assertEqual(db.one("SELECT COUNT(*) n FROM tokens WHERE kind='bearer'")["n"], 1)


if __name__ == "__main__":
    unittest.main()

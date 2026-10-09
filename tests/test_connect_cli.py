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


    def test_local_paths_are_absolute_and_found_without_an_active_virtualenv(self):
        env = self.home / "khala.env"
        env.write_text("KHALA_ISSUER=http://localhost:8100\n")
        program = self.home / "venv" / "bin" / "khala"
        program.parent.mkdir(parents=True)
        program.write_text("")
        got = connect.local_paths(env, which=lambda b: None, argv0=str(program))
        self.assertEqual(got, {"khala": str(program), "env": str(env)})
        with self.assertRaises(connect.ConnectError):
            connect.local_paths(env, which=lambda b: None, argv0="/usr/bin/python3")
        with self.assertRaises(connect.ConnectError):
            connect.local_paths(self.home / "missing.env", which=lambda b: "/x/khala")

    def test_local_mode_writes_the_stdio_command(self):
        local = {"khala": "/opt/my tools/khala", "env": str(self.home / "khala.env")}
        ran = []
        channels = connect.plan(catalog.by_id(catalog.entries(), "claude-code"), "", "khala", None, local=local)
        connect.carry_out(channels, say=self.said.append, which=lambda b: "/usr/bin/" + b,
                          run=lambda args: ran.append(args) or SimpleNamespace(returncode=0))
        self.assertEqual(ran, [["claude", "mcp", "add", "--transport", "stdio", "--scope", "user", "khala", "--", local["khala"], "--env",
                                local["env"], "serve", "--stdio"]])
        channels = connect.plan(catalog.by_id(catalog.entries(), "gemini-cli"), "", "khala", None, local=local)
        self.assertEqual(connect.carry_out(channels, say=self.said.append, which=lambda b: None), "file")
        settings = json.loads((self.home / ".gemini" / "settings.json").read_text())
        self.assertEqual(settings["mcpServers"]["khala"],
                         {"command": local["khala"], "args": ["--env", local["env"], "serve", "--stdio"]})


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


class LocalConnectTests(unittest.TestCase):
    def test_the_written_config_starts_a_working_server_from_anywhere(self):
        """khala connect --local, then start the server exactly as the client's config says, from another
        directory, and use it."""
        tmp = tempfile.TemporaryDirectory(prefix="khala-connect-local-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        with contextlib.redirect_stdout(io.StringIO()):
            instance.init("http://localhost:8100", "you@example.com", root / "data", root / "khala.env")
        bin_dir, home = root / "bin", root / "home"
        bin_dir.mkdir()
        home.mkdir()
        shim = bin_dir / "khala"                        # stands in for the installed program
        shim.write_text('#!/bin/sh\nexec "%s" -m khala.cli "$@"\n' % sys.executable)
        shim.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KHALA_", "MEMORY_", "SMTP_"))}
        env.update(HOME=str(home), PATH=str(bin_dir) + os.pathsep + env.get("PATH", ""))
        done = subprocess.run([sys.executable, "-m", "khala.cli", "connect", "gemini-cli", "--local", "--yes"],
                              cwd=root, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        server = json.loads((home / ".gemini" / "settings.json").read_text())["mcpServers"]["khala"]
        self.assertEqual(server, {"command": str(shim), "args": ["--env", str(root / "khala.env"), "serve",
                                                                 "--stdio"]})
        proc = subprocess.Popen([server["command"]] + server["args"], cwd=home, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "memory_scopes", "arguments": {}}}):
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            self.assertTrue(line, proc.stderr.read() if proc.poll() is not None else "no reply")
            reply = json.loads(line)
            if reply.get("id") == 2:
                break
        self.assertFalse(reply["result"].get("isError"), reply)
        self.assertIn("you", json.dumps(reply["result"]))


if __name__ == "__main__":
    unittest.main()

"""The client catalog: every entry renders, every link decodes back to the configuration it was built from, tokens
appear only where a channel asks for one, and signing-in clients are recognised with the right strength."""
import base64
import json
import re
import shlex
import tomllib
import unittest
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from khala import catalog

URL, NAME, TOKEN = "https://memory.example.com/mcp", "khala", "mem_secret_value"
LEFTOVER = re.compile(r"\{(url|name|token|url_q|name_q|config_json_q|config_b64|config_b64_q|config_b64url)\}")


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = catalog.load()

    def channels(self, client, token=None):
        return catalog.render(catalog.by_id(self.entries, client), URL, NAME, token)

    def link(self, client):
        return next(ch["text"] for ch in self.channels(client) if ch["type"] == "link")

    def test_every_channel_renders_completely(self):
        for e in self.entries:
            for ch in catalog.render(e, URL, NAME, TOKEN):
                text = json.dumps(ch)
                self.assertIsNone(LEFTOVER.search(text), (e["id"], ch["label"]))
                if ch["type"] == "file" and ch["format"] == "json":
                    self.assertEqual(json.loads(ch["text"]), catalog._nest(ch["merge"], ch["entry"]))
                if ch.get("text", "").lstrip().startswith("{"):
                    json.loads(ch["text"])

    def test_local_channels_start_khala_by_absolute_paths_and_stay_apart(self):
        local = {"khala": "C:\\Users\\me\\My Tools\\khala.exe", "env": "/Users/me/khala data/khala.env"}

        def strings(value):
            if isinstance(value, dict):
                return [s for v in value.values() for s in strings(v)]
            if isinstance(value, list):
                return [s for v in value for s in strings(v)]
            return [value] if isinstance(value, str) else []

        for e in self.entries:
            remote = catalog.render(e, URL, NAME, TOKEN)
            self.assertFalse(any(ch["local"] for ch in remote), e["id"])
            for ch in catalog.render(e, "", NAME, local=local):
                text = json.dumps(ch)
                self.assertTrue(ch["local"] and not ch["needs_token"], (e["id"], ch["label"]))
                self.assertIsNone(LEFTOVER.search(text), (e["id"], ch["label"]))
                self.assertNotIn("{khala}", text)
                self.assertNotIn("{env}", text)
                if ch["type"] == "command":             # a shell splits it back into the same arguments
                    args = shlex.split(ch["text"])
                    self.assertIn(local["khala"], args)
                    self.assertEqual(args[args.index(local["khala"]) + 1:],
                                     ["--env", local["env"], "serve", "--stdio"])
                parsed = (yaml.safe_load(ch["text"]) if ch.get("format") == "yaml" else
                          tomllib.loads(ch["text"]) if ch.get("format") == "toml" else
                          json.loads(ch["text"]) if ch["type"] in ("file", "connector") else None)
                if parsed is not None:                  # paths survive the format's own quoting and escapes
                    self.assertIn(local["khala"], strings(parsed), (e["id"], ch["label"]))
                    self.assertIn(local["env"], strings(parsed), (e["id"], ch["label"]))
                if ch["type"] == "file" and ch["format"] == "json":
                    self.assertEqual(parsed, catalog._nest(ch["merge"], ch["entry"]))

    def test_tokens_appear_only_where_a_channel_asks(self):
        for e in self.entries:
            for ch in catalog.render(e, URL, NAME, TOKEN):
                self.assertEqual(TOKEN in json.dumps(ch), ch["needs_token"], (e["id"], ch["label"]))

    def test_links_decode_back_to_their_configuration(self):
        q = lambda link: parse_qs(urlparse(link).query)
        cursor = q(self.link("cursor"))
        self.assertEqual(cursor["name"], [NAME])
        self.assertEqual(json.loads(base64.b64decode(cursor["config"][0])), {"url": URL})
        vscode = self.link("vscode")
        self.assertTrue(vscode.startswith("vscode:mcp/install?"))
        self.assertEqual(json.loads(unquote(vscode.split("?", 1)[1])), {"name": NAME, "type": "http", "url": URL})
        hermes = q(self.link("hermes"))["config"][0]
        self.assertEqual(json.loads(base64.urlsafe_b64decode(hermes + "=" * (-len(hermes) % 4))),
                         {"url": URL, "auth": "oauth"})
        self.assertNotIn("=", hermes)
        lm = q(self.link("lm-studio"))
        self.assertEqual(json.loads(base64.b64decode(lm["config"][0])), {"url": URL})
        kiro = q(self.link("kiro"))
        self.assertEqual(json.loads(kiro["config"][0]), {"url": URL, "disabled": False})
        claude = q(self.link("claude"))
        self.assertEqual((claude["connectorUrl"], claude["connectorName"], claude["modal"]),
                         ([URL], [NAME], ["add-custom-connector"]))
        goose = q(self.link("goose"))
        self.assertEqual((goose["url"], goose["type"], goose["name"]), ([URL], ["streamable_http"], [NAME]))

    def test_commands_name_their_binary_and_the_server(self):
        for e in self.entries:
            for ch in catalog.render(e, URL, NAME):
                if ch["type"] == "command":
                    self.assertTrue(ch["text"].startswith(ch["binary"] + " "), ch["text"])
                    self.assertIn(URL, ch["text"])

    def test_signing_in_clients_are_recognised(self):
        e, strong = catalog.identify(self.entries, "https://claude.ai/oauth/claude-code-client-metadata")
        self.assertEqual((e["id"], strong), ("claude-code", True))
        e, strong = catalog.identify(self.entries, "https://chatgpt.com/oauth/abc123/client.json")
        self.assertEqual((e["id"], strong), ("chatgpt", True))
        e, strong = catalog.identify(self.entries, "https://chatgpt.com/oauth/codex/xyz/client.json")
        self.assertEqual((e["id"], strong), ("codex", True))
        e, strong = catalog.identify(self.entries, "4f1c-registered-id", "codex")
        self.assertEqual((e["id"], strong), ("codex", False))
        self.assertEqual(catalog.identify(self.entries, "x", "Unknown thing"), (None, False))
        self.assertEqual(catalog.identify(self.entries, "https://evil.example/claude-code", "Claude Code")[1], False)

    def test_the_validator_catches_mistakes(self):
        good = catalog.by_id(self.entries, "cursor")
        for broken in (
                dict(good, id="Bad Id"),
                dict(good, channels=[{"type": "command", "label": "x", "template": "a {url}", "confidence": "verified"}]),
                dict(good, channels=[{"type": "link", "label": "x", "template": "a {token}", "confidence": "verified"}]),
                dict(good, channels=[{"type": "file", "label": "x", "format": "json", "path": {"all": "a"},
                                      "confidence": "verified"}]),
                dict(good, channels=[{"type": "link", "label": "x", "template": "a"}]),
                dict(good, channels=[{"type": "command", "label": "x", "binary": "a", "local": True,
                                      "template": "a {khala} --env {env} serve --stdio {url}",
                                      "confidence": "verified"}])):
            with self.assertRaises(catalog.CatalogError):
                catalog.validate([broken])
        with self.assertRaises(catalog.CatalogError):
            catalog.validate([good, good])


if __name__ == "__main__":
    unittest.main()

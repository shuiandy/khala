"""khala serve believes X-Forwarded-For only from KHALA_TRUSTED_PROXIES. A real server process is started, because the
rewrite happens in uvicorn, outside the app; the address it settles on is the one sign-in rate limits record."""
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProxyTrustTests(unittest.TestCase):
    def source_seen(self, trusted):
        """Start khala serve, send a sign-in code request that claims to come from 1.2.3.4, return what it recorded."""
        tmp = tempfile.TemporaryDirectory(prefix="khala-proxy-")
        self.addCleanup(tmp.cleanup)
        port, db = free_port(), Path(tmp.name) / "k.db"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KHALA_", "MEMORY_", "FORWARDED_"))}
        env.update(KHALA_ISSUER="http://127.0.0.1:%d" % port, KHALA_REPO=str(Path(tmp.name) / "v.git"),
                   KHALA_DB=str(db), KHALA_SECRET_KEY="test", KHALA_MAILER="log", KHALA_OWNERS="a@example.com")
        if trusted is not None:
            env["KHALA_TRUSTED_PROXIES"] = trusted
        proc = subprocess.Popen([sys.executable, "-m", "khala.cli", "serve", "--port", str(port)], env=env,
                                cwd=tmp.name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        base = "http://127.0.0.1:%d" % port
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        for _ in range(100):
            try:
                if opener.open(base + "/health", timeout=5).status == 200:
                    break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        else:
            self.fail("server did not start")
        page = opener.open(base + "/app/login", timeout=5).read().decode()
        nonce = re.search(r'name="nonce" value="([^"]+)"', page).group(1)
        body = urllib.parse.urlencode({"nonce": nonce, "next": "/app", "email": "x@example.com",
                                       "action": "send"}).encode()
        opener.open(urllib.request.Request(base + "/app/login", data=body, headers={
            "Origin": base, "Sec-Fetch-Site": "same-origin", "X-Forwarded-For": "1.2.3.4"}), timeout=5).read()
        with sqlite3.connect(db) as c:
            return [r[0] for r in c.execute("SELECT ip FROM sends")]

    def test_the_local_proxy_is_trusted_by_default(self):
        self.assertEqual(self.source_seen(None), ["1.2.3.4"])

    def test_an_empty_setting_trusts_no_proxy(self):
        self.assertEqual(self.source_seen(""), ["127.0.0.1"])

    def test_headers_from_an_untrusted_peer_are_ignored(self):
        self.assertEqual(self.source_seen("10.9.9.9"), ["127.0.0.1"])


if __name__ == "__main__":
    unittest.main()

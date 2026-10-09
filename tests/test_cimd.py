"""Client ID metadata documents, loopback redirects on any port (RFC 8252), and iss on authorization responses
(RFC 9207). The document fetch is replaced by a dict; the network guard is tested on its own."""
import base64
import hashlib
import secrets
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from base import ALICE, ORIGIN, Base
from khala import cimd
from khala.oauth import SCOPES, redirect_allowed

URL = "https://client.example/oauth/meta.json"
DOC = {"client_id": URL, "client_name": "Example Agent", "redirect_uris": ["http://127.0.0.1/callback"],
       "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"]}


class CimdFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.docs = {URL: DOC}
        self.fetched = []

        def fetch(url):
            self.fetched.append(url)
            if url not in self.docs:
                raise cimd.CimdError("not found")
            return dict(self.docs[url]), 3600
        self.app.state.provider.documents = cimd.Documents(redirect_allowed, SCOPES, fetcher=fetch)

    def authorize(self, client_id=URL, redirect="http://127.0.0.1:53111/callback"):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        r = self.client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "s1", "scope": "memory"}, follow_redirects=False)
        return r, verifier

    def test_metadata_advertises_documents_public_clients_and_iss(self):
        m = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertTrue(m["client_id_metadata_document_supported"])
        self.assertTrue(m["authorization_response_iss_parameter_supported"])
        self.assertIn("none", m["token_endpoint_auth_methods_supported"])
        self.assertEqual(m["code_challenge_methods_supported"], ["S256"])
        self.assertTrue(m["registration_endpoint"].endswith("/register"))       # registration stays for older clients

    def test_a_client_identified_by_its_document_signs_in_on_any_loopback_port(self):
        r, verifier = self.authorize()
        self.assertEqual(r.status_code, 302, r.text)
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        consent_page = self.client.get(self.verify(req, ALICE)).text
        self.assertIn("Example Agent", consent_page)
        r = self.consent(req, name="")                          # left blank: the document's name is used
        back = urlparse(r.headers["location"])
        q = parse_qs(back.query)
        self.assertEqual((back.port, q["state"], q["iss"]), (53111, ["s1"], ["http://localhost"]))
        tok = self.client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                               "redirect_uri": "http://127.0.0.1:53111/callback", "client_id": URL,
                                               "code_verifier": verifier})
        self.assertEqual(tok.status_code, 200, tok.text)
        self.assertTrue(self.call(tok.json()["access_token"], "memory_scopes"))
        agent = self.db.agent_for_client(self.alice_id, URL)
        self.assertEqual(agent["name"], "Example Agent")
        self.assertEqual(self.fetched, [URL])                    # cached after the first fetch

    def test_documents_that_do_not_hold_up_are_refused(self):
        for doc in (dict(DOC, client_id="https://other.example/meta.json"),
                    dict(DOC, redirect_uris=["http://evil.example/callback"]),
                    dict(DOC, token_endpoint_auth_method="client_secret_basic")):
            self.app.state.provider.documents.cache.clear()
            self.docs[URL] = doc
            r, _ = self.authorize()
            self.assertNotEqual(r.status_code, 302, doc)
        self.docs[URL] = DOC
        self.app.state.provider.documents.cache.clear()
        for redirect in ("http://127.0.0.1:53111/elsewhere", "https://evil.example/callback"):
            r, _ = self.authorize(redirect=redirect)
            self.assertNotEqual(r.status_code, 302, redirect)
        r, _ = self.authorize(client_id="https://missing.example/meta.json")
        self.assertNotEqual(r.status_code, 302)

    def test_the_consent_page_names_known_clients_and_shows_an_older_connection(self):
        code_url = "https://claude.ai/oauth/claude-code-client-metadata"
        self.docs[code_url] = dict(DOC, client_id=code_url, client_name="Claude Code")
        old = self.client.post("/register", json={"redirect_uris": ["http://127.0.0.1:9999/cb"], "client_name":
                                                  "Claude Code", "token_endpoint_auth_method": "none",
                                                  "grant_types": ["authorization_code", "refresh_token"],
                                                  "response_types": ["code"]}).json()["client_id"]
        _, verifier, req = self.start(old)                     # the same person connected earlier by registration
        page = self.client.get(self.verify(req, ALICE)).text
        self.assertIn("that is its own claim", page)
        self.assertIn('value="Claude Code"', page)
        self.consent(req, name="Laptop · Claude Code")
        r, _ = self.authorize(client_id=code_url)
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        page = self.client.get(self.verify(req, ALICE)).text
        self.assertIn("Identified as <b>Claude Code</b> by the metadata it publishes", page)
        self.assertIn("You already connected Claude Code as “Laptop · Claude Code”", page)

    def test_a_denial_carries_iss_too(self):
        r, _ = self.authorize()
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        self.client.get(self.verify(req, ALICE))
        r = self.consent(req, action="deny")
        q = parse_qs(urlparse(r.headers["location"]).query)
        self.assertEqual((q["error"], q["iss"]), (["access_denied"], ["http://localhost"]))

    def test_registered_clients_also_get_any_loopback_port(self):
        client_id = self.register(redirect="http://127.0.0.1:9999/cb").json()["client_id"]
        r, _ = self.authorize(client_id=client_id, redirect="http://localhost:41000/cb")
        self.assertEqual(r.status_code, 302, r.text)
        r, _ = self.authorize(client_id=client_id, redirect="http://127.0.0.1:41000/other")
        self.assertNotEqual(r.status_code, 302)


class FetchGuardTests(unittest.TestCase):
    def test_only_https_urls_with_a_path(self):
        for url in ("http://client.example/meta.json", "https://client.example", "https://client.example/",
                    "https://user:pw@client.example/meta.json", "https://client.example/meta.json#x"):
            with self.assertRaises(cimd.CimdError, msg=url):
                cimd.check_url(url)
        cimd.check_url(URL)

    def test_private_and_local_addresses_are_refused(self):
        for addresses in (["127.0.0.1"], ["10.0.0.5"], ["169.254.169.254"], ["93.184.216.34", "192.168.1.1"],
                          ["::1"], ["fd00::1"]):
            infos = [(None, None, None, "", (a, 443)) for a in addresses]
            with mock.patch.object(cimd.socket, "getaddrinfo", return_value=infos):
                with self.assertRaises(cimd.CimdError, msg=addresses):
                    cimd.public_addresses("client.example", 443)
        infos = [(None, None, None, "", ("93.184.216.34", 443))]
        with mock.patch.object(cimd.socket, "getaddrinfo", return_value=infos):
            self.assertEqual(cimd.public_addresses("client.example", 443), ["93.184.216.34"])

    def test_cache_ages_stay_in_bounds(self):
        self.assertEqual(cimd.max_age("max-age=10"), cimd.MIN_AGE)
        self.assertEqual(cimd.max_age("max-age=999999"), cimd.MAX_AGE)
        self.assertEqual(cimd.max_age(None), cimd.DEFAULT_AGE)
        self.assertEqual(cimd.max_age("no-store"), cimd.MIN_AGE)

    def test_loopback_matching(self):
        reg = ["http://127.0.0.1/callback", "https://app.example/cb"]
        self.assertTrue(cimd.redirect_matches("http://localhost:8000/callback", reg))
        self.assertTrue(cimd.redirect_matches("https://app.example/cb", reg))
        self.assertFalse(cimd.redirect_matches("https://app.example:8443/cb", reg))
        self.assertFalse(cimd.redirect_matches("http://127.0.0.1:8000/callback?x=1", reg))
        self.assertFalse(cimd.redirect_matches("http://10.0.0.1/callback", reg))

    def test_a_failed_refetch_serves_the_cached_document_for_a_while(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if len(calls) > 1:
                raise cimd.CimdError("down")
            return dict(DOC), 300
        docs = cimd.Documents(redirect_allowed, SCOPES, fetcher=fetch)
        first = docs.get(URL)
        expires, client = docs.cache[URL]
        docs.cache[URL] = (expires - 400, client)              # past its age, within the stale allowance
        self.assertIs(docs.get(URL), first)
        docs.cache[URL] = (expires - 400 - cimd.STALE_OK, client)
        with self.assertRaises(cimd.CimdError):
            docs.get(URL)


if __name__ == "__main__":
    unittest.main()

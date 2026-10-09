"""Client ID Metadata Documents (the MCP authorization spec's preferred way for clients to identify themselves): the
client_id is an https URL, and the document there describes the client. Fetching a URL a stranger names is the risky
part, so the fetch only goes to public addresses, connects to the address it checked (no DNS rebinding in between),
follows no redirects, reads at most 16 KB and gives up after five seconds.

Also here: the client model the server hands to the SDK, which matches loopback redirect URIs with any port, as
RFC 8252 section 7.3 asks for native apps.
"""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull

MAX_BYTES = 16 * 1024
TIMEOUT = 5
MIN_AGE, DEFAULT_AGE, MAX_AGE = 300, 3600, 86400
STALE_OK = 86400                        # a cached document may serve this long past its age when a refetch fails
LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


class CimdError(Exception):
    pass


def _loopback(parsed):
    return parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK


def redirect_matches(uri, registered):
    """Exact match, or for loopback http URIs the same path and query on any port and any loopback name."""
    if uri in registered:
        return True
    p = urlparse(uri)
    if not _loopback(p):
        return False
    return any(_loopback(r) and r.path == p.path and r.query == p.query for r in map(urlparse, registered))


class Client(OAuthClientInformationFull):
    def validate_redirect_uri(self, redirect_uri):
        if redirect_uri is not None and redirect_matches(str(redirect_uri), [str(u) for u in self.redirect_uris or []]):
            return redirect_uri
        if redirect_uri is not None:
            raise InvalidRedirectUriError("Redirect URI '%s' not registered for client" % redirect_uri)
        return super().validate_redirect_uri(redirect_uri)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connect to an address already checked, while verifying the certificate for the host name."""
    def __init__(self, host, port, address, **kw):
        super().__init__(host, port, **kw)
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def check_url(url):
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
        raise CimdError("client_id must be an https URL without credentials or fragment")
    if p.path in ("", "/"):
        raise CimdError("client_id must have a path")
    return p


def public_addresses(host, port):
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CimdError("cannot resolve %s: %s" % (host, exc))
    addresses = sorted({info[4][0] for info in infos})
    if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        raise CimdError("%s does not resolve to public addresses only" % host)
    return addresses


def max_age(cache_control):
    m = re.search(r"max-age=(\d+)", cache_control or "")
    age = int(m.group(1)) if m else DEFAULT_AGE
    if "no-store" in (cache_control or "") or "no-cache" in (cache_control or ""):
        age = MIN_AGE
    return min(max(age, MIN_AGE), MAX_AGE)


def fetch(url):
    """The document as a dict, and how long it may be cached."""
    p = check_url(url)
    port = p.port or 443
    address = public_addresses(p.hostname, port)[0]
    conn = _PinnedHTTPS(p.hostname, port, address, timeout=TIMEOUT, context=ssl.create_default_context())
    try:
        conn.request("GET", (p.path or "/") + ("?" + p.query if p.query else ""),
                     headers={"Accept": "application/json", "User-Agent": "Khala"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise CimdError("the document answered HTTP %d" % resp.status)
        body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise CimdError("the document is larger than 16 KB")
        try:
            doc = json.loads(body)
        except ValueError:
            raise CimdError("the document is not JSON")
        return doc, max_age(resp.getheader("Cache-Control"))
    except (OSError, http.client.HTTPException) as exc:
        raise CimdError("cannot fetch %s: %s" % (url, exc))
    finally:
        conn.close()


def to_client(url, doc, redirect_ok, scopes):
    """Validate a document against its URL and turn it into a public client."""
    if not isinstance(doc, dict) or doc.get("client_id") != url:
        raise CimdError("the document's client_id does not match its URL")
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and redirect_ok(u) for u in uris):
        raise CimdError("redirect_uris must be https or loopback URLs")
    if doc.get("token_endpoint_auth_method", "none") != "none":
        raise CimdError("only public clients (token_endpoint_auth_method none) are supported")
    name = doc.get("client_name") if isinstance(doc.get("client_name"), str) else ""
    return Client(client_id=url, client_name=name[:120] or urlparse(url).hostname, redirect_uris=uris,
                  token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
                  response_types=["code"], scope=" ".join(scopes))


class Documents:
    """Fetched documents, cached in memory per worker."""
    def __init__(self, redirect_ok, scopes, fetcher=fetch):
        self.redirect_ok, self.scopes, self.fetcher = redirect_ok, scopes, fetcher
        self.cache, self.lock = {}, threading.Lock()

    def get(self, url):
        now = time.time()
        with self.lock:
            hit = self.cache.get(url)
        if hit and hit[0] > now:
            return hit[1]
        try:
            doc, age = self.fetcher(url)
            client = to_client(url, doc, self.redirect_ok, self.scopes)
        except CimdError:
            if hit and hit[0] + STALE_OK > now:
                return hit[1]
            raise
        with self.lock:
            if len(self.cache) > 1000:
                self.cache.clear()
            self.cache[url] = (now + age, client)
        return client

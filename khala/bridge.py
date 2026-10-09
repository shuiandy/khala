"""stdio to remote bridge, for clients that can only start a local MCP server: each JSON-RPC line from stdin is
posted to the remote /mcp with a bearer token, and every reply goes back on stdout.

The token comes from KHALA_TOKEN or a file, never from the command line, where other users could read it in the
process list. Requests run concurrently; replies are written one whole line at a time.

Both protocol generations pass through. Under 2026-07-28 there is no initialize: each request names its version
in params._meta, and the HTTP transport routes on headers derived from the body (MCP-Protocol-Version, Mcp-Method,
Mcp-Name), which the bridge sets from each message the way the SDK's server checks them.
"""
import json
import sys
import threading
import urllib.error
import urllib.request

from mcp.shared.inbound import (MCP_METHOD_HEADER, MCP_NAME_HEADER, MCP_PROTOCOL_VERSION_HEADER, NAME_BEARING_METHODS,
                                encode_header_value)
from mcp_types import PROTOCOL_VERSION_META_KEY


def _messages(body, content_type):
    """The JSON-RPC messages in a reply: a JSON body, or the data lines of an event stream."""
    if not body.strip():
        return []
    if "text/event-stream" in content_type:
        out, data = [], []
        for line in body.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                out.append(json.loads("\n".join(data)))
                data = []
        return out
    parsed = json.loads(body)
    return parsed if isinstance(parsed, list) else [parsed]


def routing_headers(message, negotiated=None):
    """The version and routing headers for one message: a 2026-07-28 request carries its version in params._meta
    and needs Mcp-Method and, for named methods, Mcp-Name; an older one sends the version the handshake agreed."""
    if not isinstance(message, dict):
        return {}
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    version = meta.get(PROTOCOL_VERSION_META_KEY)
    if not isinstance(version, str):
        return {MCP_PROTOCOL_VERSION_HEADER: negotiated} if negotiated else {}
    out = {MCP_PROTOCOL_VERSION_HEADER: version}
    if isinstance(message.get("method"), str):
        out[MCP_METHOD_HEADER] = message["method"]
        key = NAME_BEARING_METHODS.get(message["method"])
        if key and isinstance(params.get(key), str):
            out[MCP_NAME_HEADER] = encode_header_value(params[key])
    return out


class Bridge:
    def __init__(self, url, token, out=None, timeout=120):
        self.url, self.token, self.timeout = url, token, timeout
        self.out = out or sys.stdout
        self.lock = threading.Lock()
        self.session = None
        self.protocol = None

    def write(self, message):
        with self.lock:
            self.out.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.out.flush()

    def forward(self, line):
        try:
            message = json.loads(line)
        except ValueError:
            self.write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}})
            return
        msg_id = message.get("id") if isinstance(message, dict) else None
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + self.token}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        headers.update(routing_headers(message, self.protocol))
        request = urllib.request.Request(self.url, data=line.encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                self.session = r.headers.get("Mcp-Session-Id") or self.session
                replies = _messages(r.read().decode("utf-8", "replace"), r.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            try:                                # the server's own JSON-RPC error, when it sent one, says more
                replies = [m for m in _messages(exc.read().decode("utf-8", "replace"),
                                                exc.headers.get("Content-Type", "")) if isinstance(m, dict)
                           and m.get("jsonrpc") == "2.0" and "error" in m and "id" in m]
            except ValueError:
                replies = []
            if not replies and msg_id is not None:
                reason = "the server refused the token; sign in again" if exc.code == 401 else "HTTP %d" % exc.code
                replies = [{"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": reason}}]
        except (urllib.error.URLError, OSError, ValueError) as exc:
            replies = [] if msg_id is None else [{"jsonrpc": "2.0", "id": msg_id,
                                                  "error": {"code": -32000, "message": "cannot reach %s: %s"
                                                            % (self.url, exc)}}]
        for reply in replies:
            result = reply.get("result") if isinstance(reply, dict) else None
            if isinstance(message, dict) and message.get("method") == "initialize" and isinstance(result, dict):
                self.protocol = result.get("protocolVersion") or self.protocol
            self.write(reply)

    def run(self, lines):
        threads = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if '"initialize"' in line:              # everything else waits for the handshake to finish
                self.forward(line)
                continue
            t = threading.Thread(target=self.forward, args=(line,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(self.timeout)

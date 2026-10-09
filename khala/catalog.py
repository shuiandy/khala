"""The client catalog (clients.yaml): how each known MCP client connects, and how to recognise it when it signs in.

render() fills an entry's channels for one server, or with local= its channels that start Khala on this computer over
stdio instead (they are never shown for a server); identify() matches a signing-in client to an entry. A metadata
document URL is a strong match (the server fetched and checked that document); a registered client name is weak,
since any client can claim it, and only ever suggests a name and kind.
"""
import base64
import json
import re
import shlex
from pathlib import Path
from urllib.parse import quote

import yaml

HERE = Path(__file__).parent
TYPES = {"link", "command", "connector", "file"}
FORMATS = {"json", "toml", "yaml"}
OS_KEYS = {"all", "macos", "linux", "windows"}


class CatalogError(Exception):
    pass


def load(path=HERE / "clients.yaml"):
    entries = yaml.safe_load(Path(path).read_text())
    validate(entries)
    return entries


def validate(entries):
    if not isinstance(entries, list) or not entries:
        raise CatalogError("the catalog is a non-empty list")
    seen = set()
    for e in entries:
        where = e.get("id", "?") if isinstance(e, dict) else "?"
        need = {"id", "name", "kind", "oauth", "identify", "verified", "sources", "channels"}
        if not isinstance(e, dict) or need - set(e):
            raise CatalogError("%s: missing %s" % (where, ", ".join(sorted(need - set(e or {})))))
        if e["id"] in seen or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", e["id"]):
            raise CatalogError("%s: id must be unique, lowercase letters, digits and dashes" % where)
        seen.add(e["id"])
        if e["kind"] not in ("device", "bot") or not e["sources"] or not e["channels"]:
            raise CatalogError("%s: kind, sources and channels" % where)
        for ch in e["channels"]:
            t = ch.get("type")
            if t not in TYPES or not ch.get("label") or ch.get("confidence") not in ("verified", "unverified"):
                raise CatalogError("%s: every channel needs a type, label and confidence" % where)
            if t in ("link", "command") and not ch.get("template"):
                raise CatalogError("%s: %s channel without template" % (where, t))
            if t == "command" and not ch.get("binary"):
                raise CatalogError("%s: command channel without binary" % where)
            if t == "file":
                if ch.get("format") not in FORMATS or not ch.get("path") or set(ch["path"]) - OS_KEYS:
                    raise CatalogError("%s: file channel needs format and path" % where)
                if ch["format"] == "json" and not (ch.get("merge") and ch.get("entry")):
                    raise CatalogError("%s: json file channel needs merge and entry" % where)
                if ch["format"] != "json" and not ch.get("snippet"):
                    raise CatalogError("%s: %s file channel needs a snippet" % (where, ch["format"]))
            if t == "connector" and not ch.get("steps"):
                raise CatalogError("%s: connector channel without steps" % where)
            text = json.dumps(ch)
            if "{token}" in text and not ch.get("needs_token"):
                raise CatalogError("%s: %s uses {token} without needs_token" % (where, ch["label"]))
            if ch.get("local") and (ch.get("needs_token") or "{url" in text or "{khala}" not in text
                                    or "{env}" not in text):
                raise CatalogError("%s: local channel %s starts {khala} with {env}, without a URL or token"
                                   % (where, ch["label"]))


def _fill(value, values):
    """Placeholders in every string of a nested value."""
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {_fill(k, values): _fill(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    return value


def _nest(keys, leaf):
    out = leaf
    for k in reversed(keys):
        out = {k: out}
    return out


def render_channel(ch, url, name, token=None, local=None):
    """One channel filled in. local is {"khala": program, "env": settings file}, both absolute paths."""
    local = local or {}
    values = {"url": url, "name": name, "token": token or "YOUR_TOKEN", "url_q": quote(url, safe=""),
              "name_q": quote(name, safe=""), "khala": local.get("khala", "khala"),
              "env": local.get("env", "khala.env")}
    if ch["type"] == "command":                 # a shell runs it, so paths with spaces stay whole
        values.update(khala=shlex.quote(values["khala"]), env=shlex.quote(values["env"]))
    out = {"type": ch["type"], "label": _fill(ch["label"], values), "needs_token": bool(ch.get("needs_token")),
           "local": bool(ch.get("local")), "confidence": ch["confidence"], "note": _fill(ch.get("note", ""), values)}
    if "config" in ch:
        config = json.dumps(_fill(ch["config"], values), separators=(",", ":"))
        b64 = base64.b64encode(config.encode()).decode()
        values.update(config_json_q=quote(config, safe=""), config_b64=b64, config_b64_q=quote(b64, safe=""),
                      config_b64url=base64.urlsafe_b64encode(config.encode()).decode().rstrip("="))
    if ch["type"] in ("link", "command"):
        out["text"] = _fill(ch["template"], values)
    if ch["type"] == "connector":
        out["steps"] = _fill(ch["steps"], values)
    if ch["type"] == "file":
        out["format"], out["path"] = ch["format"], _fill(ch["path"], values)
        if ch["format"] == "json":
            keys, entry = _fill(ch["merge"], values), _fill(ch["entry"], values)
            out["merge"], out["entry"] = keys, entry
            out["text"] = json.dumps(_nest(keys, entry), indent=2)
        else:
            out["text"] = _fill(ch["snippet"], values).rstrip("\n")
    elif ch.get("snippet"):
        out["text"] = _fill(ch["snippet"], values).rstrip("\n")
    elif ch["type"] == "connector" and "config" in ch:      # JSON to paste, escaped as JSON
        out["text"] = config
    if ch.get("binary"):
        out["binary"] = ch["binary"]
    return out


def render(entry, url, name, token=None, local=None):
    """The channels for a server, or with local the ones that start Khala on this computer."""
    return [render_channel(ch, url, name, token, local) for ch in entry["channels"]
            if bool(ch.get("local")) == bool(local)]


def identify(entries, client_id, client_name=""):
    """(entry, strong) for a signing-in client, or (None, False). Strong only for a metadata document URL."""
    for e in entries:
        ident = e.get("identify") or {}
        if client_id in ident.get("cimd", []) or (
                ident.get("cimd_pattern") and re.fullmatch(ident["cimd_pattern"], client_id or "")):
            return e, True
    name = (client_name or "").strip().lower()
    for e in entries:
        if name and name in [n.lower() for n in (e.get("identify") or {}).get("client_names", [])]:
            return e, False
    return None, False


_LOADED = []


def entries():
    """The packaged catalog, loaded once."""
    if not _LOADED:
        _LOADED.append(load())
    return _LOADED[0]


def by_id(entries, client):
    return next((e for e in entries if e["id"] == client), None)

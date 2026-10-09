"""khala connect: set up a client on this computer from the catalog, the quickest way it allows here.

It takes the catalog's channels in order and uses the first one this computer can carry out: run the client's own
command when that program is installed, merge a JSON config file (keeping a backup), or open an install link.
Anything else is printed as steps. When a token is needed it is fetched with device authorization, so it never
passes through the clipboard.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import catalog

OS = "macos" if sys.platform == "darwin" else "windows" if os.name == "nt" else "linux"


class ConnectError(Exception):
    pass


def say(message):
    print(message, flush=True)                  # progress is read live, including through a pipe


def endpoint(url):
    """The MCP endpoint, accepting the server's base URL too."""
    p = urllib.parse.urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ConnectError("--url must be the server address, for example https://memory.example.com")
    return urllib.parse.urlunparse(p._replace(path=p.path.rstrip("/") or "/mcp", query="", fragment=""))


def config_path(path):
    raw = path.get(OS) or path.get("all")
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else None


def merge_json(path, keys, entry, private=False):
    """Put entry at keys inside the JSON file, keeping everything else; a backup of the old file is left beside it.
    Returns the backup path, or None for a new file. Refuses files that are not plain JSON (comments, say)."""
    data, backup = {}, None
    if path.exists():
        text = path.read_text()
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError:
            raise ConnectError("%s is not plain JSON (comments?); add the snippet by hand" % path)
        if not isinstance(data, dict):
            raise ConnectError("%s does not hold a JSON object" % path)
        backup = path.with_name(path.name + ".khala-backup")
        backup.write_text(text)
    node = data
    for k in keys[:-1]:
        if not isinstance(node.get(k, {}), dict):
            raise ConnectError("%s: %s is not an object" % (path, k))
        node = node.setdefault(k, {})
    node[keys[-1]] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".khala-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    if private:                                 # it now holds a bearer token
        os.chmod(tmp, 0o600)
        if backup:
            os.chmod(backup, 0o600)
    os.replace(tmp, path)
    return backup


def _post(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read())
        except ValueError:
            raise ConnectError("the server answered HTTP %d" % exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise ConnectError("cannot reach %s: %s" % (url, exc))


def device_token(base, client_name, say=say, browser=True, sleep=time.sleep):
    """A bearer token through device authorization: show the code, wait for the person to approve it."""
    started = _post(base + "/device/code", {"client_name": client_name})
    if "device_code" not in started:
        raise ConnectError(started.get("error_description") or started.get("error") or "the server refused")
    say("To approve, open %s and enter the code %s" % (started["verification_uri"], started["user_code"]))
    if browser:
        webbrowser.open(started["verification_uri_complete"])
    interval, deadline = started.get("interval", 5), time.time() + started.get("expires_in", 600)
    while time.time() < deadline:
        sleep(interval)
        got = _post(base + "/device/token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                             "device_code": started["device_code"]})
        if "access_token" in got:
            return got["access_token"]
        error = got.get("error")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise ConnectError(got.get("error_description") or error or "the request failed")
    raise ConnectError("the code expired before it was approved")


def plan(entry, url, name, token):
    """The channels to try, in order, for this mode (token or sign-in)."""
    want_token = token is not None
    return [ch for ch in catalog.render(entry, url, name, token) if ch["needs_token"] == want_token]


def carry_out(channels, say=say, confirm=lambda q: True, dry_run=False, which=shutil.which,
              run=subprocess.run, open_link=webbrowser.open):
    """Use the first channel this computer can carry out; print the rest as steps. Returns what was done."""
    for ch in channels:
        if ch["type"] == "command" and which(ch["binary"]):
            say("Run: %s" % ch["text"])
            if dry_run or not confirm("Run this command?"):
                return "shown"
            done = run(shlex.split(ch["text"]))
            if done.returncode:
                raise ConnectError("%s exited with %d" % (ch["binary"], done.returncode))
            if ch["note"]:
                say(ch["note"])
            return "command"
        if ch["type"] == "file" and ch["format"] == "json" and config_path(ch["path"]):
            path = config_path(ch["path"])
            say("Add %s to %s" % (".".join(ch["merge"]), path))
            if dry_run or not confirm("Change this file?"):
                return "shown"
            backup = merge_json(path, ch["merge"], ch["entry"], private=ch["needs_token"])
            say("Updated %s%s." % (path, " (the old one is %s)" % backup if backup else ""))
            if ch["note"]:
                say(ch["note"])
            return "file"
        # app links (cursor://, vscode:...) open the app; https links are web pages, shown as steps instead
        if ch["type"] == "link" and not ch["text"].startswith("https://"):
            say("Open: %s" % ch["text"])
            if dry_run or not confirm("Open this install link?"):
                return "shown"
            open_link(ch["text"])
            if ch["note"]:
                say(ch["note"])
            return "link"
    if not channels:
        raise ConnectError("no way to connect this client in this mode")
    first = channels[0]
    say(first["label"] + ":")
    for step in first.get("steps", []):
        say("  - " + step)
    if first.get("text"):
        say(first["text"])
    if first.get("note"):
        say(first["note"])
    return "steps"

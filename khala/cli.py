"""Khala's command line. Day-to-day administration happens in the web UI at /app; this sets an instance up,
runs it, and is the fallback for operations on the server.

    khala init --admin you@example.com [--url URL] [--dir DIR] [--env FILE] [--name NAME] [--timezone ZONE]
                                                  new instance: data directory, first admin, settings file
    khala serve [--host 127.0.0.1] [--port 8100] [--workers 1]
                                                  run the server with the settings file
    khala serve --stdio [--as EMAIL]              one person on this computer, over stdio, without signing in;
                                                  for a client's MCP config ("command": "khala", ...)
    khala bridge URL [--token-file FILE]          stdio to a remote server, for clients that only start local
                                                  servers; the token comes from KHALA_TOKEN or the file
    khala connect CLIENT --url URL [--token]      set up a client on this computer (khala connect --list)
    khala migrate-refs                            move notes and proposals to refs/khala/* (the server also
                                                  does this at startup; deploy.sh runs it with the server stopped)

Every command reads settings from the environment, from --env FILE given first (`khala --env FILE users`), or
from ./khala.env when that exists. Values already in the environment win.

    khala users                                   accounts, aliases and grants
    khala add alice@example.com "Alice"         new account (creates the <handle> and <handle>-global scopes)
    khala alias owner@example.com other@example.com   add a sign-in email to an account
    khala grant alice@example.com team editor     role: viewer / editor / maintainer (legacy r / rw also accepted)
    khala revoke alice@example.com team
    khala disable alice@example.com              disable, revoking all their sessions and tokens now (remove is a synonym)
    khala enable alice@example.com
    khala scopes                                  scopes and owners
    khala auto-load personal-prefs on             load a scope in every session (it must have no members); off undoes it
    khala agents owner@example.com                an account's agents
    khala revoke-agent 12
    khala reset-auth alice@example.com           clear their passkeys, TOTP and recovery codes (all strong factors lost)

On a server installed by deploy.sh: sudo -u khala /srv/khala/app/.venv/bin/khala --env /etc/khala/server.env users
"""
import argparse
import json
import os
import sys

import uvicorn

from . import bridge, catalog, connect, instance
from .app import create_app
from .config import DEFAULT_REPO, Config, ConfigError, Settings, db_path
from .db import DB, ROLES
from .store import Store

LEGACY = {"r": "viewer", "rw": "editor"}


def init_command(argv):
    p = argparse.ArgumentParser(prog="khala init", description="Create a new instance.")
    p.add_argument("--admin", required=True, help="email address of the first admin")
    p.add_argument("--url", default="http://localhost:8100", help="public URL (default http://localhost:8100)")
    p.add_argument("--dir", default="khala-data", help="data directory (default ./khala-data)")
    p.add_argument("--env", default="khala.env", help="settings file to write (default ./khala.env)")
    p.add_argument("--name", default="", help="display name of the admin")
    p.add_argument("--timezone", default="UTC", help="time zone for dates, for example America/Toronto")
    a = p.parse_args(argv)
    try:
        made = instance.init(a.url, a.admin, a.dir, a.env, name=a.name, timezone=a.timezone)
    except instance.InitError as exc:
        sys.exit(str(exc))
    print("created %s and %s" % (made["data"], made["env_file"]))
    print("admin: %s" % a.admin.strip().lower())
    if made["local"]:
        print("next:  khala serve --env %s   then open %s/app (the sign-in code appears in the server log)"
              % (a.env, made["url"]))
    else:
        print("next:  fill in the SMTP settings in %s, put a TLS proxy in front of the port, then" % a.env)
        print("       khala serve --env %s   and open %s/app" % (a.env, made["url"]))


def serve_command(argv):
    p = argparse.ArgumentParser(prog="khala serve", description="Run the server.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--stdio", action="store_true", help="serve one local person over stdio instead of HTTP")
    p.add_argument("--as", dest="email", default="", help="with --stdio: the account (default: the first admin)")
    a = p.parse_args(argv)
    try:
        cfg = Config()                                  # settings problems stop here, with a clear message
    except ConfigError as exc:
        sys.exit("%s (or run `khala init`, or pass --env FILE)" % exc)
    problem = instance.missing_key(cfg)
    if problem:
        sys.exit(problem)
    if a.stdio:
        email = a.email.strip().lower()
        if not email:
            db = DB(cfg.db)
            admin = db.primary_admin()
            db.conn.close()
            if not admin:
                sys.exit("no admin yet; run `khala init` first")
            email = admin["email"]
        try:
            server = create_app(cfg, local=email)
        except RuntimeError as exc:
            sys.exit(str(exc))
        server.run("stdio")                             # stdout carries the protocol; logs go to stderr
        return
    trusted = cfg.trusted_proxies
    uvicorn.run("khala.app:create_app", factory=True, host=a.host, port=a.port, workers=a.workers,
                proxy_headers=bool(trusted), forwarded_allow_ips=trusted or None, server_header=False)


def bridge_command(argv):
    p = argparse.ArgumentParser(prog="khala bridge", description="Connect a stdio-only client to a remote server.")
    p.add_argument("url", help="the server's MCP endpoint, for example https://memory.example.com/mcp")
    p.add_argument("--token-file", default="", help="file holding the bearer token (default: KHALA_TOKEN)")
    a = p.parse_args(argv)
    token = os.environ.get("KHALA_TOKEN", "").strip()
    if a.token_file:
        with open(a.token_file) as f:
            token = f.read().strip()
    if not token:
        sys.exit("set KHALA_TOKEN or pass --token-file; create a token on the server's Agents page")
    bridge.Bridge(a.url, token).run(sys.stdin)


def connect_command(argv):
    p = argparse.ArgumentParser(prog="khala connect", description="Set up a client on this computer.")
    p.add_argument("client", nargs="?", help="a client id from --list")
    p.add_argument("--url", default="", help="the server, for example https://memory.example.com")
    p.add_argument("--name", default="khala", help="the server's name in the client's config (default khala)")
    p.add_argument("--token", action="store_true", help="use a token instead of signing in through the client")
    p.add_argument("--list", action="store_true", help="list the clients this knows")
    p.add_argument("--yes", action="store_true", help="do not ask before changing anything")
    p.add_argument("--dry-run", action="store_true", help="only say what would be done")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser for the approval")
    a = p.parse_args(argv)
    entries = catalog.entries()
    if a.list or not a.client:
        for e in entries:
            print("%-16s %s" % (e["id"], e["name"]))
        return
    entry = catalog.by_id(entries, a.client)
    if not entry:
        sys.exit("unknown client %s; khala connect --list shows them" % a.client)
    if not a.url:
        sys.exit("pass --url with the server's address")
    ask = (lambda q: True) if a.yes else (lambda q: input(q + " [y/N] ").strip().lower() in ("y", "yes"))
    try:
        url = connect.endpoint(a.url)
        token = None
        if a.token or not entry["oauth"]:
            if a.dry_run:
                token = "YOUR_TOKEN"
            else:
                base = url[:-len("/mcp")] if url.endswith("/mcp") else url
                token = connect.device_token(base, "khala connect (%s)" % entry["name"], browser=not a.no_browser)
        connect.carry_out(connect.plan(entry, url, a.name, token), confirm=ask, dry_run=a.dry_run)
    except connect.ConnectError as exc:
        sys.exit(str(exc))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--env"]:
        if len(argv) < 2 or not os.path.exists(argv[1]):
            sys.exit("--env needs an existing settings file")
        instance.load_env(argv[1])
        argv = argv[2:]
    elif os.path.exists("khala.env"):
        instance.load_env("khala.env")
    cmd = argv.pop(0) if argv else "users"
    if cmd == "init":
        return init_command(argv)
    if cmd == "serve":
        return serve_command(argv)
    if cmd == "bridge":
        return bridge_command(argv)
    if cmd == "connect":
        return connect_command(argv)
    if cmd == "migrate-refs" and not argv:
        s = Settings()
        moved = Store(s.get("REPO", DEFAULT_REPO), s.get("BRANCH", "main") or "main").migrate_refs()
        print("moved %d ref%s to refs/khala" % (len(moved), "" if len(moved) == 1 else "s"))
        return None
    path = db_path()
    if not path.exists():
        sys.exit("no state database at %s; run `khala init`, or point KHALA_DB or --env at an instance" % path)
    db = DB(path)

    def account(email):
        a = db.account_by_email(email)
        if not a:
            sys.exit("unknown account %s; run: khala add EMAIL NAME" % email)
        return a

    if cmd == "users" and not argv:
        for a in db.accounts():
            flags = (" admin" if a["is_admin"] else "") + ("" if a["status"] == "active" else " DISABLED")
            owned = [s["id"] for s in db.all_scopes() if s["owner_id"] == a["id"]]
            grants = ", ".join("%s:%s" % kv for kv in sorted(db.grants_for_account(a["id"]).items()))
            print("%-32s %-14s%s" % (a["email"], a["name"], flags))
            for alias in db.emails(a["id"]):
                if alias != a["email"]:
                    print("    alias  %s" % alias)
            print("    owns   %s" % (", ".join(owned) or "-"))
            print("    shared %s" % (grants or "-"))
    elif cmd == "add" and len(argv) == 2:
        if db.account_by_email(argv[0]):
            sys.exit("account exists")
        aid = db.create_account(argv[0], argv[1])
        db.audit("account.created", aid, target=argv[0].lower(), detail="cli")
        print("added", argv[0].lower())
    elif cmd == "alias" and len(argv) == 2:
        a = account(argv[0])
        if db.account_by_email(argv[1]):
            sys.exit("that email already belongs to an account")
        db.add_alias(a["id"], argv[1])
        db.audit("account.alias", a["id"], target=argv[1].lower(), detail="cli")
        print("added login email", argv[1].lower(), "to", a["email"])
    elif cmd == "grant" and len(argv) == 3 and LEGACY.get(argv[2], argv[2]) in ROLES:
        a, role = account(argv[0]), LEGACY.get(argv[2], argv[2])
        s = db.scope(argv[1])
        if not s:
            sys.exit("unknown scope %s (see: khala scopes)" % argv[1])
        if s["auto_load"] or not s["shareable"]:
            sys.exit("auto-loaded scopes cannot be shared")
        if s["owner_id"] == a["id"]:
            sys.exit("that account owns the scope")
        db.set_grant(s["id"], a["id"], role, None)
        db.audit("grant.set", None, target=s["id"], detail={"account": a["email"], "role": role, "via": "cli"})
        print("granted", s["id"], role, "to", a["email"])
    elif cmd == "revoke" and len(argv) == 2:
        a = account(argv[0])
        db.remove_grant(argv[1], a["id"])
        db.audit("grant.removed", None, target=argv[1], detail={"account": a["email"], "via": "cli"})
        print("revoked", argv[1], "from", a["email"])
    elif cmd in ("disable", "remove", "enable") and len(argv) == 1:
        a = account(argv[0])
        status = "active" if cmd == "enable" else "disabled"
        db.set_account_status(a["id"], status)
        db.audit("account." + ("enable" if cmd == "enable" else "disable"), None, target=a["email"], detail="cli")
        print(status, a["email"])
    elif cmd == "scopes" and not argv:
        owners = {a["id"]: a["email"] for a in db.accounts()}
        for s in db.all_scopes():
            flags = (" auto-load" if s["auto_load"] else "") + (" archived" if s["archived_at"] else "")
            print("%-24s %s%s" % (s["id"], owners.get(s["owner_id"], "?"), flags))
    elif cmd == "auto-load" and len(argv) == 2 and argv[1] in ("on", "off"):
        if not db.scope(argv[0]):
            sys.exit("unknown scope %s" % argv[0])
        if db.set_auto_load(argv[0], argv[1] == "on"):
            print("%s: auto-load %s" % (argv[0], argv[1]))
        elif argv[1] == "on":
            sys.exit("%s was not changed: it is already auto-loaded, archived, or still has members or open "
                     "invitations" % argv[0])
        else:
            print("%s was not auto-loaded" % argv[0])
    elif cmd == "agents" and len(argv) == 1:
        for g in db.agents_for(account(argv[0])["id"]):
            ceiling = json.loads(g["ceiling"]) if g["ceiling"] else "all"
            state = "revoked" if g["revoked_at"] else "active"
            print("%4d  %-28s %-6s %-7s %s" % (g["id"], g["name"], g["kind"], state, ceiling))
    elif cmd == "reset-auth" and len(argv) == 1:
        a = account(argv[0])
        with db.tx():
            for t in ("passkeys", "totp", "recovery_codes", "half_logins"):
                db.conn.execute("DELETE FROM %s WHERE account_id=?" % t, (a["id"],))
            db.conn.execute("DELETE FROM web_sessions WHERE account_id=?", (a["id"],))
        db.audit("auth.reset", None, target=a["email"], detail="cli")
        print("cleared passkeys, authenticator app and recovery codes for", a["email"],
              "; they can sign in with an email code again")
    elif cmd == "revoke-agent" and len(argv) == 1 and argv[0].isdigit():
        db.revoke_agent(int(argv[0]))
        db.audit("agent.revoked", None, int(argv[0]), detail="cli")
        print("revoked agent", argv[0])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

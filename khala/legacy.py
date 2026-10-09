"""Migration from the first schema (users and grants by email), which only the original single-user instance ever
had. Nothing else in the server depends on it; it runs once, inside the schema migration's transaction."""
import json
import time

AUTO_LOAD = "global"            # the first schema's convention for the scope every session loads


def prepare_v1(c, tables):
    """Before the new schema is created: move the old tables out of the way. Returns whether this is a v1 database."""
    if "users" not in tables or "accounts" in tables:
        return False
    c.execute("ALTER TABLE grants RENAME TO grants_v1")
    for t in ("pending", "auth_codes"):          # half-finished, expire within minutes; drop them
        c.execute("DROP TABLE IF EXISTS %s" % t)
    if "tokens" in tables:
        c.execute("ALTER TABLE tokens ADD COLUMN agent_id INTEGER")
    return True


def migrate_v1(db):
    """users/grants(email, scope, r|rw) → accounts, scopes, roles; live tokens of each (user, client) go to one
    unnamed agent.

    All owner emails are the same person: merged into one admin account, the first as the primary email and
    the rest as sign-in aliases.
    """
    c, now = db.conn, time.time()
    users = c.execute("SELECT * FROM users ORDER BY is_owner DESC, created, email").fetchall()
    primary = None
    by_email = {}
    for u in users:
        if u["is_owner"] and primary is not None:
            c.execute("INSERT INTO account_emails(email, account_id) VALUES (?,?)", (u["email"], primary))
            by_email[u["email"]] = primary
            continue
        aid = db._insert_account(u["email"], u["name"], bool(u["is_owner"]), u["created"])
        by_email[u["email"]] = aid
        if u["is_owner"]:
            primary = aid
    if primary is not None:
        db._ensure_scope(AUTO_LOAD, primary, now, auto_load=True)
    for g in c.execute("SELECT * FROM grants_v1").fetchall():
        aid = by_email.get(g["email"])
        if aid is None or aid == primary or primary is None or g["scope"] == AUTO_LOAD:
            continue
        db._ensure_scope(g["scope"], primary, now)
        c.execute("INSERT OR REPLACE INTO grants(scope_id, account_id, role, granted_by, created_at) "
                  "VALUES (?,?,?,?,?)", (g["scope"], aid, "editor" if g["mode"] == "rw" else "viewer", primary, now))
    c.execute("DROP TABLE grants_v1")
    c.execute("DROP TABLE users")
    c.execute("DELETE FROM tokens WHERE revoked=1 OR expires<?", (now,))
    names = {r["client_id"]: (json.loads(r["info"]).get("client_name") or "") for r in
             c.execute("SELECT client_id, info FROM clients")}
    groups = c.execute("SELECT subject, client_id, MAX(scopes LIKE '%offline_access%') bot FROM tokens "
                       "GROUP BY subject, client_id").fetchall()
    agents = {}                                 # (account, client) -> agent; alias emails' tokens share it
    for g in groups:
        aid = by_email.get(g["subject"])
        if aid is None:
            c.execute("DELETE FROM tokens WHERE subject=? AND client_id=?", (g["subject"], g["client_id"]))
            continue
        key = (aid, g["client_id"])
        if key not in agents:
            label = names.get(g["client_id"]) or "Unnamed agent"
            agents[key] = c.execute(
                "INSERT INTO agents(account_id, name, kind, oauth_client_id, created_at) VALUES (?,?,?,?,?)",
                (aid, label, "bot" if g["bot"] else "device", g["client_id"], now)).lastrowid
        elif g["bot"]:
            c.execute("UPDATE agents SET kind='bot' WHERE id=?", (agents[key],))
        c.execute("UPDATE tokens SET agent_id=?, subject=? WHERE subject=? AND client_id=?",
                  (agents[key], str(aid), g["subject"], g["client_id"]))
    c.execute("INSERT INTO audit_events(at, account_id, action, detail) VALUES (?,?,?,?)",
              (now, primary, "instance.migrated", "schema v1 -> v2"))
